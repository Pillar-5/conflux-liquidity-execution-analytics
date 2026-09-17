# Data model

SQLite database (`config: `database.path``, default `data/conflux_analytics.db`)
managed by numbered migrations (`storage/migrations.py`). All raw amounts are
stored as **integer strings**; decimal-normalised values are stored as
fixed-point strings produced by `Decimal` (precision 80, no floats).

## Tables

| Table | Purpose | Key provenance columns |
|---|---|---|
| `schema_migrations` | applied migration versions | — |
| `collection_runs` | one row per run: status, block range, counters, errors | `run_id`, `started_at` |
| `collection_checkpoints` | resumable cursor per (run, dex, kind) | `dex_id`, `last_block` |
| `provider_health` | RPC health observations (latency, chain id, block) | `rpc_url`, `checked_at` |
| `dexes` | configured/verified DEX deployments | `dex_id`, verification JSON |
| `tokens` | ERC-20 metadata; identity is `(chain_id, address)` | `decimals`, `metadata_status` |
| `pools` | discovered markets: token pair, pool type, fee, discovery source | `discovery_source`, `verified_at` |
| `pool_states` | market-state snapshots (reserves or sqrt_price/tick/liquidity) | `block_number`, `block_hash`, `observed_at` |
| `swap_events` | normalised swap logs (signed pool-balance deltas) | `tx_hash`, `log_index`, raw log |
| `liquidity_events` | mint/burn events where verified | `tx_hash`, `log_index` |
| `gas_observations` | **actual** gas from receipts (`receipt_actual`) | `tx_hash` |
| `prices` | USD price sources used, with their provenance | `source` |
| `execution_results` | execution analytics observations (quote, prices, impact, fee, gas estimate) | `quote_method`, `block_number`, provenance JSON |
| `analytics_results` | misc derived results (liquidity aggregates) | provenance JSON |

Indexes cover the query paths used by the API (pool address, block number,
`observed_at`, `run_id`, `(chain_id, address)`).

## Latest run vs history

`collection_runs.run_id` is stamped on every observation. "Current market
state" is the newest `pool_states` row for a pool from the **latest
successful run**; older rows remain queryable as history and are never
presented as current. The API filters with `latest=true/false` and time/block
ranges.
