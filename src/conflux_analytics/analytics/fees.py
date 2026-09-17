"""Fee handling.

Fees are always reported separately from price impact. All conversions between
basis points, fee numerators/denominators and decimal fee rates are exact
:class:`decimal.Decimal` operations.

A protocol fee taken on the input amount is removed from the input before an
execution price is compared against the reference price. That fee-excluded
execution is what the price-impact methodology measures; the fee itself is
carried by :class:`~conflux_analytics.models.execution.FeeInfo`.
"""

from __future__ import annotations

from decimal import Decimal, localcontext

from ..chain.units import DECIMAL_PRECISION
from ..models.common import DataStatus
from ..models.execution import FeeInfo

#: Source label used when the fee rate comes from the adapter configuration,
#: which itself is documented against the protocol's verified contracts.
CONFIG_SOURCE = "dex_configuration"


def bps_to_rate(fee_bps: int) -> Decimal:
    """Convert basis points to a fraction: ``30 bps -> 0.003``."""
    if fee_bps < 0:
        raise ValueError("fee_bps must be non-negative")
    with localcontext() as ctx:
        ctx.prec = DECIMAL_PRECISION
        return Decimal(fee_bps) / Decimal(10000)


def parts_to_rate(numerator: int, denominator: int) -> Decimal:
    """Convert a fee fraction such as Uniswap V3's ``fee = numerator / 1e6``."""
    if denominator <= 0:
        raise ValueError("fee denominator must be positive")
    if numerator < 0:
        raise ValueError("fee numerator must be non-negative")
    with localcontext() as ctx:
        ctx.prec = DECIMAL_PRECISION
        return Decimal(numerator) / Decimal(denominator)


def rate_to_bps(rate: Decimal) -> int:
    """Convert a decimal fee rate to whole basis points (rounded down).

    Raises :class:`ValueError` when the rate is negative, which keeps a
    malformed fee value from silently entering the pipeline.
    """
    if rate < 0:
        raise ValueError("fee rate must be non-negative")
    with localcontext() as ctx:
        ctx.prec = DECIMAL_PRECISION
        return int((rate * Decimal(10000)).to_integral_value())


def fee_amount_on_input(input_amount_raw: int, fee_rate: Decimal) -> int:
    """Raw fee amount taken from the input amount, rounded down (floor).

    Floor matches the rounding direction used by Uniswap V2-style ``getAmountIn``
    (which rounds up the required input, making the fee component at least the
    exact rate) and keeps the retained amount conservative.
    """
    if input_amount_raw < 0:
        raise ValueError("input_amount_raw must be non-negative")
    with localcontext() as ctx:
        ctx.prec = DECIMAL_PRECISION
        return int((Decimal(input_amount_raw) * fee_rate).to_integral_value(rounding="ROUND_FLOOR"))


def input_after_fee(input_amount_raw: int, fee_rate: Decimal) -> int:
    """Raw input amount that actually reaches the pool after the protocol fee."""
    return input_amount_raw - fee_amount_on_input(input_amount_raw, fee_rate)


def fee_info_from_bps(source: str, fee_bps: int | None) -> FeeInfo:
    """Build a :class:`FeeInfo` from a basis-point value read on chain."""
    if fee_bps is None:
        return FeeInfo(
            source=source, data_status=DataStatus.UNAVAILABLE, detail="fee_bps not observed"
        )
    rate = bps_to_rate(fee_bps)
    return FeeInfo(source=source, fee_bps=fee_bps, fee_rate=rate, basis="input_amount")


__all__ = [
    "CONFIG_SOURCE",
    "bps_to_rate",
    "fee_amount_on_input",
    "fee_info_from_bps",
    "input_after_fee",
    "parts_to_rate",
    "rate_to_bps",
]
