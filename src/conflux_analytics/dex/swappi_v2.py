"""Swappi V2 adapter (constant-product AMM on Conflux eSpace).

Verified facts behind this implementation (see ``docs/dex-integrations.md``):

* ``SwappiFactory`` and ``SwappiRouter`` addresses come from the official
  Swappi documentation and the ``conflux-fans/espace-uniswap-lib`` preset.
* ``factory()`` on the router returns the configured factory, and
  ``WETH()`` returns the configured wrapped-native token.
* ``factory.getPair(WCFX, USDT)`` returns exactly the pair address published in
  the Swappi documentation, which cross-validates factory, router and the
  documented pool.
* The swap fee was derived from the chain, not assumed: ``router.getAmountsOut``
  output is reproduced *exactly* by the constant-product formula with a
  9975/10000 fee numerator for multiple trade sizes in both directions.

Prices here are pool-derived. No external price source is consulted.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from ..chain.abi import (
    decode_address,
    decode_uint,
    decode_uint_array,
    encode_address,
    encode_address_array,
    encode_uint,
    event_topic,
    function_selector,
)
from ..chain.addresses import normalize_address
from ..chain.logs import LogFetcher, decode_indexed_address, log_index_of, sort_logs
from ..chain.tokens import TokenReader
from ..errors import RpcError
from ..logging_setup import get_logger
from ..models.common import DataStatus, PoolType, Provenance, QuoteMethod, utc_now_iso
from ..models.events import LiquidityEvent, SwapEvent
from ..models.execution import FeeInfo, Quote
from ..models.market import MarketState
from ..models.pool import Pool
from ..rpc.provider import RpcProvider
from .base import ContractCheck, DexAdapter, DexCapabilities, DexHealth

logger = get_logger(__name__)

ALL_PAIRS_SIGNATURE = "allPairs(uint256)"
ALL_PAIRS_LENGTH_SIGNATURE = "allPairsLength()"
GET_PAIR_SIGNATURE = "getPair(address,address)"
GET_RESERVES_SIGNATURE = "getReserves()"
GET_AMOUNTS_OUT_SIGNATURE = "getAmountsOut(uint256,address[])"
TOKEN0_SIGNATURE = "token0()"
TOKEN1_SIGNATURE = "token1()"
TOTAL_SUPPLY_SIGNATURE = "totalSupply()"
FACTORY_SIGNATURE = "factory()"
WETH_SIGNATURE = "WETH()"

SWAP_EVENT = "Swap(address,uint256,uint256,uint256,uint256,address)"
MINT_EVENT = "Mint(address,uint256,uint256)"
BURN_EVENT = "Burn(address,uint256,uint256,address)"

#: Fee numerator verified on-chain for this deployment (25 bps).
FEE_NUMERATOR = 9975
FEE_DENOMINATOR = 10000


class SwappiV2Adapter(DexAdapter):
    """Adapter for Swappi's constant-product pools."""

    def __init__(
        self,
        definition: Any,
        provider: RpcProvider,
        chain_id: int,
        token_reader: TokenReader | None = None,
        log_chunk_size: int = 2000,
        wrapped_native_address: str | None = None,
    ) -> None:
        super().__init__(definition, provider, chain_id, token_reader)
        self.log_chunk_size = log_chunk_size
        self.wrapped_native_address = (
            normalize_address(wrapped_native_address) if wrapped_native_address else None
        )
        self.capabilities = DexCapabilities(
            supports_pool_discovery=True,
            supports_pool_state=True,
            supports_quotes=True,
            supports_swap_simulation=False,
            supports_historical_events=True,
            supports_liquidity_events=True,
            supports_fee_metadata=True,
            supports_gas_estimation=True,
            supports_quoter_gas_estimate=False,
            supports_direct_contract_calls=True,
            supports_enumerable_pools=True,
            supports_concentrated_liquidity=False,
        )
        self._timestamp_cache: dict[int, int | None] = {}

    # -- configuration -----------------------------------------------------
    @property
    def factory_address(self) -> str:
        address = self.definition.contract("factory")
        if not address:
            raise ValueError(f"{self.dex_id}: factory address is not configured")
        return normalize_address(address)

    @property
    def router_address(self) -> str:
        address = self.definition.contract("router") or self.definition.contract("legacy_router")
        if not address:
            raise ValueError(f"{self.dex_id}: router address is not configured")
        return normalize_address(address)

    def _sources(self, key: str) -> list[str]:
        return self.definition.address_sources().get(key, [])

    def _ordered_tokens(self, pair: str, block: int | str = "latest") -> tuple[str, str]:
        """Return ``(token0, token1)`` as the pool contract orders them."""
        token0 = normalize_address(
            decode_address(self.provider.eth_call(pair, function_selector(TOKEN0_SIGNATURE), block))
        )
        token1 = normalize_address(
            decode_address(self.provider.eth_call(pair, function_selector(TOKEN1_SIGNATURE), block))
        )
        return token0, token1

    # -- discovery ---------------------------------------------------------
    def pool_count(self, block: int | str = "latest") -> int:
        """Number of pairs registered in the factory."""
        raw = self.provider.eth_call(
            self.factory_address, function_selector(ALL_PAIRS_LENGTH_SIGNATURE), block
        )
        return decode_uint(raw)

    def all_pairs(self, block: int | str = "latest") -> list[str]:
        """Enumerate every pair address registered in the factory."""
        total = self.pool_count(block)
        pairs: list[str] = []
        for index in range(total):
            raw = self.provider.eth_call(
                self.factory_address,
                function_selector(ALL_PAIRS_SIGNATURE) + encode_uint(index),
                block,
            )
            address = decode_address(raw)
            if int(address, 16) != 0:
                pairs.append(normalize_address(address))
        return pairs

    def get_pair(self, token_a: str, token_b: str, block: int | str = "latest") -> str | None:
        """Return the factory's pair for two tokens, or ``None`` if unlisted."""
        raw = self.provider.eth_call(
            self.factory_address,
            function_selector(GET_PAIR_SIGNATURE) + encode_address(token_a) + encode_address(token_b),
            block,
        )
        address = normalize_address(decode_address(raw))
        return None if int(address, 16) == 0 else address

    def discover_pools(
        self, tracked_tokens: Sequence[str], block: int | str = "latest"
    ) -> list[Pool]:
        """Discover pairs among the tracked tokens via ``factory.getPair``.

        For a bounded tracked-token set this is authoritative and complete for
        the question being asked (markets between tracked tokens) while needing
        one call per unordered token pair instead of enumerating all ~900
        factory pairs. Every discovered pool is still re-verified by
        :meth:`get_pool_metadata` (``factory.getPair(token0,token1) == pool``).
        """
        tracked = sorted({normalize_address(address) for address in tracked_tokens})
        pools: list[Pool] = []
        for i, token_a in enumerate(tracked):
            for token_b in tracked[i + 1 :]:
                pair = self.get_pair(token_a, token_b, block)
                if pair is None:
                    continue
                token0, token1 = self._ordered_tokens(pair, block)
                if {token0, token1} != {token_a, token_b}:
                    # The pair exists but its tokens are not the requested ones
                    # (should not happen with a canonical factory); skip it.
                    continue
                pools.append(
                    Pool(
                        chain_id=self.chain_id,
                        pool_address=pair,
                        dex_id=self.dex_id,
                        token0_address=token0,
                        token1_address=token1,
                        pool_type=PoolType.CONSTANT_PRODUCT,
                        fee_bps=self.definition.fee.bps,
                        discovery_source="factory_getPair",
                        verification_method="factory.getPair(token0,token1)==pool",
                        verified_at=utc_now_iso(),
                        provenance=Provenance(
                            source="rpc_eth_call",
                            method="factory_getPair",
                            chain_id=self.chain_id,
                            contract_address=self.factory_address,
                            observed_at=utc_now_iso(),
                            data_status=DataStatus.VERIFIED,
                            sources=self._sources("factory"),
                        ),
                    )
                )
        return pools

    def get_pool_metadata(self, pool: Pool, block: int | str = "latest") -> Pool:
        """Confirm the pool is the factory's pair for its two tokens.

        The fee for this deployment is a deployment-wide constant that was
        derived on-chain (see the module docstring). It is attached here rather
        than assumed inside the analytics layer.
        """
        expected = self.get_pair(pool.token0_address, pool.token1_address, block)
        matches = expected == pool.pool_address
        return Pool(
            chain_id=pool.chain_id,
            pool_address=pool.pool_address,
            dex_id=pool.dex_id,
            token0_address=pool.token0_address,
            token1_address=pool.token1_address,
            pool_type=pool.pool_type,
            fee_bps=self.definition.fee.bps,
            fee_tier_raw=None,
            tick_spacing=None,
            discovery_source=pool.discovery_source,
            verification_method="factory.getPair(token0,token1)==pool",
            verified_at=utc_now_iso(),
            active=matches,
            status=DataStatus.VERIFIED if matches else DataStatus.ERROR,
            detail=None if matches else f"factory reports pair {expected} for these tokens",
            provenance=pool.provenance,
        )

    # -- state -------------------------------------------------------------
    def block_timestamp(self, block_number: int) -> int | None:
        """Block timestamp, cached per run to avoid repeated calls."""
        if block_number not in self._timestamp_cache:
            block = self.provider.get_block_by_number(block_number, False)
            timestamp = int(block["timestamp"], 16) if block and block.get("timestamp") else None
            self._timestamp_cache[block_number] = timestamp
        return self._timestamp_cache[block_number]

    def _resolve_block_number(self, block: int | str) -> int:
        if isinstance(block, int):
            return block
        return self.provider.block_number()

    def get_pool_state(
        self, pool: Pool, block: int | str = "latest", run_id: str | None = None
    ) -> MarketState:
        """Read reserves and LP supply for a constant-product pair."""
        block_number = self._resolve_block_number(block)
        try:
            raw = self.provider.eth_call(
                pool.pool_address, function_selector(GET_RESERVES_SIGNATURE), block
            )
        except RpcError as exc:
            return self._empty_state(
                pool, block_number, f"getReserves() failed: {exc.rpc_message or exc}", run_id
            )
        try:
            reserve0 = decode_uint(raw, 0)
            reserve1 = decode_uint(raw, 1)
            timestamp_last = decode_uint(raw, 2)
        except (ValueError, IndexError) as exc:
            return self._empty_state(
                pool, block_number, f"getReserves() returned malformed data: {exc}", run_id
            )

        total_supply: int | None = None
        try:
            total_supply = decode_uint(
                self.provider.eth_call(
                    pool.pool_address, function_selector(TOTAL_SUPPLY_SIGNATURE), block
                )
            )
        except (RpcError, ValueError, IndexError) as exc:
            logger.info(
                "LP totalSupply unavailable",
                extra={
                    "event": "market_state.partial",
                    "dex": self.dex_id,
                    "pool": pool.pool_address,
                    "error": str(exc),
                },
            )

        return MarketState(
            chain_id=self.chain_id,
            dex_id=self.dex_id,
            pool_address=pool.pool_address,
            pool_type=PoolType.CONSTANT_PRODUCT,
            token0=self.token_metadata(pool.token0_address, block),
            token1=self.token_metadata(pool.token1_address, block),
            block_number=block_number,
            observed_at=utc_now_iso(),
            run_id=run_id,
            reserve0_raw=reserve0,
            reserve1_raw=reserve1,
            block_timestamp_last=timestamp_last,
            fee_bps=pool.fee_bps if pool.fee_bps is not None else self.definition.fee.bps,
            total_supply_raw=total_supply,
            raw_state={
                "getReserves": {
                    "reserve0": str(reserve0),
                    "reserve1": str(reserve1),
                    "blockTimestampLast": timestamp_last,
                },
                "totalSupply": str(total_supply) if total_supply is not None else None,
            },
            source="rpc_eth_call:getReserves",
            data_status=DataStatus.VERIFIED,
            provenance=Provenance(
                source="rpc_eth_call",
                method="getReserves()",
                chain_id=self.chain_id,
                block_number=block_number,
                contract_address=pool.pool_address,
                observed_at=utc_now_iso(),
                data_status=DataStatus.VERIFIED,
            ),
        )

    # -- fees --------------------------------------------------------------
    def get_fee_configuration(self, pool: Pool, block: int | str = "latest") -> FeeInfo:
        """Fee for this deployment, derived on-chain rather than assumed."""
        bps = self.definition.fee.bps
        return FeeInfo(
            source=self.definition.fee.source,
            fee_bps=bps,
            fee_rate=None if bps is None else _bps_to_rate(bps),
            basis="input_token",
            data_status=DataStatus.VERIFIED if bps is not None else DataStatus.UNAVAILABLE,
            detail=(
                "deployment-wide constant; router.getAmountsOut reproduced exactly by the "
                f"constant-product formula with numerator {FEE_NUMERATOR}/{FEE_DENOMINATOR}"
            ),
        )

    # -- quoting -----------------------------------------------------------
    def get_quote(
        self,
        pool: Pool,
        token_in: str,
        token_out: str,
        amount_in_raw: int,
        block: int | str = "latest",
    ) -> Quote:
        """Quote through the protocol's own router (``getAmountsOut``).

        The route is validated first: if the factory's pair for these two tokens
        is not this pool, the quote is refused rather than reporting a price that
        could not be executed against the pool being analysed.
        """
        token_in = normalize_address(token_in)
        token_out = normalize_address(token_out)
        block_number = self._resolve_block_number(block)

        def failure(reason: str) -> Quote:
            return Quote(
                dex_id=self.dex_id,
                pool_address=pool.pool_address,
                input_token=token_in,
                output_token=token_out,
                input_amount_raw=amount_in_raw,
                method=QuoteMethod.ROUTER_QUOTE,
                block_number=block_number,
                success=False,
                failure_reason=reason,
                source=f"router:{self.router_address}",
                data_status=DataStatus.ERROR,
                detail=reason,
            )

        if amount_in_raw <= 0:
            return failure("amount_in_raw must be positive")
        if not pool.contains(token_in) or not pool.contains(token_out):
            return failure("token pair does not belong to this pool")

        try:
            route_pool = self.get_pair(token_in, token_out, block)
        except (RpcError, ValueError, IndexError) as exc:
            return failure(f"factory.getPair failed: {exc}")
        if route_pool != pool.pool_address:
            return failure(f"router route uses pair {route_pool}, not {pool.pool_address}")

        data = (
            function_selector(GET_AMOUNTS_OUT_SIGNATURE)
            + encode_uint(amount_in_raw)
            + encode_uint(64)
            + encode_address_array([token_in, token_out])
        )
        try:
            raw = self.provider.eth_call(self.router_address, data, block)
        except RpcError as exc:
            return failure(f"getAmountsOut reverted: {exc.rpc_message or exc}")
        try:
            amounts = decode_uint_array(raw)
        except ValueError as exc:
            return failure(f"getAmountsOut returned malformed data: {exc}")
        if len(amounts) < 2:
            return failure("getAmountsOut returned fewer amounts than the path length")

        return Quote(
            dex_id=self.dex_id,
            pool_address=pool.pool_address,
            input_token=token_in,
            output_token=token_out,
            input_amount_raw=amount_in_raw,
            method=QuoteMethod.ROUTER_QUOTE,
            block_number=block_number,
            output_amount_raw=amounts[-1],
            success=True,
            source=f"router:{self.router_address}",
            data_status=DataStatus.SIMULATED,
            detail="router.getAmountsOut at the pinned block",
            raw_response={"amounts": [str(amount) for amount in amounts]},
        )

    # -- events ------------------------------------------------------------
    def get_swap_events(self, pool: Pool, from_block: int, to_block: int) -> list[SwapEvent]:
        """Collect ``Swap`` events for the pool over a bounded range."""
        fetcher = LogFetcher(self.provider, self.log_chunk_size)
        result = fetcher.fetch(
            address=pool.pool_address,
            topics=[event_topic(SWAP_EVENT)],
            from_block=from_block,
            to_block=to_block,
        )
        events: list[SwapEvent] = []
        for log in sort_logs(result.logs):
            event = self._parse_swap_log(pool, log)
            if event is not None:
                events.append(event)
        return events

    def _parse_swap_log(self, pool: Pool, log: dict[str, Any]) -> SwapEvent | None:
        """Decode a V2 ``Swap`` log into a normalised :class:`SwapEvent`."""
        try:
            data = log.get("data", "0x")
            amount0_in = decode_uint(data, 0)
            amount1_in = decode_uint(data, 1)
            amount0_out = decode_uint(data, 2)
            amount1_out = decode_uint(data, 3)
        except (ValueError, IndexError):
            logger.warning(
                "discarding malformed Swap log",
                extra={
                    "event": "swap_events.malformed",
                    "dex": self.dex_id,
                    "pool": pool.pool_address,
                    "tx": log.get("transactionHash"),
                },
            )
            return None
        topics = log.get("topics") or []
        block_number = int(log.get("blockNumber", "0x0"), 16)
        return SwapEvent(
            chain_id=self.chain_id,
            dex_id=self.dex_id,
            pool_address=pool.pool_address,
            pool_type=PoolType.CONSTANT_PRODUCT,
            block_number=block_number,
            block_hash=log.get("blockHash"),
            block_timestamp=self.block_timestamp(block_number),
            transaction_hash=log.get("transactionHash", ""),
            log_index=log_index_of(log),
            sender=decode_indexed_address(topics[1] if len(topics) > 1 else None),
            recipient=decode_indexed_address(topics[2] if len(topics) > 2 else None),
            amount0_raw=amount0_in - amount0_out,
            amount1_raw=amount1_in - amount1_out,
            observed_at=utc_now_iso(),
            data_status=DataStatus.VERIFIED,
            raw_log={"topics": topics, "data": data},
            provenance=Provenance(
                source="rpc_eth_getLogs",
                method=SWAP_EVENT,
                chain_id=self.chain_id,
                block_number=block_number,
                block_hash=log.get("blockHash"),
                transaction_hash=log.get("transactionHash"),
                log_index=log_index_of(log),
                contract_address=pool.pool_address,
                observed_at=utc_now_iso(),
                data_status=DataStatus.VERIFIED,
            ),
        )


    def get_liquidity_events(
        self, pool: Pool, from_block: int, to_block: int
    ) -> list[LiquidityEvent]:
        """Collect V2 ``Mint`` and ``Burn`` logs for a bounded block range."""
        fetcher = LogFetcher(self.provider, self.log_chunk_size)
        mints = fetcher.fetch(
            address=pool.pool_address,
            topics=[event_topic(MINT_EVENT)],
            from_block=from_block,
            to_block=to_block,
        )
        burns = fetcher.fetch(
            address=pool.pool_address,
            topics=[event_topic(BURN_EVENT)],
            from_block=from_block,
            to_block=to_block,
        )
        events: list[LiquidityEvent] = []
        for log in sort_logs([*mints.logs, *burns.logs]):
            event = self._parse_liquidity_log(pool, log)
            if event is not None:
                events.append(event)
        return events

    def _parse_liquidity_log(self, pool: Pool, log: dict[str, Any]) -> LiquidityEvent | None:
        """Decode a V2 ``Mint``/``Burn`` log into a normalised liquidity event."""
        topics = log.get("topics") or []
        if not topics:
            return None
        mint_topic = event_topic(MINT_EVENT)
        burn_topic = event_topic(BURN_EVENT)
        if topics[0] == mint_topic:
            event_type = "mint"
        elif topics[0] == burn_topic:
            event_type = "burn"
        else:
            return None
        try:
            data = log.get("data", "0x")
            amount0_raw = decode_uint(data, 0)
            amount1_raw = decode_uint(data, 1)
        except (ValueError, IndexError):
            logger.warning(
                "discarding malformed liquidity log",
                extra={
                    "event": "liquidity_events.malformed",
                    "dex": self.dex_id,
                    "pool": pool.pool_address,
                    "tx": log.get("transactionHash"),
                },
            )
            return None
        block_number = int(log.get("blockNumber", "0x0"), 16)
        # V2 Mint(sender, amount0, amount1): sender is the caller.
        # V2 Burn(sender, amount0, amount1, to): `to` receives the tokens.
        sender = decode_indexed_address(topics[1] if len(topics) > 1 else None)
        owner = None
        if event_type == "burn":
            owner = decode_indexed_address(topics[2] if len(topics) > 2 else None)
        return LiquidityEvent(
            chain_id=self.chain_id,
            dex_id=self.dex_id,
            pool_address=pool.pool_address,
            pool_type=PoolType.CONSTANT_PRODUCT,
            event_type=event_type,
            block_number=block_number,
            transaction_hash=log.get("transactionHash", ""),
            log_index=log_index_of(log),
            observed_at=utc_now_iso(),
            block_hash=log.get("blockHash"),
            block_timestamp=self.block_timestamp(block_number),
            owner=owner,
            sender=sender,
            amount0_raw=amount0_raw,
            amount1_raw=amount1_raw,
            data_status=DataStatus.VERIFIED,
            raw_log={"topics": topics, "data": log.get("data", "0x")},
            provenance=Provenance(
                source="rpc_eth_getLogs",
                method=MINT_EVENT if event_type == "mint" else BURN_EVENT,
                chain_id=self.chain_id,
                block_number=block_number,
                block_hash=log.get("blockHash"),
                transaction_hash=log.get("transactionHash"),
                log_index=log_index_of(log),
                contract_address=pool.pool_address,
                observed_at=utc_now_iso(),
                data_status=DataStatus.VERIFIED,
            ),
        )

    def verify_contracts(self, block: int | str = "latest") -> list[ContractCheck]:
        """Verify configured addresses and the factory/router relationship.

        Every check is an on-chain read. A failed check disables the adapter for
        the run instead of being logged and ignored.
        """
        checks: list[ContractCheck] = []
        factory = self.factory_address
        router = self.router_address

        factory_code = self.provider.get_code(factory, block)
        checks.append(
            ContractCheck(
                contract_key="factory",
                address=factory,
                check="eth_getCode",
                expected="non-empty contract code",
                observed=f"{max(len(factory_code[2:]) // 2, 0)} bytes",
                ok=len(factory_code) > 2,
                detail=None if len(factory_code) > 2 else "no code at the configured address",
                sources=self._sources("factory"),
            )
        )

        pair_count: int | None = None
        try:
            raw = self.provider.eth_call(
                factory, function_selector(ALL_PAIRS_LENGTH_SIGNATURE), block
            )
            pair_count = decode_uint(raw)
        except (RpcError, ValueError, IndexError) as exc:
            checks.append(
                ContractCheck(
                    contract_key="factory",
                    address=factory,
                    check="allPairsLength()",
                    expected="uint256",
                    observed=None,
                    ok=False,
                    detail=str(exc),
                    sources=self._sources("factory"),
                )
            )
        else:
            checks.append(
                ContractCheck(
                    contract_key="factory",
                    address=factory,
                    check="allPairsLength()",
                    expected="uint256 > 0",
                    observed=str(pair_count),
                    ok=bool(pair_count and pair_count > 0),
                    detail="V2-style factory enumeration",
                    sources=self._sources("factory"),
                )
            )

        router_code = self.provider.get_code(router, block)
        checks.append(
            ContractCheck(
                contract_key="router",
                address=router,
                check="eth_getCode",
                expected="non-empty contract code",
                observed=f"{max(len(router_code[2:]) // 2, 0)} bytes",
                ok=len(router_code) > 2,
                sources=self._sources("router"),
            )
        )

        try:
            raw = self.provider.eth_call(router, function_selector(FACTORY_SIGNATURE), block)
            observed_factory = decode_address(raw)
        except (RpcError, ValueError, IndexError) as exc:
            checks.append(
                ContractCheck(
                    contract_key="router",
                    address=router,
                    check="factory()",
                    expected=factory,
                    observed=None,
                    ok=False,
                    detail=str(exc),
                    sources=self._sources("router"),
                )
            )
        else:
            checks.append(
                ContractCheck(
                    contract_key="router",
                    address=router,
                    check="factory()",
                    expected=factory,
                    observed=observed_factory,
                    ok=observed_factory == factory,
                    detail="router must point at the configured factory",
                    sources=self._sources("router"),
                )
            )

        if self.wrapped_native_address:
            try:
                raw = self.provider.eth_call(router, function_selector(WETH_SIGNATURE), block)
                observed_weth = decode_address(raw)
            except (RpcError, ValueError, IndexError) as exc:
                checks.append(
                    ContractCheck(
                        contract_key="router",
                        address=router,
                        check="WETH()",
                        expected=self.wrapped_native_address,
                        observed=None,
                        ok=False,
                        detail=str(exc),
                        sources=self._sources("router"),
                    )
                )
            else:
                checks.append(
                    ContractCheck(
                        contract_key="router",
                        address=router,
                        check="WETH()",
                        expected=self.wrapped_native_address,
                        observed=observed_weth,
                        ok=observed_weth == self.wrapped_native_address,
                        detail="router wrapped-native token must match the configured token",
                        sources=self._sources("router"),
                    )
                )
        return checks

    def health_check(self, block: int | str = "latest") -> DexHealth:
        """Verify contracts and report a single overall status."""
        checks = self.verify_contracts(block)
        ok = all(check.ok for check in checks)
        detail = None
        if not ok:
            detail = "; ".join(
                f"{check.contract_key}.{check.check}: {check.detail or 'mismatch'}"
                for check in checks
                if not check.ok
            )
        return DexHealth(
            dex_id=self.dex_id,
            status="ok" if ok else "failed",
            checks=checks,
            detail=detail,
            checked_at=utc_now_iso(),
        )


def _bps_to_rate(bps: int) -> Decimal:
    """Convert basis points to a :class:`~decimal.Decimal` fee rate."""
    return Decimal(bps) / Decimal(10000)