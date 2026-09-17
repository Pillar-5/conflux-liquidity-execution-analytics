"""Fee conversion and gas-cost maths, including the "estimated vs actual" split."""

from __future__ import annotations

from decimal import Decimal

import pytest

from conflux_analytics.analytics.fees import (
    bps_to_rate,
    fee_amount_on_input,
    fee_info_from_bps,
    input_after_fee,
    parts_to_rate,
    rate_to_bps,
)
from conflux_analytics.analytics.gas import (
    ESTIMATE_METHOD_ETH,
    METHOD_PROTOCOL,
    cost_native_wei,
    estimate_route_gas,
    protocol_gas_estimate,
    wei_to_native,
)
from conflux_analytics.errors import RpcError
from conflux_analytics.models.common import DataStatus


# ---------------------------------------------------------------------------
# fee conversion
# ---------------------------------------------------------------------------
def test_bps_converts_to_a_fraction() -> None:
    assert bps_to_rate(30) == Decimal("0.003")
    assert bps_to_rate(5) == Decimal("0.0005")
    assert bps_to_rate(0) == Decimal("0")
    with pytest.raises(ValueError):
        bps_to_rate(-1)


def test_univ3_parts_convert_to_a_fraction() -> None:
    # vSwap/Swappi V3 pools report fee() in parts per 1e6.
    assert parts_to_rate(500, 1_000_000) == Decimal("0.0005")
    assert parts_to_rate(3000, 1_000_000) == Decimal("0.003")
    with pytest.raises(ValueError):
        parts_to_rate(1, 0)
    with pytest.raises(ValueError):
        parts_to_rate(-1, 1_000_000)


def test_rate_round_trips_through_bps() -> None:
    assert rate_to_bps(Decimal("0.003")) == 30
    assert rate_to_bps(Decimal("0")) == 0
    with pytest.raises(ValueError):
        rate_to_bps(Decimal("-0.1"))


def test_fee_amount_rounds_down_and_never_exceeds_the_input() -> None:
    rate = bps_to_rate(30)
    assert fee_amount_on_input(1_000_000, rate) == 3_000
    # floor, not round-half-up: 333..3 * 0.003 = 0.9999 -> 0
    assert fee_amount_on_input(333, rate) == 0
    assert input_after_fee(1_000_000, rate) == 997_000
    with pytest.raises(ValueError):
        fee_amount_on_input(-1, rate)


def test_large_amounts_keep_full_precision() -> None:
    """A raw amount near 2**100 must not lose digits through the fee maths."""
    raw = 1 << 100
    rate = bps_to_rate(30)
    fee = fee_amount_on_input(raw, rate)
    assert fee == raw * 3 // 1000
    assert fee + input_after_fee(raw, rate) == raw


def test_fee_info_from_bps_marks_a_missing_fee_as_unavailable() -> None:
    info = fee_info_from_bps("pool_state", 30)
    assert info.fee_bps == 30
    assert info.fee_rate == Decimal("0.003")
    assert info.data_status is DataStatus.VERIFIED

    missing = fee_info_from_bps("pool_state", None)
    assert missing.fee_bps is None
    assert missing.fee_rate is None
    assert missing.data_status is DataStatus.UNAVAILABLE


# ---------------------------------------------------------------------------
# gas
# ---------------------------------------------------------------------------
def test_native_cost_is_units_times_price_wei() -> None:
    assert cost_native_wei(150_000, 10_000_000_000) == 1_500_000_000_000_000
    assert wei_to_native(1_500_000_000_000_000) == pytest.approx(0.0015)


class GasProvider:
    def __init__(self, units: int | None = 21_000, price: int | None = 10**10):
        self.units = units
        self.price = price
        self.price_calls = 0

    def estimate_gas(self, transaction):  # noqa: ANN001, ANN201
        if self.units is None:
            raise RpcError("eth_estimateGas", 3, "execution reverted")
        return self.units

    def gas_price(self):
        self.price_calls += 1
        if self.price is None:
            raise RpcError("eth_gasPrice", -32000, "unavailable")
        return self.price


def test_gas_estimate_is_labelled_estimated() -> None:
    estimate = estimate_route_gas(GasProvider(), {"to": "0xpool", "data": "0x"})
    assert estimate.data_status is DataStatus.ESTIMATED
    assert estimate.method == ESTIMATE_METHOD_ETH
    assert estimate.gas_units == 21_000
    assert estimate.gas_cost_native_wei == 21_000 * 10**10
    assert estimate.native_symbol == "CFX"


def test_reverting_estimate_is_unavailable_not_fabricated() -> None:
    estimate = estimate_route_gas(GasProvider(units=None), {"to": "0xpool", "data": "0x"})
    assert estimate.data_status is DataStatus.UNAVAILABLE
    assert estimate.gas_units is None
    assert estimate.gas_cost_native_wei is None
    assert "reverted" in (estimate.failure_reason or "")


def test_missing_gas_price_yields_gas_units_only() -> None:
    estimate = estimate_route_gas(GasProvider(price=None), {"to": "0xpool", "data": "0x"})
    assert estimate.data_status is DataStatus.PARTIAL
    assert estimate.gas_units == 21_000
    assert estimate.gas_cost_native_wei is None


def test_protocol_estimate_keeps_its_own_method_label() -> None:
    estimate = protocol_gas_estimate(GasProvider(), 116_443, source="vswap_v3:quoter")
    assert estimate.method == METHOD_PROTOCOL
    assert estimate.source == "vswap_v3:quoter"
    assert estimate.data_status is DataStatus.ESTIMATED
    assert estimate.gas_units == 116_443
    assert estimate.gas_cost_native_wei == 116_443 * 10**10


def test_protocol_estimate_without_a_gas_price_records_units_only() -> None:
    estimate = protocol_gas_estimate(GasProvider(price=None), 116_443, source="vswap_v3:quoter")
    assert estimate.data_status is DataStatus.PARTIAL
    assert estimate.gas_units == 116_443
    assert estimate.gas_cost_native_wei is None