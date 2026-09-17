"""Decimal arithmetic helpers for token quantities.

Rules enforced here:

* Raw on-chain quantities stay integers. They are never routed through a
  binary float.
* Conversions use :class:`decimal.Decimal` inside a widened context so that
  large integer amounts (27+ significant digits) survive intact.
* Token decimals are always supplied explicitly by the caller; nothing is
  guessed.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext

#: Decimal context precision used for all conversions and financial maths.
#: Wide enough for 27-digit integer supplies divided by 10**18 plus headroom
#: for intermediate products in pricing formulas.
DECIMAL_PRECISION = 80

Numeric = int | str | Decimal


def _as_decimal(value: Numeric, *, what: str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):  # bool is an int subclass; reject explicitly
        raise ValueError(f"{what} must be a number, got a bool")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value.strip())
        except InvalidOperation as exc:
            raise ValueError(f"{what} is not a decimal number: {value!r}") from exc
    raise TypeError(f"{what} must be int, str or Decimal, got {type(value).__name__}")


def to_raw(amount: Numeric, decimals: int) -> int:
    """Convert a human-readable token amount to its raw integer representation.

    ``to_raw("1.5", 18)`` returns ``1500000000000000000``. Values with more
    decimal places than the token supports raise :class:`ValueError` rather
    than being silently rounded.
    """
    if decimals < 0:
        raise ValueError("decimals must be >= 0")
    value = _as_decimal(amount, what="amount")
    with localcontext() as ctx:
        ctx.prec = DECIMAL_PRECISION
        scaled = value.scaleb(decimals)
        if scaled != scaled.to_integral_value():
            raise ValueError(
                f"amount {amount!r} has more precision than {decimals} decimals allow"
            )
        return int(scaled.to_integral_value())


def from_raw(raw: int | str, decimals: int) -> Decimal:
    """Convert a raw integer amount to a decimal quantity.

    Accepts an ``int`` or a decimal string (as stored in SQLite) and never
    converts through ``float``.
    """
    if decimals < 0:
        raise ValueError("decimals must be >= 0")
    if isinstance(raw, bool):
        raise ValueError("raw amount must be an integer, got a bool")
    if isinstance(raw, int):
        integer = raw
    elif isinstance(raw, str):
        try:
            integer = int(raw.strip())
        except ValueError as exc:
            raise ValueError(f"raw amount is not an integer string: {raw!r}") from exc
    else:
        raise TypeError(f"raw amount must be int or str, got {type(raw).__name__}")
    with localcontext() as ctx:
        ctx.prec = DECIMAL_PRECISION
        return Decimal(integer).scaleb(-decimals)


def decimal_str(value: Decimal | None, places: int = 18) -> str | None:
    """Render a decimal as a fixed-point string for storage or display.

    Fixed-point output (never scientific notation) keeps SQLite values readable
    and round-trippable. ``None`` in, ``None`` out, so unavailable data stays
    distinguishable from a real zero.
    """
    if value is None:
        return None
    with localcontext() as ctx:
        ctx.prec = DECIMAL_PRECISION
        quant = Decimal(1).scaleb(-places)
        return format(value.quantize(quant), "f")


def safe_div(numerator: Numeric, denominator: Numeric) -> Decimal | None:
    """Divide two numbers, returning ``None`` when the denominator is zero."""
    num = _as_decimal(numerator, what="numerator")
    den = _as_decimal(denominator, what="denominator")
    if den == 0:
        return None
    with localcontext() as ctx:
        ctx.prec = DECIMAL_PRECISION
        return num / den


def scale_by_power_of_ten(value: Numeric, exponent: int) -> Decimal:
    """Multiply ``value`` by ``10**exponent`` exactly."""
    with localcontext() as ctx:
        ctx.prec = DECIMAL_PRECISION
        return _as_decimal(value, what="value").scaleb(exponent)