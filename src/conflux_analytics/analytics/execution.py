"""Execution analytics engine.

Turns a raw protocol quote plus the pool state it was quoted against into an
:class:`ExecutionObservation` with a reference price, an effective price, a
size-only price impact, fee information and an optional gas estimate.

Method labels (never interchanged):

* ``quote.method`` states which mechanism produced the output amount.
* ``reference.method`` states which pool-state basis produced the reference
  price.
* ``gas.method`` states how gas was estimated.

Every produced observation carries full provenance (block, pool address, quote
source) so the result can be reproduced.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from typing import Any

from ..chain.units import DECIMAL_PRECISION, from_raw
from ..config import TradeSizeConfig
from ..errors import CapabilityUnavailableError
from ..logging_setup import get_logger
from ..models.common import DataStatus, PoolType, Provenance, QuoteMethod, utc_now_iso
from ..models.execution import (
    ExecutionObservation,
    FeeInfo,
    GasEstimate,
    PriceImpact,
    Quote,
    ReferencePrice,
    TradeSize,
)
from ..models.market import MarketState
from . import fees as fee_math
from . import gas as gas_math
from .pricing import reference_price_from_state

logger = get_logger(__name__)

#: How the reported price impact is defined. Stored with every observation.
IMPACT_METHODOLOGY = (
    "impact = 1 - fee_excluded_effective_price / reference_price, where "
    "fee_excluded_effective_price is the effective price of the quoted output "
    "rebuilt as if the protocol fee had not been taken. Protocol fees are "
    "therefore excluded from the impact and reported separately; both values "
    "use the output-per-input price convention at the same block."
)

#: Gas estimate callback: takes an unsigned transaction dict, returns estimate.
GasEstimator = Callable[[dict[str, Any]], GasEstimate]

#: Builds the unsigned transaction whose gas represents the simulated route.
GasTxBuilder = Callable[[str, str, int], dict[str, Any] | None]

#: Produces a protocol quote for one direction and size.
QuoteFunction = Callable[[str, str, int, int], Quote]


@dataclass
class DirectionAnalysis:
    """Results for one input->output direction of one pool."""

    input_token: str
    output_token: str
    observations: list[ExecutionObservation] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)

    @property
    def successful(self) -> list[ExecutionObservation]:
        return [obs for obs in self.observations if obs.success]

    def as_dict(self) -> dict[str, Any]:
        return {
            "input_token": self.input_token,
            "output_token": self.output_token,
            "observations": [obs.as_dict() for obs in self.observations],
            "skipped": self.skipped,
        }


def compute_price_impact(
    *,
    reference_price: Decimal | None,
    output_amount_raw: int | None,
    input_amount_raw: int,
    fee_rate: Decimal | None,
    input_decimals: int,
    output_decimals: int,
) -> PriceImpact:
    """Price impact per the fixed methodology in :data:`IMPACT_METHODOLOGY`.

    The effective price is ``output/input`` in whole units. The fee-excluded
    variant grosses the output back up by the protocol fee rate so that the
    impact isolates size-related deviation. Fees are never folded into the
    impact itself.
    """
    if reference_price is None or output_amount_raw is None:
        return PriceImpact(
            reference_price=reference_price,
            effective_price=None,
            fee_excluded_effective_price=None,
            impact_fraction=None,
            total_deviation_fraction=None,
            methodology=IMPACT_METHODOLOGY,
            data_status=DataStatus.UNAVAILABLE,
            detail="reference price or quote output unavailable; impact not computed",
        )

    input_amount = from_raw(input_amount_raw, input_decimals)
    output_amount = from_raw(output_amount_raw, output_decimals)
    with localcontext() as ctx:
        ctx.prec = DECIMAL_PRECISION
        effective = output_amount / input_amount if input_amount != 0 else None
        if effective is None or reference_price == 0:
            return PriceImpact(
                reference_price=reference_price,
                effective_price=effective,
                fee_excluded_effective_price=None,
                impact_fraction=None,
                total_deviation_fraction=None,
                methodology=IMPACT_METHODOLOGY,
                data_status=DataStatus.UNAVAILABLE,
                detail="degenerate division; impact not computed",
            )
        if fee_rate is not None:
            # Rebuild the execution as if no fee had been taken on the input.
            fee_excluded_effective = effective / (Decimal(1) - fee_rate)
        else:
            fee_excluded_effective = effective
        impact = Decimal(1) - fee_excluded_effective / reference_price
        total_deviation = Decimal(1) - effective / reference_price

    detail = (
        "fees excluded from impact; reported separately"
        if fee_rate is not None
        else "fee rate unavailable; fee-excluded variant equals the raw effective price"
    )
    return PriceImpact(
        reference_price=reference_price,
        effective_price=effective,
        fee_excluded_effective_price=fee_excluded_effective,
        impact_fraction=impact,
        total_deviation_fraction=total_deviation,
        methodology=IMPACT_METHODOLOGY,
        data_status=DataStatus.DERIVED,
        detail=detail,
    )


class ExecutionAnalyzer:
    """Produces execution observations for one pool."""

    def __init__(
        self,
        *,
        chain_id: int,
        dex_id: str,
        pool_address: str,
        pool_type: PoolType,
        gas_estimator: GasEstimator | None = None,
        gas_tx_builder: GasTxBuilder | None = None,
        gas_price_provider: Any = None,
    ) -> None:
        self.chain_id = chain_id
        self.dex_id = dex_id
        self.pool_address = pool_address
        self.pool_type = pool_type
        self.gas_estimator = gas_estimator
        self.gas_tx_builder = gas_tx_builder
        #: Provider used to price a protocol-reported gas figure. Optional:
        #: without it, protocol estimates are recorded as units only.
        self.gas_price_provider = gas_price_provider

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def analyze_direction(
        self,
        state: MarketState,
        quote_fn: QuoteFunction,
        trade_sizes: Sequence[TradeSizeConfig],
        input_token: str,
        output_token: str,
        *,
        run_id: str,
    ) -> DirectionAnalysis:
        """Analyze every configured trade size for one direction."""
        analysis = DirectionAnalysis(input_token=input_token, output_token=output_token)
        lowered_in = input_token.lower()
        lowered_out = output_token.lower()
        decimals = _token_decimals(state, lowered_in, lowered_out)
        if decimals is None:
            analysis.skipped.append(
                {
                    "reason": "token_decimals_unavailable",
                    "detail": "input/output decimals are unknown; no execution analysis",
                }
            )
            return analysis
        input_decimals, output_decimals = decimals

        reference = reference_price_from_state(state, input_token, output_token)
        fee_info = self._fee_info(state)
        now = utc_now_iso()

        for size_config in trade_sizes:
            try:
                raw_amount = size_config.resolve_raw(input_decimals)
            except Exception as exc:  # configuration error: recorded, not raised
                analysis.skipped.append(
                    {
                        "trade_size": size_config.label,
                        "reason": "configuration_error",
                        "detail": str(exc),
                    }
                )
                continue
            trade = TradeSize(label=size_config.label, raw_amount=raw_amount, decimals=input_decimals)
            try:
                quote = quote_fn(input_token, output_token, raw_amount, state.block_number)
            except CapabilityUnavailableError as exc:
                analysis.skipped.append(
                    {
                        "trade_size": size_config.label,
                        "reason": "capability_unavailable",
                        "detail": str(exc),
                    }
                )
                continue
            except Exception as exc:  # noqa: BLE001 - recorded as a failed quote
                quote = Quote(
                    dex_id=self.dex_id,
                    pool_address=self.pool_address,
                    input_token=lowered_in,
                    output_token=lowered_out,
                    input_amount_raw=raw_amount,
                    method=QuoteMethod.NO_SUPPORT,
                    block_number=state.block_number,
                    success=False,
                    failure_reason=str(exc),
                )
            observation = self._build_observation(
                state=state,
                quote=quote,
                trade=trade,
                reference=reference,
                fee_info=fee_info,
                input_decimals=input_decimals,
                output_decimals=output_decimals,
                run_id=run_id,
                observed_at=now,
            )
            analysis.observations.append(observation)
        return analysis

    def analyze_pool(
        self,
        state: MarketState,
        quote_fn: QuoteFunction,
        trade_sizes: Sequence[TradeSizeConfig],
        *,
        run_id: str,
    ) -> list[DirectionAnalysis]:
        """Analyze both directions of a pool (token0->token1 and token1->token0)."""
        return [
            self.analyze_direction(
                state, quote_fn, trade_sizes, state.token0.address, state.token1.address, run_id=run_id
            ),
            self.analyze_direction(
                state, quote_fn, trade_sizes, state.token1.address, state.token0.address, run_id=run_id
            ),
        ]

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _fee_info(self, state: MarketState) -> FeeInfo:
        if state.fee_bps is None:
            return FeeInfo(
                source="pool_state",
                data_status=DataStatus.UNAVAILABLE,
                detail="pool fee not observed on chain",
            )
        return fee_math.fee_info_from_bps("pool_state", state.fee_bps)

    def _build_observation(
        self,
        *,
        state: MarketState,
        quote: Quote,
        trade: TradeSize,
        reference: ReferencePrice,
        fee_info: FeeInfo,
        input_decimals: int,
        output_decimals: int,
        run_id: str,
        observed_at: str,
    ) -> ExecutionObservation:
        _ = trade  # label carried by the caller; amount comes from the quote
        output_amount: Decimal | None = None
        effective_price: Decimal | None = None
        if quote.is_routable and quote.output_amount_raw is not None:
            output_amount = from_raw(quote.output_amount_raw, output_decimals)
            input_amount_dec = from_raw(quote.input_amount_raw, input_decimals)
            with localcontext() as ctx:
                ctx.prec = DECIMAL_PRECISION
                if input_amount_dec != 0:
                    effective_price = output_amount / input_amount_dec

        impact = compute_price_impact(
            reference_price=reference.price,
            output_amount_raw=quote.output_amount_raw if quote.is_routable else None,
            input_amount_raw=quote.input_amount_raw,
            fee_rate=fee_info.fee_rate,
            input_decimals=input_decimals,
            output_decimals=output_decimals,
        )

        fee = fee_info
        if quote.is_routable and fee_info.fee_rate is not None:
            fee_raw = fee_math.fee_amount_on_input(quote.input_amount_raw, fee_info.fee_rate)
            fee = FeeInfo(
                source=fee_info.source,
                fee_bps=fee_info.fee_bps,
                fee_rate=fee_info.fee_rate,
                fee_amount_raw=fee_raw,
                fee_amount=from_raw(fee_raw, input_decimals),
                basis="input_amount",
                data_status=fee_info.data_status,
                detail=fee_info.detail,
            )

        gas = (
            self._estimate_gas(quote)
            if quote.is_routable
            else _gas_skipped("quote not routable")
        )
        gas = self._fallback_protocol_gas(quote, gas)

        success = quote.is_routable and reference.price is not None
        failure_reason = None
        if not quote.is_routable:
            failure_reason = quote.failure_reason or "quote produced no output"
        elif reference.price is None:
            failure_reason = f"reference price unavailable: {reference.detail}"

        input_amount_dec = from_raw(quote.input_amount_raw, input_decimals)
        notes = self._notes(reference, fee, gas)
        provenance = Provenance(
            source="analytics_engine",
            method=f"execution_analysis:{quote.method.value}",
            chain_id=self.chain_id,
            block_number=state.block_number,
            block_hash=state.block_hash,
            contract_address=self.pool_address,
            observed_at=observed_at,
            data_status=quote.data_status,
            detail=notes,
            sources=[quote.source, reference.method],
        )
        return ExecutionObservation(
            run_id=run_id,
            chain_id=self.chain_id,
            dex_id=self.dex_id,
            pool_address=self.pool_address,
            pool_type=self.pool_type,
            input_token=quote.input_token,
            output_token=quote.output_token,
            trade_size_label=trade.label,
            input_amount_raw=quote.input_amount_raw,
            input_amount=input_amount_dec,
            block_number=state.block_number,
            observed_at=observed_at,
            quote=quote,
            reference=reference,
            impact=impact,
            fee=fee,
            gas=gas,
            output_amount_raw=quote.output_amount_raw if quote.is_routable else None,
            output_amount=output_amount,
            effective_price=effective_price,
            success=success,
            failure_reason=failure_reason,
            block_hash=state.block_hash,
            notes=notes,
            provenance=provenance,
        )

    def _estimate_gas(self, quote: Quote) -> GasEstimate:
        if self.gas_estimator is None or self.gas_tx_builder is None:
            return _gas_skipped("no gas estimator configured for this route")
        try:
            transaction = self.gas_tx_builder(
                quote.input_token, quote.output_token, quote.input_amount_raw
            )
        except Exception as exc:  # noqa: BLE001 - recorded, never raised past analytics
            return GasEstimate(
                method="unavailable",
                source="analytics_engine",
                data_status=DataStatus.UNAVAILABLE,
                failure_reason=f"transaction construction failed: {exc}",
            )
        if transaction is None:
            return _gas_skipped("this route has no estimable transaction shape")
        return self.gas_estimator(transaction)

    def _fallback_protocol_gas(self, quote: Quote, gas: GasEstimate) -> GasEstimate:
        """Use a protocol-reported gas figure when no RPC estimate exists.

        Router ``eth_estimateGas`` reverts for swaps that no account can
        actually execute (no allowance or balance), so a DEX that returns its
        own gas estimate through its quoter is the only available source. The
        result keeps a distinct method and source label.
        """
        if gas.data_status is not DataStatus.UNAVAILABLE:
            return gas
        if self.gas_price_provider is None or not quote.raw_response:
            return gas
        reported = quote.raw_response.get("gasEstimate")
        if not isinstance(reported, int) or reported <= 0:
            return gas
        return gas_math.protocol_gas_estimate(
            self.gas_price_provider,
            reported,
            source=f"{self.dex_id}:quoter",
        )

    @staticmethod
    def _notes(reference: ReferencePrice, fee: FeeInfo, gas: GasEstimate) -> str | None:
        parts: list[str] = []
        if reference.price is None:
            parts.append(f"reference unavailable: {reference.detail}")
        if fee.data_status is DataStatus.UNAVAILABLE:
            parts.append(f"fee unavailable: {fee.detail}")
        if gas.data_status is DataStatus.UNAVAILABLE:
            parts.append(f"gas unavailable: {gas.failure_reason}")
        return "; ".join(parts) if parts else None


def _gas_skipped(reason: str) -> GasEstimate:
    return GasEstimate(
        method="unavailable",
        source="analytics_engine",
        data_status=DataStatus.UNAVAILABLE,
        failure_reason=reason,
    )


def _token_decimals(
    state: MarketState, input_token: str, output_token: str
) -> tuple[int, int] | None:
    tokens = {
        state.token0.address: state.token0.decimals,
        state.token1.address: state.token1.decimals,
    }
    in_dec = tokens.get(input_token)
    out_dec = tokens.get(output_token)
    if in_dec is None or out_dec is None:
        return None
    return in_dec, out_dec


__all__ = [
    "DirectionAnalysis",
    "ExecutionAnalyzer",
    "GasEstimator",
    "GasTxBuilder",
    "IMPACT_METHODOLOGY",
    "QuoteFunction",
    "compute_price_impact",
]
