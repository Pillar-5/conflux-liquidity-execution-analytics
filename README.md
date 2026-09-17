# Conflux Liquidity & Execution Analytics

An open-source **Liquidity and Execution Analytics** layer for **Conflux eSpace**
(EVM, chain ID 1030). It collects real on-chain and DEX data, normalizes it,
calculates liquidity and execution metrics, persists observations to SQLite,
exposes a REST API + dashboard, and produces reproducible analytical results.

> This is deterministic blockchain analytics infrastructure. It does **not**
> place trades, sign transactions, hold private keys, or run AI/ML models.

## What it answers

- What DEX markets are available on Conflux eSpace?
- What tokens are in each market, and in what order?
- What is the current liquidity / reserves for each pool?
- What execution price would a trade receive, and what is the price impact?
- What fees and gas cost apply to a simulated swap?
- How do these values change across configurable trade sizes?
- Which market gives better execution for a given trade size?
- How fresh is the underlying data, and how was each number derived?

## Supported DEXs (mainnet)

| DEX | Adapter | Pool model | Discovery | Quote method |
|-----|---------|------------|-----------|--------------|
| Swappi (V2 constant-product) | `swappi_v2` | Constant product (x·y=k) | Factory `allPairs` + `getPair` | Router `getAmountOut` |
| vSwap (concentrated liquidity) | `vswap_v3` | Concentrated liquidity (Uniswap V3-style) | Configured pools | Quoters `quoteExactInputSingle` (contract simulation) |

> Adapter addresses are **verified against the chain at startup** and recorded
> with their documentation sources. See `docs/dex-integrations.md`.

## Quick start

```bash
# 1. Install
git clone https://github.com/Pillar-5/conflux-liquidity-execution-analytics
cd conflux-liquidity-execution-analytics
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# 2. Configure (all defaults work against public RPC)
cp .env.example .env

# 3. Initialize the database schema
conflux-analytics initdb

# 4. Discover DEX pools (verifies contracts on-chain)
conflux-analytics discover

# 5. Collect market state, execution analytics and historical events
conflux-analytics collect

# 6. Serve API + dashboard (open http://127.0.0.1:8000)
conflux-analytics serve

# 7. Write reproducible reports
conflux-analytics report
```

## Architecture

```
                        ┌────────────────────────────────────┐
  public RPC  ───────► │  rpc provider  (http, retries,      │
  https://evm.conflux  │  │  chain validation, health probe)   │
                        └──────────┬─────────────────────────┘
                                   │
         ┌─────────────────────────┴──────────────────────────┐
         │                     pipeline                       │
         │  discover ─► collect(state, execution, events)    │
         └──────┬───────────────┬───────────────┬────────────┘
                │               │               │
          ┌─────▼────┐    ┌────▼────┐    ┌─────▼─────┐
          │  dex      │    │ analytics │    │ storage   │
          │  adapters │    │ (pricing, │    │ (SQLite)  │
          │  (v2/v3)  │    │  liq,     │    │           │
          └─────┬────┘    │  exec,    │    └─────┬─────┘
                │          │  gas)      │          │
                └──────────┬───────────┘          │
                           │                     │
                    ┌──────▼──────┐    ┌─────────▼────────┐
                    │   API       │    │   dashboard      │
                    │  (FastAPI)  │    │  (server-rendered)│
                    └─────────────┘    └──────────────────┘
```

A modular **monolith**: adapters implement a common `DexAdapter` interface, so
adding a new DEX is a matter of implementing an adapter class and enabling it in
`config/dexes.yaml` — no changes to the analytics engine are required.

## Configuration

All values are environment-driven via `.env` (see `.env.example`). Key variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `CONFLUX_RPC_URL` | `https://evm.confluxrpc.com` | Primary RPC |
| `CONFLUX_WS_RPC_URL` | *(empty)* | Optional fallback WS |
| `CONFLUX_CHAIN_ID` | `1030` | Chain-ID validated against the node |
| `CONFLUX_EXPLORER_URL` | `https://evm.confluxscan.org` | Block-explorer links |
| `RPC_TIMEOUT_SECONDS` | `20.0` | Per-call timeout |
| `RPC_MAX_RETRIES` | `3` | Retry count for transient failures |
| `CONFLUX_ANALYTICS_DB` | `data/conflux_analytics.db` | SQLite path |
| `DEXS_ENABLED` | `swappi_v2,vswap_v3` | DEXs to enable |
| `TRADE_SIZES` | `small:1,medium:10,large:100` | Execution trade sizes (whole input-token units) |
| `PRICE_STABLECOIN_PEGS` | `{}` | Explicit peg assumptions (never implicit) |
| `MAX_BLOCKS_PER_RUN` | `20000` | Historical scan safety cap |

Testnet: set `CONFLUX_RPC_URL=https://evmtestnet.confluxrpc.com` and
`CONFLUX_CHAIN_ID=71`.

## Data provenance & status labels

Every analytical value is tagged with its origin so results are always
**reproducible and auditable**:

| Label | Meaning |
|-------|---------|
| **VERIFIED** | Read directly from an on-chain contract call / RPC. |
| **DERIVED** | Computed from verified inputs via a deterministic formula. |
| **ESTIMATED** | Approximated from heuristics (e.g. gas unit estimate). |
| **SIMULATED** | Produced by a deterministic on-chain `eth_call` quote (no state change). |
| **UNAVAILABLE** | The data source is not exposed by the selected provider / DEX; never silently substituted. |

Every observation records: `chain_id`, `block_number`, `block_hash` (where
available), `contract_address`, `observed_at` (UTC), the `method` used, and the
`data_status`. USD values are always labelled with their `price_source`.

## Data model

- `pools` — pool_address, dex_id, token0/1, decimals, pool_type, fee_bps,
  fee_tier_raw, tick_spacing, discovery_source, verified_at, active.
- `pool_states` — snapshot of reserves / concentrated-liquidity state per
  block, with `provenance_json`.
- `execution_observations` — one row per (pool, direction, trade size):
  input/output amounts, effective price, price impact, fees, gas estimate,
  confidence label, success/failure.
- `swap_events`, `liquidity_events` — historical on-chain events (chunked,
  checkpointed).
- `runs` / `run_errors` / `checkpoints` — run-level metadata and resumability.

See `docs/data-model.md` for the full schema.

## Analytics methodology

- **Pricing**: reference price is DEX-derived (CPMM spot for v2 / sqrt-price
  for v3), never a hardcoded peg. USD liquidity requires an explicit, documented
  price source; otherwise values are token-denominated with `price_source=null`.
- **Price impact**: `effective_price` vs `reference_price`, expressed as a
  fraction and basis points, with fee adjustment.
- **Gas**: real `eth_estimateGas` + `eth_maxPriorityFeePerGas`/`eth_gasPrice`;
  never fabricated. Marked `SIMULATED`/`ESTIMATED` accordingly.
- All token-quantity and financial math uses Python `Decimal`; raw on-chain
  amounts are stored as integers.

See `docs/analytics-methodology.md`.

## CLI reference

| Command | Purpose |
|---------|---------|
| `conflux-analytics health` | Probe RPC reachability, chain ID, eth_call. |
| `conflux-analytics initdb` | Create / upgrade the SQLite schema (migrations 1–7). |
| `conflux-analytics discover` | Discover & verify pools, persist to catalog. |
| `conflux-analytics collect` | Collect state, executions and events. |
| `conflux-analytics analyze` | Re-run execution analytics over stored markets. |
| `conflux-analytics report` | Write CSV/JSON/Markdown reports. |
| `conflux-analytics benchmark` | Reproducible collection benchmark. |
| `conflux-analytics serve` | Start the API + dashboard (uvicorn). |

## API

OpenAPI schema at `http://127.0.0.1:8000/openapi.json`. Key routes:

- `GET /health` — RPC + node health.
- `GET /api/dexes` — enabled DEXs and capabilities.
- `GET /api/pools` — discovered markets.
- `GET /api/pools/{address}/state` — latest market state.
- `GET /api/execution` — latest execution observations.
- `GET /api/runs/{run_id}/results` — results of a completed collection run.
- `GET /api/report` — latest generated report summary.

The dashboard is mounted at `/dashboard` (server-rendered HTML, no JS bundle,
no wallet access).

## Testing

```bash
# Offline unit tests (deterministic, no network):
pytest -q

# Full suite including integration (requires a live RPC):
pytest -q -m "integration or not integration"
```

Integration tests that need a live RPC are marked `@pytest.mark.integration`
and are skipped by default.

## Reproducibility

`docs/reproducibility.md` documents how to reproduce any observation. The
`benchmark` and `report` commands emit a self-describing dataset containing the
exact configuration, block numbers, schema version and data-status labels used
to produce every metric.

## Adding a DEX

1. Implement a subclass of `DexAdapter` in `src/conflux_analytics/dex/`.
2. Set capability flags accurately in `DexCapabilities`.
3. Register it in `config/dexes.yaml` and the registry.
4. Run `conflux-analytics discover --dex <dex_id>` — addresses are verified
   on-chain before being enabled.

## Notes & limitations

- The MVP uses the **public Conflux eSpace RPC** as its only blockchain data
  source (`https://evm.confluxrpc.com`). No paid APIs are required.
- Historical `eth_getLogs` is chunked and checkpointed; public RPCs cap log
  ranges, so large scans are rate-limit aware.
- Tracing (`trace_transaction` / `debug_traceTransaction`) is **optional**: it
  is probed at startup and trace-derived fields are marked `UNAVAILABLE` when
  the provider does not expose it.
- Concentrated-liquidity pools are **not** forced into a constant-product
  model; each adapter implements its own state/quote math.

## License

MIT — see `LICENSE`.


