"""Unit tests for the pool-math reference price and CPMM quote engine."""
from decimal import Decimal

import pytest

from conflux_analytics.analytics.pricing import (
    PRICE_CONVENTION,
    reference_price_from_state,
    sqrt_price_x96_to_price,
)
from conflux_analytics.models.common import DataStatus, PoolType, QuoteMethod
from conflux_analytics.models.market import MarketState
from conflux_analytics.models.token import Token


def _cpmm_state(fee_bps=30):
    return MarketState(
        chain_id=1030,
        dex_id="testdex",
        pool_address="0x" + "33" * 20,
        pool_type=PoolType.CONSTANT_PRODUCT,
        token0=Token(chain_id=1030, address="0x" + "11" * 20, symbol="AAA", decimals=18),
        token1=Token(chain_id=1030, address="0x" + "22" * 20, symbol="BBB", decimals=6),
        block_number=100,
        observed_at="2026-09-16T00:00:00Z",
        reserve0_raw=1_000_000 * 10**18,   # 1,000,000 token0
        reserve1_raw=2_000_000 * 10**6,    # 2,000,000 token1
        fee_bps=fee_bps,
        data_status=DataStatus.VERIFIED,
    )


@pytest.mark.unit
class TestReferencePrice:
    def test_constant_product_spot_price(self):
        state = _cpmm_state()
        ref = reference_price_from_state(state, state.token0.address, state.token1.address)
        # price = (reserve1/1e6) / (reserve0/1e18) = 2 BBB per AAA
        assert ref.price == Decimal("2")
        assert ref.data_status == DataStatus.DERIVED
        assert ref.method == "constant_product_reserves"

    def test_constant_product_inverse(self):
        state = _cpmm_state()
        ref = reference_price_from_state(state, state.token1.address, state.token0.address)
        assert ref.price == Decimal("0.5")

    def test_token_order_is_not_base_quote(self):
        # Swapping the direction must invert the price, proving that ordering
        # is protocol-defined (address order), not "base over quote".
        state = _cpmm_state()
        fwd = reference_price_from_state(state, state.token0.address, state.token1.address)
        rev = reference_price_from_state(state, state.token1.address, state.token0.address)
        assert abs(fwd.price * rev.price - 1) < Decimal("1E-18")

    def test_unknown_token_is_unavailable(self):
        state = _cpmm_state()
        ref = reference_price_from_state(state, "0x" + "ff" * 20, state.token1.address)
        assert ref.price is None
        assert ref.data_status == DataStatus.UNAVAILABLE

    def test_missing_reserves_unavailable(self):
        state = _cpmm_state()
        empty = MarketState(
            chain_id=state.chain_id,
            dex_id=state.dex_id,
            pool_address=state.pool_address,
            pool_type=state.pool_type,
            token0=state.token0,
            token1=state.token1,
            block_number=100,
            observed_at=state.observed_at,
            data_status=DataStatus.UNAVAILABLE,
        )
        ref = reference_price_from_state(
            empty, state.token0.address, state.token1.address
        )
        assert ref.price is None
        assert ref.data_status == DataStatus.UNAVAILABLE

    def test_convention_string(self):
        assert "output" in PRICE_CONVENTION


@pytest.mark.unit
class TestSqrtPrice:
    def test_sqrt_price_x96_to_price(self):
        # sqrt(1) in X96: 2**96 -> price 1.0
        assert sqrt_price_x96_to_price(1 << 96, 18, 18) == Decimal(1)

    def test_sqrt_price_scaled_decimals(self):
        # price (token1 raw per token0 raw) = 2 -> sqrt = sqrt(2)*2**96
        two = Decimal(2)
        sqrt_raw = int((two.sqrt() * (1 << 96)).to_integral_value())
        price = sqrt_price_x96_to_price(sqrt_raw, 18, 18)
        assert abs(price - 2) < Decimal("1E-10")


@pytest.mark.unit
class TestQuoteMethodEnum:
    def test_pool_math_is_not_simulation_label(self):
        # The pool-math method must be distinguishable from router quotes.
        assert QuoteMethod.POOL_MATH.value == "pool_math"
        assert QuoteMethod.ROUTER_QUOTE.value == "router_quote"


class TestCPMMQuoteEngine:
    def _engine(self):
        from conflux_analytics.dex.pool_math import ConstantProductMath

        return ConstantProductMath(fee_bps=30)

    def test_small_trade_output(self):
        engine = self._engine()
        out = engine.amount_out(
            input_amount_raw=10**18,  # 1 token0
            reserve_in_raw=1_000_000 * 10**18,
            reserve_out_raw=2_000_000 * 10**6,
        )
        # exact Uniswap-V2 formula: out = 997*in*r_out / (1000*r_in + 997*in)
        expected = (Decimal(997) * 10**18) * (2_000_000 * 10**6) / (
            Decimal(1000) * (1_000_000 * 10**18) + Decimal(997) * 10**18
        )
        assert out == int(expected)
        reserve_out = 2_000_000 * 10**6
        no_fee = Decimal(reserve_out) * (10**18) / Decimal(1_000_000 * 10**18)
        assert out < int(no_fee)  # less than the spot price would give

    def test_zero_input(self):
        engine = self._engine()
        with pytest.raises(ValueError):
            engine.amount_out(input_amount_raw=0, reserve_in_raw=1000, reserve_out_raw=1000)

    def test_empty_reserve(self):
        engine = self._engine()
        with pytest.raises(ValueError):
            engine.amount_out(input_amount_raw=100, reserve_in_raw=0, reserve_out_raw=1000)

    def test_price_impact_grows_with_size(self):
        engine = self._engine()
        reserves_in, reserves_out = 10**24, 10**24
        small = engine.amount_out(10**18, reserves_in, reserves_out)
        large = engine.amount_out(10**21, reserves_in, reserves_out)
        small_price = Decimal(small) / Decimal(10**18)
        large_price = Decimal(large) / Decimal(10**21)
        assert large_price < small_price  # worse execution at larger size
