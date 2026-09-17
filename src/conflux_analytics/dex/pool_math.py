"""Deterministic pool mathematics for quotation without a protocol call.

The module implements the constant-product AMM trade function used by
Uniswap-V2-style pools (which is the model Swappi V2 pools follow). All
arithmetic is integer-first: inputs and outputs are raw integer token
amounts, and the intermediate multiplication uses a wide Decimal context so
that 27+ digit values are never truncated through binary floating point.

Quotes produced here are labelled ``pool_math``: they are deterministic
calculations from verified pool state, **not** router quotes or contract
simulations, and the analytics layer records them as such.
"""

from __future__ import annotations

from decimal import Decimal, localcontext

from ..chain.units import DECIMAL_PRECISION

#: Uniswap-V2-style fee numerator/denominator (0.30%) expressed exactly.
DEFAULT_FEE_NUMERATOR = 997
DEFAULT_FEE_DENOMINATOR = 1000


class ConstantProductMath:
    """Constant-product trade function for one fee tier."""

    def __init__(self, fee_bps: int) -> None:
        if fee_bps < 0 or fee_bps > 10_000:
            raise ValueError(f"fee_bps must be within [0, 10000], got {fee_bps}")
        self.fee_bps = fee_bps

    @property
    def fee_numerator(self) -> int:
        return 10_000 - self.fee_bps

    @property
    def fee_denominator(self) -> int:
        return 10_000

    def amount_out(
        self, input_amount_raw: int, reserve_in_raw: int, reserve_out_raw: int
    ) -> int:
        """Return the raw output of a swap under ``x*y = k`` with fee applied.

        Uses the exact Uniswap-V2 formula with the fee expressed in basis
        points (``fee_bps=30`` keeps 9970/10000 of the trade):

            amount_out = (r_out * amount_in * fee_num) /
                         (r_in * fee_den + amount_in * fee_num)

        Raises :class:`ValueError` for zero or negative inputs so callers can
        record a failure instead of producing a fabricated quote.
        """
        if input_amount_raw <= 0:
            raise ValueError("input_amount_raw must be positive")
        if reserve_in_raw <= 0 or reserve_out_raw <= 0:
            raise ValueError("reserves must be positive to quote a swap")
        num = Decimal(self.fee_numerator) * Decimal(input_amount_raw) * Decimal(reserve_out_raw)
        den = (
            Decimal(self.fee_denominator) * Decimal(reserve_in_raw)
            + Decimal(self.fee_numerator) * Decimal(input_amount_raw)
        )
        with localcontext() as ctx:
            ctx.prec = DECIMAL_PRECISION
            return int((num / den).to_integral_value(rounding="ROUND_FLOOR"))

    def spot_price(self, *, reserve_in_raw: int, reserve_out_raw: int) -> Decimal:
        """Marginal price: raw output units per raw input unit."""
        if reserve_in_raw <= 0:
            raise ValueError("reserve_in_raw must be positive")
        with localcontext() as ctx:
            ctx.prec = DECIMAL_PRECISION
            return Decimal(reserve_out_raw) / Decimal(reserve_in_raw)


__all__ = ["ConstantProductMath", "DEFAULT_FEE_DENOMINATOR", "DEFAULT_FEE_NUMERATOR"]
