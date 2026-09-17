# DEX integrations

Addresses in `config/dexes.yaml` are configuration data with documented
sources; every adapter verifies each address against the chain (contract code
present, expected interface selectors respond) before enabling it. Nothing in
this file should be treated as a substitute for that verification.

## Swappi (constant-product AMM)

* Documentation: <https://docs.swappi.finance/> — Swappi is an automated market
  maker on Conflux eSpace modelled on the Uniswap-V2 design (factory +
  pair contracts, `getReserves()`-style state, Router `getAmountsOut`).
* Pool model: constant product (`constant_product`).
* Discovery: `factory.getPair(tokenA, tokenB)` over the configured tracked
  tokens (factory-based, deterministic, minimal RPC load). The factory also
  supports `allPairsLength`/`allPairs` enumeration, which is used when
  discovery runs without a token restriction.
* Pool state: `getReserves()` on the pair plus `token0()`/`token1()`,
  `feeBps()` style configuration where exposed by the verified interface.
* Quoting: `router_quote` — `getAmountsOut`/`getAmountsIn` on the verified
  Swappi Router via `eth_call` (no transaction is sent).
* Events: `Swap(address,uint256,uint256,uint256,uint256,address)`,
  `Mint`, `Burn` (Uniswap-V2 signature set), collected over bounded ranges
  with `eth_getLogs`.
* Verified live on eSpace mainnet (chain ID 1030) during development: pools
  for the configured WCFX/USDT/USDC token set were discovered and reserves
  read from chain.

## vSwap (concentrated liquidity)

* Documentation: <https://vswap.fi/> — vSwap is a concentrated-liquidity DEX
  on Conflux eSpace modelled on the Uniswap-V3 design (pool with
  `slot0`/`liquidity`, non-fungible positions).
* Pool model: concentrated liquidity (`concentrated_liquidity`). CL pools are
  never forced into a constant-product model; the market-state abstraction
  carries `sqrt_price_x96`, `tick`, `liquidity`, fee tier and tick spacing.
* Discovery: configured pool discovery — vSwap factory interfaces verified
  during development are used where enumerable; otherwise verified pools are
  configured explicitly in `config/dexes.yaml` with sources.
* Quoting: `contract_simulation` where a Quoter contract is verified and
  reachable via `eth_call`; `pool_math` (deterministic CL maths on verified
  `sqrt_price_x96`/`liquidity` state) otherwise, and the method is labelled
  accordingly.
* Verified live on eSpace mainnet: configured pools resolved and state read
  from chain.

## Capability matrix (as shipped)

| Capability | Swappi | vSwap |
|---|---|---|
| supports_pool_discovery | yes | yes (factory/configured) |
| supports_pool_state | yes | yes |
| supports_quotes | yes (router_quote) | yes (quoter/pool_math) |
| supports_swap_simulation | no (no verified simulate interface) | no |
| supports_historical_events | yes (Swap/Mint/Burn) | yes where verified |
| supports_fee_metadata | yes (pair fee) | yes (fee tier from pool) |
| supports_gas_estimation | yes (eth_estimateGas on router call data) | yes where estimable |

Anything not in the matrix is recorded as `unavailable` at runtime rather than
approximated.

## Verification procedure

For each configured contract the adapter, at run time:

1. resolves the address and normalises it,
2. `eth_getCode` — the address must be a contract,
3. `eth_call` on the interface-specific selector(s) (e.g. `factory()` on a
   pair, `getReserves()` on a pool) — must decode successfully,
4. results are recorded per contract in `DexHealth` and exposed via
   `GET /api/dexes` and the CLI `health` command.

If a check fails, the DEX's markets are marked degraded/error in the API and
dashboard; data already collected keeps its original provenance.
