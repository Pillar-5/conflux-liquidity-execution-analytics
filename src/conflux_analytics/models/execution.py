"""Execution analytics result models.

Reporting convention (fixed and consistent across the project)
-------------------------------------------------------------
* A price always means **units of the output token per one whole unit of the
  input token**.
* ``effective_price`` = ``output_amount / input_amount`` for the quoted trade.
* ``reference_price`` = the pool's spot price at the same block, computed from
  pool state (see :mod:`conflux_analytics.analytics.pricing`).
* ``price_impact`` = ``1 - fee_excluded_effective_price / reference_price``.
  Protocol fees are removed from the execution before the comparison, so the
  impact measures size-related deviation only. Fees are reported separately in
  :class:`FeeInfo`.
* ``total_deviation_incl_fees`` reports ``1 - effective_price / reference_price``
  and is labelled as including fees. The two numbers are never mixed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ..chain.units import decimal_str, from_raw
from .common import DataStatus, PoolType, Provenance, QuoteMethod


@dataclass(frozen=True)
class TradeSize:
    """A configured trade size resolved for one input token."""

    label: str
    raw_amount: int
    decimals: int

    @property
    def amount(self) -> Decimal:
        return from_raw(self.raw_amount, self.decimals)

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "raw_amount": str(self.raw_amount),
            "decimals": self.decimals,
            "amount": decimal_str(self.amount),
        }


@dataclass(frozen=True)
class FeeInfo:
    """Fee information for one pool or one quoted trade."""

    source: str
    fee_bps: int | None = None
    fee_rate: Decimal | None = None
    fee_amount_raw: int | None = None
    fee_amount: Decimal | None = None
    basis: str | None = None
    data_status: DataStatus = DataStatus.VERIFIED
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "fee_bps": self.fee_bps,
            "fee_rate": decimal_str(self.fee_rate, 12),
            "fee_amount_raw": str(self.fee_amount_raw)
            if self.fee_amount_raw is not None
            else None,
            "fee_amount": decimal_str(self.fee_amount),
            "basis": self.basis,
            "data_status": self.data_status.value,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Quote:
    """The raw outcome of asking a protocol for an output amount."""

    dex_id: str
    pool_address: str
    input_token: str
    output_token: str
    input_amount_raw: int
    method: QuoteMethod
    block_number: int
    output_amount_raw: int | None = None
    success: bool = False
    failure_reason: str | None = None
    source: str = "rpc_eth_call"
    data_status: DataStatus = DataStatus.SIMULATED
    detail: str | None = None
    # Protocol-reported extras, when the interface returns them.
    gas_estimate_units: int | None = None
    gas_estimate_source: str | None = None
    sqrt_price_x96_after: int | None = None
    initialized_ticks_crossed: int | None = None
    raw_response: dict[str, Any] = field(default_factory=dict)

    @property
    def is_routable(self) -> bool:
        return self.success and self.output_amount_raw is not None and self.output_amount_raw > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "dex_id": self.dex_id,
            "pool_address": self.pool_address,
            "input_token": self.input_token,
            "output_token": self.output_token,
            "input_amount_raw": str(self.input_amount_raw),
            "output_amount_raw": str(self.output_amount_raw)
            if self.output_amount_raw is not None
            else None,
            "method": self.method.value,
            "block_number": self.block_number,
            "success": self.success,
            "failure_reason": self.failure_reason,
            "source": self.source,
            "data_status": self.data_status.value,
            "detail": self.detail,
            "gas_estimate_units": self.gas_estimate_units,
            "gas_estimate_source": self.gas_estimate_source,
            "sqrt_price_x96_after": str(self.sqrt_price_x96_after)
            if self.sqrt_price_x96_after is not None
            else None,
            "initialized_ticks_crossed": self.initialized_ticks_crossed,
            "raw_response": self.raw_response,
        }


@dataclass(frozen=True)
class ReferencePrice:
    """Spot/reference price used as the execution comparison baseline."""

    price: Decimal | None
    convention: str
    method: str
    data_status: DataStatus
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "price": decimal_str(self.price),
            "convention": self.convention,
            "method": self.method,
            "data_status": self.data_status.value,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class PriceImpact:
    """Size-related execution deviation, reported separately from fees."""

    reference_price: Decimal | None
    effective_price: Decimal | None
    fee_excluded_effective_price: Decimal | None
    impact_fraction: Decimal | None
    total_deviation_fraction: Decimal | None
    methodology: str
    data_status: DataStatus
    detail: str | None = None

    @property
    def impact_bps(self) -> Decimal | None:
        if self.impact_fraction is None:
            return None
        return self.impact_fraction * Decimal(10000)

    @property
    def total_deviation_bps(self) -> Decimal | None:
        if self.total_deviation_fraction is None:
            return None
        return self.total_deviation_fraction * Decimal(10000)

    def as_dict(self) -> dict[str, Any]:
        return {
            "reference_price": decimal_str(self.reference_price),
            "effective_price": decimal_str(self.effective_price),
            "fee_excluded_effective_price": decimal_str(self.fee_excluded_effective_price),
            "impact_fraction": decimal_str(self.impact_fraction, 12),
            "impact_bps": decimal_str(self.impact_bps, 6),
            "total_deviation_fraction": decimal_str(self.total_deviation_fraction, 12),
            "total_deviation_incl_fees_bps": decimal_str(self.total_deviation_bps, 6),
            "methodology": self.methodology,
            "data_status": self.data_status.value,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class GasEstimate:
    """A gas estimate for a route that was **not** broadcast.

    Every field here is an estimate. Actual gas consumed by historical
    transactions is stored separately (``gas_observations``) and is never mixed
    with this.
    """

    method: str
    source: str
    data_status: DataStatus = DataStatus.ESTIMATED
    gas_units: int | None = None
    gas_price_wei: int | None = None
    max_fee_per_gas_wei: int | None = None
    gas_cost_native_wei: int | None = None
    native_symbol: str = "CFX"
    failure_reason: str | None = None
    detail: str | None = None

    @property
    def gas_cost_native(self) -> Decimal | None:
        if self.gas_cost_native_wei is None:
            return None
        return from_raw(self.gas_cost_native_wei, 18)

    def as_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "source": self.source,
            "data_status": self.data_status.value,
            "gas_units": self.gas_units,
            "gas_price_wei": str(self.gas_price_wei) if self.gas_price_wei is not None else None,
            "max_fee_per_gas_wei": str(self.max_fee_per_gas_wei)
            if self.max_fee_per_gas_wei is not None
            else None,
            "gas_cost_native_wei": str(self.gas_cost_native_wei)
            if self.gas_cost_native_wei is not None
            else None,
            "gas_cost_native": decimal_str(self.gas_cost_native),
            "native_symbol": self.native_symbol,
            "failure_reason": self.failure_reason,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ExecutionObservation:
    """A complete execution-quality observation for one trade size.

    This is the record the API and reports read. Provenance is mandatory: an
    observation without a block number and a quote method cannot be reproduced
    and is therefore not produced.
    """

    run_id: str
    chain_id: int
    dex_id: str
    pool_address: str
    pool_type: PoolType
    input_token: str
    output_token: str
    trade_size_label: str
    input_amount_raw: int
    input_amount: Decimal | None
    block_number: int
    observed_at: str
    quote: Quote
    reference: ReferencePrice
    impact: PriceImpact
    fee: FeeInfo
    gas: GasEstimate
    output_amount_raw: int | None = None
    output_amount: Decimal | None = None
    effective_price: Decimal | None = None
    success: bool = False
    failure_reason: str | None = None
    block_hash: str | None = None
    notes: str | None = None
    provenance: Provenance | None = None

    def as_row(self) -> dict[str, Any]:
        """Flatten to a row for the ``execution_observations`` table."""
        return {
            "run_id": self.run_id,
            "chain_id": self.chain_id,
            "dex_id": self.dex_id,
            "pool_address": self.pool_address,
            "pool_type": self.pool_type.value,
            "input_token": self.input_token,
            "output_token": self.output_token,
            "trade_size": self.trade_size_label,
            "input_amount_raw": str(self.input_amount_raw),
            "input_amount_dec": decimal_str(self.input_amount),
            "output_amount_raw": str(self.output_amount_raw)
            if self.output_amount_raw is not None
            else None,
            "output_amount_dec": decimal_str(self.output_amount),
            "reference_price": decimal_str(self.reference.price),
            "reference_price_method": self.reference.method,
            "reference_price_status": self.reference.data_status.value,
            "effective_price": decimal_str(self.effective_price),
            "fee_excluded_effective_price": decimal_str(self.impact.fee_excluded_effective_price),
            "price_impact_bps": decimal_str(self.impact.impact_bps, 6),
            "total_deviation_bps": decimal_str(self.impact.total_deviation_bps, 6),
            "impact_methodology": self.impact.methodology,
            "fee_bps": self.fee.fee_bps,
            "fee_rate": decimal_str(self.fee.fee_rate, 12),
            "fee_amount_raw": str(self.fee.fee_amount_raw)
            if self.fee.fee_amount_raw is not None
            else None,
            "fee_amount_dec": decimal_str(self.fee.fee_amount),
            "fee_source": self.fee.source,
            "gas_units": self.gas.gas_units,
            "gas_price_wei": str(self.gas.gas_price_wei)
            if self.gas.gas_price_wei is not None
            else None,
            "gas_cost_native_wei": str(self.gas.gas_cost_native_wei)
            if self.gas.gas_cost_native_wei is not None
            else None,
            "gas_cost_native_dec": decimal_str(self.gas.gas_cost_native),
            "gas_method": self.gas.method,
            "gas_source": self.gas.source,
            "gas_status": self.gas.data_status.value,
            "gas_failure_reason": self.gas.failure_reason,
            "quote_method": self.quote.method.value,
            "quote_source": self.quote.source,
            "quote_status": self.quote.data_status.value,
            "block_number": self.block_number,
            "block_hash": self.block_hash,
            "observed_at": self.observed_at,
            "success": 1 if self.success else 0,
            "failure_reason": self.failure_reason,
            "notes": self.notes,
            "data_status": self.quote.data_status.value,
            "provenance_json": self.provenance.to_json() if self.provenance else None,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "chain_id": self.chain_id,
            "dex_id": self.dex_id,
            "pool_address": self.pool_address,
            "pool_type": self.pool_type.value,
            "input_token": self.input_token,
            "output_token": self.output_token,
            "trade_size": self.trade_size_label,
            "input_amount_raw": str(self.input_amount_raw),
            "input_amount": decimal_str(self.input_amount),
            "output_amount_raw": str(self.output_amount_raw)
            if self.output_amount_raw is not None
            else None,
            "output_amount": decimal_str(self.output_amount),
            "block_number": self.block_number,
            "block_hash": self.block_hash,
            "observed_at": self.observed_at,
            "success": self.success,
            "failure_reason": self.failure_reason,
            "notes": self.notes,
            "quote": self.quote.as_dict(),
            "reference_price": self.reference.as_dict(),
            "price_impact": self.impact.as_dict(),
            "effective_price": decimal_str(self.effective_price),
            "fee": self.fee.as_dict(),
            "gas": self.gas.as_dict(),
            "provenance": self.provenance.as_dict() if self.provenance else None,
        }