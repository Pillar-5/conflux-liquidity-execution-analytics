"""Structured logging for collection, analytics and serving.

Two formats are supported:

``text``  human readable, single line per event, ``key=value`` context.
``json``  one JSON object per line, suitable for ingestion elsewhere.

Every pipeline operation logs a stable ``event`` name plus contextual fields
(``run_id``, ``component``, ``dex``, ``pool``, ``block_range``, ``duration_ms``,
``status``, ``error_category``) so that a market failure can be explained
without reading the source.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

_CONFIGURED = False
_RESERVED = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
        "message",
        "asctime",
    }
)


class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
        }
        event = getattr(record, "event", None)
        if event:
            payload["event"] = event
        payload["message"] = record.getMessage()
        for key, value in record.__dict__.items():
            if key in _RESERVED or key == "event" or key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """Render log records as readable ``key=value`` lines."""

    def format(self, record: logging.LogRecord) -> str:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created))
        parts = [stamp, record.levelname.ljust(7), record.name]
        event = getattr(record, "event", None)
        if event:
            parts.append(f"event={event}")
        parts.append(record.getMessage())
        for key, value in record.__dict__.items():
            if key in _RESERVED or key == "event" or key.startswith("_"):
                continue
            parts.append(f"{key}={value}")
        line = " ".join(str(p) for p in parts)
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def configure_logging(
    level: str = "INFO",
    fmt: str = "text",
    file: str | None = None,
) -> None:
    """Configure the root logger once per process."""
    global _CONFIGURED
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    formatter: logging.Formatter = JsonFormatter() if fmt == "json" else TextFormatter()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if file:
        handlers.append(logging.FileHandler(file, encoding="utf-8"))
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)
    resolved = getattr(logging, str(level).upper(), logging.INFO)
    root.setLevel(resolved if isinstance(resolved, int) else logging.INFO)
    # Third-party HTTP client chatter is not part of our structured stream; a
    # per-request INFO line from httpx would drown the pipeline events.
    for noisy in ("httpx", "httpcore", "asyncio", "uvicorn.error"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a module logger, configuring defaults on first use."""
    if not _CONFIGURED:
        configure_logging()
    return logging.getLogger(name)


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    message: str,
    **fields: Any,
) -> None:
    """Log ``message`` with a stable ``event`` name and structured fields."""
    logger.log(level, message, extra={"event": event, **fields})


@contextmanager
def logged_operation(
    logger: logging.Logger,
    event: str,
    message: str,
    **fields: Any,
) -> Iterator[dict[str, Any]]:
    """Time an operation and log its outcome, including failure category.

    Yields a mutable context dictionary; set ``context["status"]`` inside the
    block to override the reported status. Exceptions are logged with their
    structured category and re-raised - they are never swallowed.
    """
    from .errors import error_category

    context: dict[str, Any] = {"status": "ok", "error_category": None}
    started = time.perf_counter()
    try:
        yield context
    except Exception as exc:
        context["status"] = "error"
        context["error_category"] = error_category(exc)
        log_event(
            logger,
            logging.ERROR,
            event,
            message,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            error=str(exc),
            **{**fields, **context},
        )
        raise
    else:
        log_event(
            logger,
            logging.INFO if context["status"] == "ok" else logging.WARNING,
            event,
            message,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            **{**fields, **context},
        )
