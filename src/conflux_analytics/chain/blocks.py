"""Block references and bounded block-range handling.

Every collection is expressed as an explicit, bounded range. Ranges are never
unbounded, and the helper here is the only place where ``latest`` is resolved
into a concrete number so that a run can be reproduced afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import ConfigurationError
from ..rpc.provider import RpcProvider


@dataclass(frozen=True)
class BlockRef:
    """A resolved block reference."""

    number: int
    hash: str | None = None
    timestamp: int | None = None
    parent_hash: str | None = None
    gas_limit: int | None = None
    gas_used: int | None = None
    base_fee_per_gas: int | None = None
    transaction_count: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "hash": self.hash,
            "timestamp": self.timestamp,
            "parent_hash": self.parent_hash,
            "gas_limit": self.gas_limit,
            "gas_used": self.gas_used,
            "base_fee_per_gas": str(self.base_fee_per_gas)
            if self.base_fee_per_gas is not None
            else None,
            "transaction_count": self.transaction_count,
        }


def parse_block(raw: dict[str, Any] | None) -> BlockRef | None:
    """Convert a raw ``eth_getBlockByNumber`` payload into a :class:`BlockRef`."""
    if not raw or "number" not in raw:
        return None
    return BlockRef(
        number=int(raw["number"], 16),
        hash=raw.get("hash"),
        timestamp=int(raw["timestamp"], 16) if raw.get("timestamp") else None,
        parent_hash=raw.get("parentHash"),
        gas_limit=int(raw["gasLimit"], 16) if raw.get("gasLimit") else None,
        gas_used=int(raw["gasUsed"], 16) if raw.get("gasUsed") else None,
        base_fee_per_gas=int(raw["baseFeePerGas"], 16) if raw.get("baseFeePerGas") else None,
        transaction_count=len(raw.get("transactions", []) or []) if "transactions" in raw else None,
    )


def fetch_block(provider: RpcProvider, number: int) -> BlockRef | None:
    """Fetch a block reference by number."""
    return parse_block(provider.get_block_by_number(number, False))


@dataclass(frozen=True)
class BlockRange:
    """A validated, bounded inclusive block range."""

    start: int
    end: int
    chunk_size: int

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < 0:
            raise ConfigurationError("block numbers must be non-negative")
        if self.end < self.start:
            raise ConfigurationError(
                f"end block {self.end} precedes start block {self.start}"
            )
        if self.chunk_size <= 0:
            raise ConfigurationError("chunk_size must be positive")

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    def chunks(self) -> list[tuple[int, int]]:
        """Split into inclusive ``(from_block, to_block)`` chunks."""
        ranges: list[tuple[int, int]] = []
        cursor = self.start
        while cursor <= self.end:
            last = min(cursor + self.chunk_size - 1, self.end)
            ranges.append((cursor, last))
            cursor = last + 1
        return ranges

    def as_dict(self) -> dict[str, Any]:
        return {
            "start_block": self.start,
            "end_block": self.end,
            "chunk_size": self.chunk_size,
            "block_count": self.length,
        }


def resolve_block_range(
    *,
    head: int,
    start_block: int | None,
    end_block: int | None,
    default_lookback_blocks: int,
    max_blocks_per_run: int,
    chunk_size: int,
) -> BlockRange:
    """Resolve configuration into a concrete bounded range.

    ``block_count`` is capped at ``max_blocks_per_run`` so that a single run
    cannot silently turn into an unbounded scan. When the requested range is
    larger, the most recent blocks are kept and the truncation is reported by
    the caller.
    """
    resolved_end = head if end_block is None else min(end_block, head)
    if start_block is None:
        resolved_start = max(resolved_end - default_lookback_blocks + 1, 0)
    else:
        resolved_start = start_block
    if resolved_start > resolved_end:
        raise ConfigurationError(
            f"resolved start block {resolved_start} is after end block {resolved_end}"
        )
    if resolved_end - resolved_start + 1 > max_blocks_per_run:
        resolved_start = resolved_end - max_blocks_per_run + 1
    return BlockRange(start=resolved_start, end=resolved_end, chunk_size=chunk_size)