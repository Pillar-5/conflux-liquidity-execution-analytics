"""Analytics engine: liquidity, pricing, fees, gas and execution metrics.

This package contains only deterministic calculations over normalized market
data. It never talks to the network itself; inputs come from DEX adapters and
the collectors, outputs feed storage, the API, the dashboard and reports.
"""

from .fees import bps_to_rate, fee_amount_on_input, input_after_fee, rate_to_bps
from .liquidity import LiquidityMetrics, UsdPriceSource, liquidity_metrics
from .pricing import PRICE_CONVENTION, reference_price_from_state

__all__ = [
    "PRICE_CONVENTION",
    "LiquidityMetrics",
    "UsdPriceSource",
    "bps_to_rate",
    "fee_amount_on_input",
    "input_after_fee",
    "liquidity_metrics",
    "rate_to_bps",
    "reference_price_from_state",
]
