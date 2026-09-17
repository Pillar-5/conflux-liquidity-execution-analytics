"""Token model.

Canonical identity is ``(chain_id, address)``. Symbols and names are display
labels only: they are optional, may be missing on non-standard contracts and
are never used as identifiers.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ..chain.addresses import normalize_address
from ..chain.units import from_raw
from .common import DataStatus, Provenance


@dataclass(frozen=True)
class Token:
    """An ERC-20 token as observed on chain."""

    chain_id: int
    address: str
    symbol: str | None = None
    name: str | None = None
    decimals: int | None = None
    total_supply_raw: int | None = None
    metadata_status: DataStatus = DataStatus.VERIFIED
    metadata_detail: str | None = None
    fetched_at: str | None = None
    provenance: Provenance | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "address", normalize_address(self.address))

    @property
    def key(self) -> tuple[int, str]:
        return (self.chain_id, self.address)

    @property
    def label(self) -> str:
        """Display label that never pretends to be an identifier."""
        if self.symbol:
            return self.symbol
        return f"{self.address[:8]}…{self.address[-4:]}"

    @property
    def has_metadata(self) -> bool:
        return self.decimals is not None

    def to_decimal(self, raw: int | str) -> Decimal | None:
        """Convert a raw amount, or ``None`` when decimals are unknown.

        Returning ``None`` rather than assuming 18 decimals keeps an unknown
        token from silently producing a wrong quantity.
        """
        if self.decimals is None:
            return None
        return from_raw(raw, self.decimals)

    def as_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "address": self.address,
            "symbol": self.symbol,
            "name": self.name,
            "decimals": self.decimals,
            "total_supply_raw": str(self.total_supply_raw)
            if self.total_supply_raw is not None
            else None,
            "metadata_status": self.metadata_status.value,
            "metadata_detail": self.metadata_detail,
            "fetched_at": self.fetched_at,
            "provenance": self.provenance.as_dict() if self.provenance else None,
        }