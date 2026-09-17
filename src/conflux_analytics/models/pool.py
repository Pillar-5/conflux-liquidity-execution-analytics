"""Pool model.

Token ordering follows the protocol: ``token0``/``token1`` come from the pool
contract and are **not** base/quote. Any code that depends on a direction must
state it explicitly (see :meth:`Pool.token_index`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..chain.addresses import normalize_address
from .common import DataStatus, PoolType, Provenance


@dataclass(frozen=True)
class Pool:
    """A discovered DEX market."""

    chain_id: int
    pool_address: str
    dex_id: str
    token0_address: str
    token1_address: str
    pool_type: PoolType
    fee_bps: int | None = None
    fee_tier_raw: int | None = None
    tick_spacing: int | None = None
    discovery_source: str = "unknown"
    verification_method: str | None = None
    verified_at: str | None = None
    active: bool = True
    status: DataStatus = DataStatus.VERIFIED
    detail: str | None = None
    provenance: Provenance | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "pool_address", normalize_address(self.pool_address))
        object.__setattr__(self, "token0_address", normalize_address(self.token0_address))
        object.__setattr__(self, "token1_address", normalize_address(self.token1_address))

    @property
    def key(self) -> tuple[int, str]:
        return (self.chain_id, self.pool_address)

    @property
    def token_addresses(self) -> tuple[str, str]:
        return (self.token0_address, self.token1_address)

    def token_index(self, token_address: str) -> int | None:
        """Return 0 or 1 for a pool token, or ``None`` when it is not in the pool."""
        candidate = normalize_address(token_address)
        if candidate == self.token0_address:
            return 0
        if candidate == self.token1_address:
            return 1
        return None

    def counterparty(self, token_address: str) -> str | None:
        """Return the other token of the pool."""
        index = self.token_index(token_address)
        if index is None:
            return None
        return self.token1_address if index == 0 else self.token0_address

    def contains(self, token_address: str) -> bool:
        return self.token_index(token_address) is not None

    def as_dict(self, token_symbols: dict[str, str] | None = None) -> dict[str, Any]:
        symbols = token_symbols or {}
        return {
            "chain_id": self.chain_id,
            "pool_address": self.pool_address,
            "dex_id": self.dex_id,
            "token0_address": self.token0_address,
            "token1_address": self.token1_address,
            "token0_symbol": symbols.get(self.token0_address),
            "token1_symbol": symbols.get(self.token1_address),
            "pool_type": self.pool_type.value,
            "fee_bps": self.fee_bps,
            "fee_tier_raw": self.fee_tier_raw,
            "tick_spacing": self.tick_spacing,
            "discovery_source": self.discovery_source,
            "verification_method": self.verification_method,
            "verified_at": self.verified_at,
            "active": self.active,
            "status": self.status.value,
            "detail": self.detail,
            "provenance": self.provenance.as_dict() if self.provenance else None,
        }