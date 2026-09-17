"""Retry policy with exponential backoff and bounded attempts.

Transient failures (HTTP 429/5xx, timeouts, transient JSON-RPC error codes) are
retried. Permanent failures - a revert, an unsupported method, a malformed
request - are raised immediately so a broken call cannot stall a collection run
or be hidden by repeated attempts.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from ..errors import (
    RpcError,
    RpcMalformedResponseError,
    RpcMethodNotSupportedError,
    RpcRateLimitError,
    RpcTimeoutError,
    RpcTransportError,
)
from ..logging_setup import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

#: JSON-RPC error codes that indicate a transient condition worth retrying.
RETRYABLE_RPC_CODES = frozenset({-32005, -32603, 429})
#: JSON-RPC error code for "method not found".
METHOD_NOT_FOUND_CODE = -32601
NOT_SUPPORTED_MESSAGE_MARKERS = (
    "method not found",
    "not supported",
    "not allowed",
    "access forbidden",
    "is not available",
    "unknown method",
    "unsupported",
)


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded retry configuration."""

    max_retries: int = 3
    backoff_seconds: float = 0.5
    max_backoff_seconds: float = 8.0
    sleep: Callable[[float], None] = time.sleep

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if self.backoff_seconds < 0:
            raise ValueError("backoff_seconds must be >= 0")

    def delay_for(self, attempt: int) -> float:
        """Exponential backoff for a zero-based ``attempt`` index."""
        return min(self.backoff_seconds * (2**attempt), self.max_backoff_seconds)

    def attempts(self) -> int:
        """Total number of attempts (initial call plus retries)."""
        return self.max_retries + 1


def classify(exc: BaseException) -> str:
    """Classify an RPC failure as ``transient`` or ``permanent``."""
    if isinstance(exc, RpcRateLimitError):
        return "transient"
    if isinstance(exc, RpcTimeoutError):
        return "transient"
    if isinstance(exc, RpcMalformedResponseError):
        return "transient"
    if isinstance(exc, RpcMethodNotSupportedError):
        return "permanent"
    if isinstance(exc, RpcTransportError):
        return "transient"
    if isinstance(exc, RpcError):
        if exc.code in RETRYABLE_RPC_CODES:
            return "transient"
        message = (exc.rpc_message or "").lower()
        if any(marker in message for marker in ("timeout", "rate limit", "try again")):
            return "transient"
        return "permanent"
    return "permanent"


def is_method_supported_error(exc: BaseException) -> bool:
    """Return ``True`` when the failure means "this method is not available"."""
    if isinstance(exc, RpcMethodNotSupportedError):
        return True
    if isinstance(exc, RpcError):
        message = (exc.rpc_message or "").lower()
        return exc.code == METHOD_NOT_FOUND_CODE or any(
            marker in message for marker in NOT_SUPPORTED_MESSAGE_MARKERS
        )
    return False


def run_with_retries(
    operation: Callable[[], T],
    policy: RetryPolicy,
    method: str,
    **log_fields: Any,
) -> T:
    """Execute ``operation`` with bounded retries.

    Raises the final exception when every attempt fails or when the failure is
    classified as permanent.
    """
    last_error: BaseException | None = None
    for attempt in range(policy.attempts()):
        try:
            return operation()
        except Exception as exc:  # noqa: BLE001 - re-raised or retried deliberately
            last_error = exc
            kind = classify(exc)
            remaining = policy.attempts() - attempt - 1
            if kind == "permanent" or remaining <= 0:
                raise
            delay = policy.delay_for(attempt)
            logger.warning(
                "retrying RPC call",
                extra={
                    "event": "rpc.retry",
                    "method": method,
                    "attempt": attempt + 1,
                    "max_attempts": policy.attempts(),
                    "delay_seconds": delay,
                    "error": str(exc),
                    **log_fields,
                },
            )
            policy.sleep(delay)
    assert last_error is not None  # pragma: no cover - loop always raises
    raise last_error