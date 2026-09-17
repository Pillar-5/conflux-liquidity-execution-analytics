"""Unit tests for the execution analytics math (pricing, impact, fees)."""
from decimal import Decimal

import pytest

from conflux_analytics.analytics.execution import (
    IMPACT_METHODOLOGY,
    compute_price_impact,
)
from conflux_analytics.analytics.fees import (
    bps_to_rate,
    fee_amount_on_input,
    input_after_fee,
    rate_to_bps,
)
from conflux_analytics.models.common import DataStatus


@pytest.mark.unit
class TestFees:
    def test_bps_to_rate(self):
        assert bps_to_rate(30) == Decimal("0.003")

    def test_rate_to_bps(self):
        assert rate_to_bps(Decimal("0.003")) == 30

    def test_roundtrip(self):
        for bps in (0, 1, 5, 25, 30, 100, 3000):
            assert rate_to_bps(bps_to_rate(bps)) == bps

    def test_fee_amount(self):
        assert fee_amount_on_input(1_000_000, Decimal("0.003")) == 3_000

    def test_fee_amount_floor(self):
        # 1 wei input with 0.3% fee -> fraction of a wei, floored, never invented
        assert fee_amount_on_input(1, Decimal("0.003")) == 0

    def test_input_after_fee(self):
        assert input_after_fee(1_000_000, Decimal("0.003")) == 997_000


@pytest.mark.unit
class TestPriceImpact:
    def test_impact_matches_convention(self):
        # reference 2000 output/input; effective execution 1990 (no fee data,
        # so the fee-excluded price equals the raw effective price) -> 0.5%
        impact = compute_price_impact(
            reference_price=Decimal(2000),
            output_amount_raw=1_990_000,
            input_amount_raw=1_000,
            fee_rate=None,
            input_decimals=6,
            output_decimals=6,
        )
        assert impact is not None
        expected = Decimal("0.005")
        assert abs(impact.impact_fraction - expected) < Decimal("0.0001")

    def test_fee_excluded_price_removes_fee(self):
        impact = compute_price_impact(
            reference_price=Decimal(1),
            output_amount_raw=997_000,
            input_amount_raw=1_000_000,
            fee_rate=Decimal("0.003"),
            input_decimals=6,
            output_decimals=6,
        )
        assert impact is not None
        # fee-excluded effective price == reference -> zero impact
        assert abs(impact.impact_fraction) < Decimal("1E-12")
        # raw effective price (fees included) shows the 0.3% deviation
        assert abs(impact.total_deviation_fraction - Decimal("0.003")) < Decimal("1E-12")

    def test_none_reference_gives_none_impact(self):
        impact = compute_price_impact(
            reference_price=None,
            output_amount_raw=1,
            input_amount_raw=1,
            fee_rate=None,
            input_decimals=18,
            output_decimals=18,
        )
        # unavailable, not fabricated: the fraction is None and the record
        # carries an explicit unavailable status.
        assert impact.impact_fraction is None
        assert impact.data_status.value == "unavailable"

    def test_zero_output_gives_maximal_impact(self):
        """A zero-output quote means the full deviation (100%), not 'no data'."""
        impact = compute_price_impact(
            reference_price=Decimal(1),
            output_amount_raw=0,
            input_amount_raw=1,
            fee_rate=None,
            input_decimals=18,
            output_decimals=18,
        )
        assert impact.impact_fraction == 1
        assert impact.total_deviation_fraction == 1

    def test_methodology_documented(self):
        assert "fee" in IMPACT_METHODOLOGY.lower()

    def test_bps_fields(self):
        impact = compute_price_impact(
            reference_price=Decimal(100),
            output_amount_raw=99,
            input_amount_raw=1,
            fee_rate=None,
            input_decimals=0,
            output_decimals=0,
        )
        assert impact is not None
        assert impact.impact_bps == pytest.approx(100.0, abs=0.01)
        assert impact.data_status == DataStatus.DERIVED
