"""Shared vocabulary: data-status, pool types, quote methods and provenance.

The :class:`DataStatus` enum is the mechanism that keeps verified, derived,
estimated, simulated and unavailable data distinguishable everywhere - in the
database, the API and the dashboard. Nothing in this project may present one
category as another.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC
from enum import StrEnum
from typing import Any


class DataStatus(StrEnum):
    """How a stored or returned value was obtained."""

    #: Read directly from the chain in this observation (RPC response).
    VERIFIED = "verified"
    #: Computed from verified values by a documented formula.
    DERIVED = "derived"
    #: Produced by an estimator (gas estimates, peg-based USD conversions).
    ESTIMATED = "estimated"
    #: Produced by a contract simulation or pool mathematics (quotes).
    SIMULATED = "simulated"
    #: The source data does not exist; no substitute was created.
    UNAVAILABLE = "unavailable"
    #: Part of the record could be read, part could not.
    PARTIAL = "partial"
    #: The read failed; the error is recorded.
    ERROR = "error"
    #: The observation exists but is older than the freshness window.
    STALE = "stale"


class PoolType(StrEnum):
    """Supported automated market maker models."""

    CONSTANT_PRODUCT = "constant_product"
    CONCENTRATED_LIQUIDITY = "concentrated_liquidity"


class QuoteMethod(StrEnum):
    """Which mechanism produced a quote.

    ``router_quote`` and ``contract_simulation`` both call the protocol's own
    contracts. ``pool_math`` is a local calculation and is never described as a
    simulation because no protocol contract was executed.
    """

    ROUTER_QUOTE = "router_quote"
    CONTRACT_SIMULATION = "contract_simulation"
    POOL_MATH = "pool_math"
    SDK_QUOTE = "sdk_quote"
    NO_SUPPORT = "no_support"


@dataclass(frozen=True)
class Provenance:
    """Everything needed to reproduce where an observation came from."""

    source: str
    method: str | None = None
    chain_id: int | None = None
    block_number: int | None = None
    block_hash: str | None = None
    transaction_hash: str | None = None
    log_index: int | None = None
    contract_address: str | None = None
    observed_at: str | None = None
    data_status: DataStatus = DataStatus.VERIFIED
    detail: str | None = None
    sources: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "source": self.source,
            "method": self.method,
            "chain_id": self.chain_id,
            "block_number": self.block_number,
            "block_hash": self.block_hash,
            "transaction_hash": self.transaction_hash,
            "log_index": self.log_index,
            "contract_address": self.contract_address,
            "observed_at": self.observed_at,
            "data_status": self.data_status.value,
            "detail": self.detail,
        }
        if self.sources:
            payload["sources"] = list(self.sources)
        return payload

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str | None) -> Provenance | None:
        if not raw:
            return None
        data = json.loads(raw)
        return cls(
            source=data.get("source", "unknown"),
            method=data.get("method"),
            chain_id=data.get("chain_id"),
            block_number=data.get("block_number"),
            block_hash=data.get("block_hash"),
            transaction_hash=data.get("transaction_hash"),
            log_index=data.get("log_index"),
            contract_address=data.get("contract_address"),
            observed_at=data.get("observed_at"),
            data_status=DataStatus(data.get("data_status", DataStatus.VERIFIED.value)),
            detail=data.get("detail"),
            sources=list(data.get("sources") or []),
        )


def utc_now_iso() -> str:
    """Current UTC time in a fixed, sortable format."""
    from datetime import datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def seconds_since(iso_timestamp: str | None, *, now: str | None = None) -> float | None:
    """Age in seconds of an ISO-8601 UTC timestamp, or ``None`` if unparseable."""
    from datetime import datetime

    if not iso_timestamp:
        return None
    try:
        observed = datetime.strptime(iso_timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC
        )
    except ValueError:
        return None
    reference = (
        datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        if now
        else datetime.now(UTC)
    )
    return (reference - observed).total_seconds()