# Reproducibility

Every analytical result produced by this project can be traced back to the
exact on-chain inputs it was derived from. This document explains how.

## What "reproducible" means here

An **execution observation** (one row per pool × direction × trade size) is
fully determined by:

1. the **configuration** (trade sizes, enabled DEXs, RPC endpoint),
2. the **block number** at which pool state was read,
3. the **deterministic quote math** (Uniswap V2 formula for `swappi_v2`,
   on-chain quoter `eth_call` for `vswap_v3`),
4. the **schema version** of the SQLite database.

No random sampling, no hidden price feeds, no AI models. Given the same
configuration and the same block, any developer gets the same numbers.

## Provenance on every row

Each observation stores:

| Field | Meaning |
|-------|---------|
| `chain_id` | Chain the data came from (1030 = eSpace mainnet, 71 = testnet). |
| `block_number` / `block_hash` | Exact block the pool state was read at. |
| `contract_address` | Pool contract that was queried. |
| `observed_at` | UTC timestamp of the observation. |
| `method` | How the value was produced (e.g. `router_quote`, `pool_math`, `contract_simulation`). |
| `data_status` | `VERIFIED` / `DERIVED` / `ESTIMATED` / `SIMULATED` / `UNAVAILABLE` — see README. |
| `price_source` | Source of any USD conversion, or `null` when token-denominated. |

## Reproducing the demo dataset

The `report` command emits a self-describing dataset (CSV + JSON + Markdown)
under `reports/` containing the exact configuration, block numbers, schema
version and data-status labels:

```bash
conflux-analytics initdb
conflux-analytics discover
conflux-analytics collect
conflux-analytics report
```

Re-running the same commands at a later block produces fresh observations;
comparing `block_number` columns explains any differences.

## Reproducing a single observation

1. Find the row in `execution_observations` (or the CSV export).
2. Note `pool_address`, `block_number`, `trade_size` (raw input amount) and
   `method`.
3. For `swappi_v2` (`pool_math`): call `getReserves` on the pool and
   `getAmountOut` on the router at that block, then apply the V2 formula —
   the result must match `output_amount_raw` exactly.
4. For `vswap_v3` (`contract_simulation`): replay `quoteExactInputSingle`
   against the quoter contract at that block with the same inputs.

Historical state queries require an **archive node**; the default public RPC
serves recent state only. The `benchmark` command records the provider used
so this is always auditable.

## What is deliberately *not* reproduced

- **USD values** depend on the configured price source (`PRICE_STABLECOIN_PEGS`
  or an external API). The assumption is stored with the value; change the
  assumption and the USD column changes — the token-denominated columns do not.
- **Gas estimates** depend on live network conditions (`eth_gasPrice`,
  `eth_estimateGas`). They are labelled `ESTIMATED`/`SIMULATED` and are
  inherently point-in-time.
