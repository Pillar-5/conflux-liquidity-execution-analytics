
"""Liquidity metrics.

Only metrics that the underlying pool model supports are calculated:

* constant-product pools: token reserves, reserve imbalance, quote-side
  liquidity for a direction.
* concentrated-liquidity pools: active (in-range) liquidity and the spot price
  derived from ``sqrtPriceX96``; the full position inventory is *not* assumed
  from active liquidity and is reported as unavailable instead.

USD values are only produced when a caller supplies an explicit, documented
price source (see :class:`UsdPriceSource`); this module never invents one.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Any

from ..chain.units import DECIMAL_PRECISION, decimal_str
from ..models.common import DataStatus, PoolType
from ..models.market import MarketState

IMBALANCE_METHOD = "reserve_imbalance_abs_normalized"


@dataclass(frozen=True)
class UsdPriceSource:
    """An explicitly configured USD price for one token address.

    ``label`` documents where the price came from (e.g. a configured peg or an
    external API). USD liquidity is only computed when a price exists for the
    requested token; otherwise the field is ``None`` and the status is
    ``unavailable``.
    """

    token_address: str
    usd_price: Decimal
    label: str
    data_status: DataStatus = DataStatus.DERIVED


@dataclass(frozen=True)
class LiquidityMetrics:
    """Liquidity metrics for one pool state at one block."""

    chain_id: int
    dex_id: str
    pool_address: str
    pool_type: PoolType
    block_number: int
    observed_at: str
    reserve0: Decimal | None = None
    reserve1: Decimal | None = None
    reserve0_usd: Decimal | None = None
    reserve1_usd: Decimal | None = None
    reserve_usd_total: Decimal | None = None
    usd_price_source: str | None = None
    imbalance_fraction: Decimal | None = None
    active_liquidity: int | None = None
    data_status: DataStatus = DataStatus.DERIVED
    detail: str | None = None
    usd_detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "dex_id": self.dex_id,
            "pool_address": self.pool_address,
            "pool_type": self.pool_type.value,
            "block_number": self.block_number,
            "observed_at": self.observed_at,
            "reserve0": decimal_str(self.reserve0),
            "reserve1": decimal_str(self.reserve1),
            "reserve0_usd": decimal_str(self.reserve0_usd),
            "reserve1_usd": decimal_str(self.reserve1_usd),
            "reserve_usd_total": decimal_str(self.reserve_usd_total),
            "usd_price_source": self.usd_price_source,
            "imbalance_fraction": decimal_str(self.imbalance_fraction, 12)
            if self.imbalance_fraction is not None
            else None,
            "active_liquidity": str(self.active_liquidity)
            if self.active_liquidity is not None
            else None,
            "data_status": self.data_status.value,
            "detail": self.detail,
            "usd_detail": self.usd_detail,
        }


def liquidity_metrics(
    state: MarketState,
    usd_prices: dict[str, UsdPriceSource] | None = None,
    run_id: str | None = None,
) -> LiquidityMetrics:
    """Compute liquidity metrics for ``state``.

    ``usd_prices`` maps token addresses to explicitly configured USD prices.
    Missing prices leave USD fields as ``None`` with ``usd_detail`` explaining
    why; they are never filled with an assumption.
    """
    _ = run_id  # provenance is carried by the pool state itself
    prices = usd_prices or {}
    reserve0 = state.reserve_decimal(state.token0.address)
    reserve1 = state.reserve_decimal(state.token1.address)
    has_reserves = state.reserve0_raw is not None and state.reserve1_raw is not None

    reserve0_usd = _usd_value(state.token0.address, reserve0, prices)
    reserve1_usd = _usd_value(state.token1.address, reserve1, prices)
    total_usd: Decimal | None = None
    usd_detail: str | None = None
    if reserve0_usd is not None and reserve1_usd is not None:
        with localcontext() as ctx:
            ctx.prec = DECIMAL_PRECISION
            total_usd = reserve0_usd + reserve1_usd
    elif not prices:
        usd_detail = "no USD price sources configured; USD liquidity not computed"
    else:
        missing = [
            addr
            for addr, value in (
                (state.token0.address, reserve0_usd),
                (state.token1.address, reserve1_usd),
            )
            if value is None
        ]
        usd_detail = f"no verified USD price for {', '.join(missing)}"

    imbalance = _imbalance(reserve0, reserve1)
    active_liquidity = state.liquidity if state.is_concentrated_liquidity else None

    if state.data_status in (DataStatus.ERROR, DataStatus.UNAVAILABLE):
        status = DataStatus.UNAVAILABLE
        detail = state.detail or "pool state unavailable; liquidity metrics not computed"
    elif has_reserves or active_liquidity is not None:
        status = DataStatus.DERIVED
        detail = None
    else:
        status = DataStatus.PARTIAL
        detail = "pool state observed but contains no liquidity fields for this model"

    return LiquidityMetrics(
        chain_id=state.chain_id,
        dex_id=state.dex_id,
        pool_address=state.pool_address,
        pool_type=state.pool_type,
        block_number=state.block_number,
        observed_at=state.observed_at,
        reserve0=reserve0,
        reserve1=reserve1,
        reserve0_usd=reserve0_usd,
        reserve1_usd=reserve1_usd,
        reserve_usd_total=total_usd,
        usd_price_source=_usd_source_label(prices) if total_usd is not None else None,
        imbalance_fraction=imbalance,
        active_liquidity=active_liquidity,
        data_status=status,
        detail=detail,
        usd_detail=usd_detail,
    )


def quote_side_liquidity(state: MarketState, token_in: str) -> Decimal | None:
    """Decimal reserve of the input token for a direction, or ``None``."""
    return state.reserve_decimal(token_in)


def _usd_value(
    token_address: str,
    amount: Decimal | None,
    prices: dict[str, UsdPriceSource],
) -> Decimal | None:
    if amount is None:
        return None
    source = prices.get(token_address.lower())
    if source is None:
        return None
    with localcontext() as ctx:
        ctx.prec = DECIMAL_PRECISION
        return amount * source.usd_price


def _usd_source_label(prices: dict[str, UsdPriceSource]) -> str | None:
    labels = sorted({source.label for source in prices.values()})
    return ",".join(labels) if labels else None


def _imbalance(reserve0: Decimal | None, reserve1: Decimal | None) -> Decimal | None:
    """``|r0 - r1| / (r0 + r1)`` for two-sided pools; ``None`` otherwise."""
    if reserve0 is None or reserve1 is None:
        return None
    total = reserve0 + reserve1
    if total == 0:
        return None
    with localcontext() as ctx:
        ctx.prec = DECIMAL_PRECISION
        return abs(reserve0 - reserve1) / total


__all__ = [
    "IMBALANCE_METHOD",
    "LiquidityMetrics",
    "UsdPriceSource",
    "liquidity_metrics",
    "quote_side_liquidity",
]
