"""Reference price calculation.

Convention (fixed across the project): a price is *units of the output token
per one whole unit of the input token*.

* For a constant-product pool the spot price is ``reserve_out / reserve_in``
  (whole-token units). This is the marginal price at zero trade size, which is
  the standard AMM reference price.
* For a concentrated-liquidity pool the spot price is derived from the pool's
  ``sqrtPriceX96``: ``price = (sqrtPrice / 2^96)^2`` gives token1 per token0 in
  whole units, which is then inverted for the other direction. This follows the
  Uniswap V3 whitepaper section 6.1 and is documented in
  docs/analytics-methodology.md.

No price is invented: when the required state is missing the result is
``None`` and the caller records ``unavailable``.
"""

from __future__ import annotations

from decimal import Decimal, localcontext

from ..chain.units import DECIMAL_PRECISION, decimal_str, safe_div
from ..models.common import DataStatus
from ..models.execution import ReferencePrice
from ..models.market import MarketState

#: Human-readable convention string attached to every reference price.
PRICE_CONVENTION = "output_token_units_per_input_token_unit"

CP_METHOD = "constant_product_reserves"
CL_METHOD = "concentrated_liquidity_sqrt_price_x96"


def _reference(status: DataStatus, method: str, detail: str) -> ReferencePrice:
    return ReferencePrice(
        price=None,
        convention=PRICE_CONVENTION,
        method=method,
        data_status=status,
        detail=detail,
    )


def reference_price_from_state(state: MarketState, token_in: str, token_out: str) -> ReferencePrice:
    """Compute the spot reference price for a trade through ``state``.

    Returns a :class:`ReferencePrice` whose ``price`` is ``None`` (with an
    explicit ``data_status``) whenever the pool state does not support the
    calculation. Nothing is estimated here.
    """
    if token_in.lower() not in (state.token0.address, state.token1.address):
        return _reference(
            DataStatus.UNAVAILABLE, "not_applicable", f"{token_in} is not a token of this pool"
        )
    if token_out.lower() not in (state.token0.address, state.token1.address):
        return _reference(
            DataStatus.UNAVAILABLE, "not_applicable", f"{token_out} is not a token of this pool"
        )
    if token_in.lower() == token_out.lower():
        return _reference(DataStatus.UNAVAILABLE, "not_applicable", "input equals output token")

    if state.is_constant_product:
        return _cp_reference(state, token_in, token_out)
    if state.is_concentrated_liquidity:
        return _cl_reference(state, token_in, token_out)
    return _reference(
        DataStatus.UNAVAILABLE, "not_applicable", f"unsupported pool type {state.pool_type.value}"
    )


def _cp_reference(state: MarketState, token_in: str, token_out: str) -> ReferencePrice:
    """Spot price from reserves: ``reserve_out / reserve_in``."""
    if not state.has_decimals:
        return _reference(
            DataStatus.UNAVAILABLE, CP_METHOD, "token decimals are unknown; price not computed"
        )
    reserve_in = state.reserve_decimal(token_in)
    reserve_out = state.reserve_decimal(token_out)
    if reserve_in is None or reserve_out is None:
        return _reference(
            DataStatus.UNAVAILABLE, CP_METHOD, "pool reserves were not observed"
        )
    if reserve_in <= 0:
        return _reference(DataStatus.UNAVAILABLE, CP_METHOD, "input reserve is zero")
    price = safe_div(reserve_out, reserve_in)
    if price is None:
        return _reference(DataStatus.UNAVAILABLE, CP_METHOD, "output reserve is zero")
    return ReferencePrice(
        price=price,
        convention=PRICE_CONVENTION,
        method=CP_METHOD,
        data_status=DataStatus.DERIVED,
        detail=f"reserve_out={decimal_str(reserve_out)} reserve_in={decimal_str(reserve_in)}",
    )


def _cl_reference(state: MarketState, token_in: str, token_out: str) -> ReferencePrice:
    """Spot price from ``sqrtPriceX96`` per the Uniswap V3 formula."""
    if state.sqrt_price_x96 is None:
        return _reference(
            DataStatus.UNAVAILABLE, CL_METHOD, "sqrtPriceX96 was not observed for this pool"
        )
    if not state.has_decimals:
        return _reference(
            DataStatus.UNAVAILABLE, CL_METHOD, "token decimals are unknown; price not computed"
        )
    assert state.token0.decimals is not None and state.token1.decimals is not None
    decimal_shift = state.token0.decimals - state.token1.decimals
    with localcontext() as ctx:
        ctx.prec = DECIMAL_PRECISION
        ratio = Decimal(state.sqrt_price_x96) / Decimal(1 << 96)
        price_t1_per_t0 = (ratio * ratio).scaleb(decimal_shift)
    if token_in.lower() == state.token0.address:
        price = price_t1_per_t0
    else:
        if price_t1_per_t0 == 0:
            return _reference(DataStatus.UNAVAILABLE, CL_METHOD, "sqrt price is zero")
        price = Decimal(1) / price_t1_per_t0
    return ReferencePrice(
        price=price,
        convention=PRICE_CONVENTION,
        method=CL_METHOD,
        data_status=DataStatus.DERIVED,
        detail=(
            "computed from sqrtPriceX96 per the Uniswap V3 formula; "
            f"sqrtPriceX96={state.sqrt_price_x96}"
        ),
    )


def price_source_label(state: MarketState) -> str:
    """A short provenance label for the price basis of one pool state."""
    if state.is_concentrated_liquidity:
        return "dex_pool_state:sqrtPriceX96"
    return "dex_pool_state:reserves"


def sqrt_price_x96_to_price(
    sqrt_price_x96: int, decimals0: int, decimals1: int
) -> Decimal:
    """Convert a ``sqrtPriceX96`` value into token1-per-token0 whole-unit price.

    ``price = (sqrtPriceX96 / 2^96)^2 * 10^(decimals0 - decimals1)``.

    This is the Uniswap-V3 spot price formula (whitepaper §6.1). It is exposed
    separately for tests and for direct conversions from raw pool state.
    """
    with localcontext() as ctx:
        ctx.prec = DECIMAL_PRECISION
        ratio = Decimal(sqrt_price_x96) / Decimal(1 << 96)
        return (ratio * ratio).scaleb(decimals0 - decimals1)


__all__ = [
    "CL_METHOD",
    "CP_METHOD",
    "PRICE_CONVENTION",
    "price_source_label",
    "reference_price_from_state",
    "sqrt_price_x96_to_price",
]
