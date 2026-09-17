"""Shared offline fixtures. No live network access is required here."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from conflux_analytics.chain.addresses import normalize_address  # noqa: E402
from conflux_analytics.models.common import DataStatus, PoolType  # noqa: E402
from conflux_analytics.models.market import MarketState  # noqa: E402
from conflux_analytics.models.token import Token  # noqa: E402

CHAIN_ID = 1030
WCFX_ADDRESS = normalize_address("0x82521026fC6C7b0B48B03C5Fb6E4cb02D4a3Fb52")
USDT_ADDRESS = normalize_address("0x9c84E637c9Cd7CbC50a29b29b0b3C7E6c4c8b0E1")
POOL_ADDRESS = normalize_address("0x2A0b6dd8D37dB4C4b0Dd9E0bD3e6b6b0e0a1c1A2")


@pytest.fixture()
def wcfx() -> Token:
    return Token(chain_id=CHAIN_ID, address=WCFX_ADDRESS, symbol="WCFX", decimals=18)


@pytest.fixture()
def usdt() -> Token:
    return Token(chain_id=CHAIN_ID, address=USDT_ADDRESS, symbol="USDT", decimals=6)


@pytest.fixture()
def cpmm_state(wcfx: Token, usdt: Token) -> MarketState:
    """1,000,000 WCFX / 250,000 USDT -> spot price 0.25 USDT per WCFX."""
    return MarketState(
        chain_id=CHAIN_ID,
        dex_id="swappi",
        pool_address=POOL_ADDRESS,
        pool_type=PoolType.CONSTANT_PRODUCT,
        token0=wcfx,
        token1=usdt,
        block_number=123456789,
        observed_at="2026-09-15T00:00:00Z",
        reserve0_raw=1_000_000 * 10**18,
        reserve1_raw=250_000 * 10**6,
        fee_bps=30,
        data_status=DataStatus.VERIFIED,
    )
