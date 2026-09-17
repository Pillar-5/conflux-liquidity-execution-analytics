"""Event models for swaps, liquidity changes and historical gas.

Normalisation rule for swap amounts
-----------------------------------
``amount0_raw`` / ``amount1_raw`` are **signed deltas of the pool's balance**:
positive means the token entered the pool (the trader sold it), negative means
the token left the pool (the trader bought it).

* Uniswap-V2 style ``Swap`` events report ``amount0In/amount0Out`` separately,
  so the delta is ``amount0In - amount0Out``.
* Uniswap-V3 style ``Swap`` events already report signed ``amount0``/``amount1``
  deltas, which are used unchanged.

One table and one analytical path therefore serve both market models without
pretending they are identical.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .common import DataStatus, PoolType, Provenance


@dataclass(frozen=True)
class SwapEvent:
    """A normalised swap observed in a pool."""

    chain_id: int
    dex_id: str
    pool_address: str
    pool_type: PoolType
    block_number: int
    transaction_hash: str
    log_index: int
    amount0_raw: int
    amount1_raw: int
    observed_at: str
    block_hash: str | None = None
    block_timestamp: int | None = None
    sender: str | None = None
    recipient: str | None = None
    sqrt_price_x96: int | None = None
    liquidity: int | None = None
    tick: int | None = None
    run_id: str | None = None
    data_status: DataStatus = DataStatus.VERIFIED
    raw_log: dict[str, Any] = field(default_factory=dict)
    provenance: Provenance | None = None

    @property
    def key(self) -> tuple[int, str, int]:
        return (self.chain_id, self.transaction_hash, self.log_index)

    def as_row(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "chain_id": self.chain_id,
            "dex_id": self.dex_id,
            "pool_address": self.pool_address,
            "pool_type": self.pool_type.value,
            "block_number": self.block_number,
            "block_hash": self.block_hash,
            "block_timestamp": self.block_timestamp,
            "transaction_hash": self.transaction_hash,
            "log_index": self.log_index,
            "sender": self.sender,
            "recipient": self.recipient,
            "amount0_raw": str(self.amount0_raw),
            "amount1_raw": str(self.amount1_raw),
            "sqrt_price_x96": str(self.sqrt_price_x96)
            if self.sqrt_price_x96 is not None
            else None,
            "liquidity": str(self.liquidity) if self.liquidity is not None else None,
            "tick": self.tick,
            "observed_at": self.observed_at,
            "data_status": self.data_status.value,
            "raw_log_json": None,
        }

    def as_dict(self) -> dict[str, Any]:
        payload = self.as_row()
        payload["raw_log"] = self.raw_log
        payload["provenance"] = self.provenance.as_dict() if self.provenance else None
        return payload


@dataclass(frozen=True)
class LiquidityEvent:
    """A liquidity addition or removal."""

    chain_id: int
    dex_id: str
    pool_address: str
    pool_type: PoolType
    event_type: str
    block_number: int
    transaction_hash: str
    log_index: int
    observed_at: str
    block_hash: str | None = None
    block_timestamp: int | None = None
    owner: str | None = None
    sender: str | None = None
    amount0_raw: int | None = None
    amount1_raw: int | None = None
    amount_raw: int | None = None
    liquidity: int | None = None
    tick_lower: int | None = None
    tick_upper: int | None = None
    run_id: str | None = None
    data_status: DataStatus = DataStatus.VERIFIED
    raw_log: dict[str, Any] = field(default_factory=dict)
    provenance: Provenance | None = None

    def as_row(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "chain_id": self.chain_id,
            "dex_id": self.dex_id,
            "pool_address": self.pool_address,
            "pool_type": self.pool_type.value,
            "event_type": self.event_type,
            "block_number": self.block_number,
            "block_hash": self.block_hash,
            "block_timestamp": self.block_timestamp,
            "transaction_hash": self.transaction_hash,
            "log_index": self.log_index,
            "owner": self.owner,
            "sender": self.sender,
            "amount0_raw": str(self.amount0_raw) if self.amount0_raw is not None else None,
            "amount1_raw": str(self.amount1_raw) if self.amount1_raw is not None else None,
            "amount_raw": str(self.amount_raw) if self.amount_raw is not None else None,
            "liquidity": str(self.liquidity) if self.liquidity is not None else None,
            "tick_lower": self.tick_lower,
            "tick_upper": self.tick_upper,
            "observed_at": self.observed_at,
            "data_status": self.data_status.value,
            "raw_log_json": None,
        }

    def as_dict(self) -> dict[str, Any]:
        payload = self.as_row()
        payload["raw_log"] = self.raw_log
        payload["provenance"] = self.provenance.as_dict() if self.provenance else None
        return payload


@dataclass(frozen=True)
class TransactionGasObservation:
    """**Actual** gas consumed by a historical transaction (from its receipt).

    Never confused with a gas estimate: ``source`` says ``receipt_actual`` and
    the two quantities live in separate tables.
    """

    chain_id: int
    transaction_hash: str
    block_number: int
    gas_used: int
    observed_at: str
    dex_id: str | None = None
    pool_address: str | None = None
    effective_gas_price_wei: int | None = None
    gas_cost_wei: int | None = None
    transaction_type: int | None = None
    method_selector: str | None = None
    status: int | None = None
    block_timestamp: int | None = None
    source: str = "receipt_actual"
    data_status: DataStatus = DataStatus.VERIFIED
    provenance: Provenance | None = None

    def as_row(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "dex_id": self.dex_id,
            "pool_address": self.pool_address,
            "transaction_hash": self.transaction_hash,
            "block_number": self.block_number,
            "block_timestamp": self.block_timestamp,
            "gas_used": self.gas_used,
            "effective_gas_price_wei": str(self.effective_gas_price_wei)
            if self.effective_gas_price_wei is not None
            else None,
            "gas_cost_wei": str(self.gas_cost_wei) if self.gas_cost_wei is not None else None,
            "transaction_type": self.transaction_type,
            "method_selector": self.method_selector,
            "status": self.status,
            "observed_at": self.observed_at,
            "source": self.source,
            "data_status": self.data_status.value,
        }

    def as_dict(self) -> dict[str, Any]:
        payload = self.as_row()
        payload["provenance"] = self.provenance.as_dict() if self.provenance else None
        return payload