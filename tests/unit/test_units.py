"""Unit tests for decimal and unit arithmetic."""
from decimal import Decimal

import pytest

from conflux_analytics.chain.units import (
    decimal_str,
    from_raw,
    safe_div,
    scale_by_power_of_ten,
    to_raw,
)


@pytest.mark.unit
class TestToRaw:
    def test_basic_conversion(self):
        assert to_raw("1.5", 18) == 1_500_000_000_000_000_000

    def test_zero_decimals(self):
        assert to_raw("42", 0) == 42

    def test_six_decimals(self):
        assert to_raw("0.25", 6) == 250_000

    def test_integer_input(self):
        assert to_raw(3, 18) == 3_000_000_000_000_000_000

    def test_rejects_extra_precision(self):
        with pytest.raises(ValueError):
            to_raw("0.0000000001", 6)

    def test_rejects_negative_decimals(self):
        with pytest.raises(ValueError):
            to_raw("1", -1)

    def test_no_float_drift(self):
        # A float path would produce 999999999999999999 or similar drift.
        assert to_raw("100000000000000000000", 18) == 10**38


@pytest.mark.unit
class TestFromRaw:
    def test_basic(self):
        assert from_raw(1_500_000_000_000_000_000, 18) == Decimal("1.5")

    def test_string_roundtrip(self):
        raw = to_raw("12.345678", 18)
        assert from_raw(str(raw), 18) == Decimal("12.345678")

    def test_six_decimals(self):
        assert from_raw(250_000, 6) == Decimal("0.25")

    def test_rejects_bool(self):
        with pytest.raises(ValueError):
            from_raw(True, 18)

    def test_rejects_bad_string(self):
        with pytest.raises(ValueError):
            from_raw("12.5.6", 18)


@pytest.mark.unit
class TestDecimalStr:
    def test_fixed_point(self):
        assert decimal_str(Decimal("1E-8")) == "0.000000010000000000"

    def test_none_stays_none(self):
        assert decimal_str(None) is None

    def test_no_scientific_notation(self):
        value = Decimal("123456789012345678901234567890")
        assert "E" not in decimal_str(value).upper()


@pytest.mark.unit
class TestHelpers:
    def test_safe_div_zero_denominator(self):
        assert safe_div(1, 0) is None

    def test_safe_div_ok(self):
        assert safe_div(3, 4) == Decimal("0.75")

    def test_scale(self):
        assert scale_by_power_of_ten("1.5", 18) == Decimal(15 * 10**17)
