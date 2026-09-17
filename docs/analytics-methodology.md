# Analytics methodology

Conventions fixed across the whole project (API, dashboard, reports):

* A **price** is always *units of the output token per one whole unit of the
  input token*.
* **Effective execution price** = `output_amount / input_amount` for the
  quoted trade.
* **Reference price** = the pool's spot price at the same block, derived from
  verified pool state:
  * constant-product pools: `reserve_out / reserve_in` from verified
    `getReserves()` data → status `derived`;
  * concentrated-liquidity pools: `sqrt_price_x96^2 / 2^192` with token
    decimals applied → status `derived`.
* **Price impact** = `1 - fee_excluded_effective_price / reference_price`.
  Protocol fees are removed from the execution before comparison, so impact
  measures size-related deviation only. The fee is reported separately
  (`fee_bps`, `fee_amount`). `total_deviation_bps` reports
  `1 - effective_price / reference_price` and is labelled as including fees.
  The two numbers are never mixed.
* **Fee amount** is expressed on the input amount at the pool's verified fee
  rate (e.g. 30 bps → `input * 30/10000`).
* **Gas** from `eth_estimateGas` is always `estimated` and carries the gas
  price used; **actual** gas from transaction receipts is a different
  quantity, stored in a separate table and never compared to estimates as if
  equal.
* **Liquidity**: token-denominated reserves are computed for every supported
  pool. USD liquidity is produced **only** when a price source is configured
  (a documented stablecoin peg in `config/default.yaml`, or a future external
  API). Every USD value records its `price_source`; without a source USD
  fields are `null`.
* **Imbalance** = `|reserve0_value - reserve1_value| /
  (reserve0_value + reserve1_value)`, computed in token-denominated terms
  (scaled by the pool's own price ratio when tokens differ).
* **Freshness**: `data_age_seconds` is derived from `observed_at`; the API and
  dashboard map freshness to `fresh`/`stale`/`unavailable` states using the
  configured freshness window.

## Determinism

Given the same database, `python -m conflux_analytics.report` reproduces the
same numbers byte-for-byte: all values come from stored rows via `Decimal`
arithmetic (precision 80) with no floats, no randomness and no network access
at report time.
