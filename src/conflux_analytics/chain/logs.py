"""Bounded, self-adjusting log fetching.

``eth_getLogs`` is only ever issued over a bounded range. When a provider
rejects a request because the range or result set is too large, the fetcher
halves the chunk size and retries, recording the effective chunk size so the
next run starts from a value that is known to work.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..errors import RpcError
from ..logging_setup import get_logger
from ..rpc.provider import RpcProvider

logger = get_logger(__name__)

#: Substrings that indicate "your request was too large" rather than a bug.
RANGE_TOO_LARGE_MARKERS = (
    "too large",
    "more than",
    "exceeds",
    "limit",
    "block range",
    "query timeout",
    "response size",
    "result set",
    "out of range",
)


@dataclass
class LogFetchResult:
    """Logs plus how they were obtained."""

    logs: list[dict[str, Any]] = field(default_factory=list)
    chunks_requested: int = 0
    requests_issued: int = 0
    effective_chunk_size: int = 0
    shrink_events: int = 0
    failed_chunks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def log_count(self) -> int:
        return len(self.logs)


class LogFetcher:
    """Fetches logs in descending chunk sizes, shrinking on provider limits."""

    def __init__(
        self,
        provider: RpcProvider,
        chunk_size: int,
        min_chunk_size: int = 1,
        max_shrinks: int = 6,
    ) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        self.provider = provider
        self.chunk_size = chunk_size
        self.min_chunk_size = max(1, min_chunk_size)
        self.max_shrinks = max_shrinks

    def fetch(
        self,
        address: str | Sequence[str],
        topics: Sequence[Any] | None,
        from_block: int,
        to_block: int,
    ) -> LogFetchResult:
        """Fetch logs for an inclusive range, adapting to provider limits."""
        result = LogFetchResult(effective_chunk_size=self.chunk_size)
        chunk = self.chunk_size
        cursor = from_block
        shrinks = 0
        while cursor <= to_block:
            last = min(cursor + chunk - 1, to_block)
            result.chunks_requested += 1
            try:
                result.requests_issued += 1
                batch = self.provider.get_logs(
                    address=address, topics=topics, from_block=cursor, to_block=last
                )
            except RpcError as exc:
                message = f"{exc.rpc_message or exc}"
                if (
                    _is_range_too_large(message)
                    and chunk > self.min_chunk_size
                    and shrinks < self.max_shrinks
                ):
                    chunk = max(chunk // 2, self.min_chunk_size)
                    shrinks += 1
                    result.shrink_events += 1
                    result.effective_chunk_size = chunk
                    logger.warning(
                        "reducing getLogs block range after provider limit",
                        extra={
                            "event": "logs.shrink",
                            "from_block": cursor,
                            "chunk_size": chunk,
                            "error": message,
                        },
                    )
                    continue
                result.failed_chunks.append(
                    {"from_block": cursor, "to_block": last, "error": message}
                )
                logger.error(
                    "getLogs chunk failed",
                    extra={
                        "event": "logs.chunk_error",
                        "from_block": cursor,
                        "to_block": last,
                        "error": message,
                    },
                )
                cursor = last + 1
                continue
            result.logs.extend(batch)
            cursor = last + 1
        result.effective_chunk_size = chunk
        return result


def _is_range_too_large(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in RANGE_TOO_LARGE_MARKERS)


def sort_logs(logs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deterministic ordering: block number, then log index."""

    def key(log: dict[str, Any]) -> tuple[int, int]:
        return (int(log.get("blockNumber", "0x0"), 16), int(log.get("logIndex", "0x0"), 16))

    return sorted(logs, key=key)


def log_index_of(log: dict[str, Any]) -> int:
    """Decode the ``logIndex`` field of a raw log."""
    return int(log.get("logIndex", "0x0"), 16)


def block_number_of(log: dict[str, Any]) -> int:
    """Decode the ``blockNumber`` field of a raw log."""
    return int(log.get("blockNumber", "0x0"), 16)


def topic_of(log: dict[str, Any], index: int = 0) -> str | None:
    """Return topic ``index`` of a raw log, or ``None`` when absent."""
    topics = log.get("topics") or []
    if index < len(topics):
        value = topics[index]
        return value if isinstance(value, str) else None
    return None


def decode_indexed_address(topic: str | None) -> str | None:
    """Decode an indexed address topic into a lower-cased address."""
    if not topic or not isinstance(topic, str) or len(topic) < 42:
        return None
    return "0x" + topic[-40:].lower()