"""vSwap (WallFreeX V3) adapter - concentrated liquidity on Conflux eSpace.

Verified facts behind this implementation (see ``docs/dex-integrations.md``):

* Factory, router, QuoterV2 and NFT position manager addresses come from the
  ``conflux-fans/espace-uniswap-lib`` mainnet preset and were cross-checked
  on-chain: ``router.factory()`` returns the configured factory, the position
  manager reports ``name() = 'vSwap Positions NFT-V1'``, and the QuoterV2
  answered a live ``quoteExactInputSingle`` eth_call with the expected 4-word
  tuple.
* Pools are Uniswap-V3 style: ``slot0()`` (sqrtPriceX96, tick, ...),
  ``liquidity()``, ``fee()``, ``tickSpacing()``.
* Quotes come from the protocol's own QuoterV2 contract
  (``quoteExactInputSingle``), so they are contract simulations, not local
  estimates. The quoter also returns the post-trade sqrtPriceX96, initialized
  ticks crossed and a gas estimate.

Prices are pool-derived. No external price source is consulted.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..chain.abi import (
    decode_address,
    decode_int,
    decode_uint,
    decode_words,
    encode_address,
    encode_uint,
    event_topic,
    function_selector,
    strip_hex,
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

GET_POOL_SIGNATURE = "getPool(address,address,uint24)"
POOL_FEE_SIGNATURE = "fee()"
TICK_SPACING_SIGNATURE = "tickSpacing()"
SLOT0_SIGNATURE = "slot0()"
LIQUIDITY_SIGNATURE = "liquidity()"
QUOTE_EXACT_INPUT_SINGLE_SIGNATURE = (
    "quoteExactInputSingle((address,address,uint256,uint24,uint160))"
)
#: Router entry point used only to build an *unsigned* transaction for
#: ``eth_estimateGas``. It is never signed or broadcast.
EXACT_INPUT_SINGLE_SIGNATURE = (
    "exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))"
)

SWAP_EVENT = "Swap(address,address,int256,int256,uint160,uint128,int24)"
MINT_EVENT = "Mint(address,address,int24,int24,uint128,uint256,uint256)"
BURN_EVENT = "Burn(address,int24,int24,uint128,uint256,uint256)"
POOL_CREATED_EVENT = "PoolCreated(address,address,uint24,int24,address)"

#: Denominator of the Uniswap-V3 style pool ``fee()`` value (fee / 1e6).
V3_FEE_DENOMINATOR = 1_000_000
#: sqrtPriceX96 sentinel meaning "no price limit" in QuoterV2 calls.
SQRT_PRICE_LIMIT_NONE = 0
#: Default fee tiers probed during discovery (Uniswap-V3 convention).
DEFAULT_FEE_TIERS: tuple[int, ...] = (500, 3000, 10000)


class VswapV3Adapter(DexAdapter):
    """Adapter for vSwap's Uniswap-V3 style concentrated-liquidity pools."""

    def __init__(
        self,
        definition: Any,
        provider: RpcProvider,
        chain_id: int,
        token_reader: TokenReader | None = None,
        log_chunk_size: int = 2000,
    ) -> None:
        super().__init__(definition, provider, chain_id, token_reader)
        self.log_chunk_size = log_chunk_size
        self.capabilities = DexCapabilities(
            supports_pool_discovery=True,
            supports_pool_state=True,
            supports_quotes=True,
            supports_swap_simulation=True,
            supports_historical_events=True,
            supports_liquidity_events=True,
            supports_fee_metadata=True,
            supports_gas_estimation=True,
            supports_quoter_gas_estimate=True,
            supports_direct_contract_calls=True,
            supports_enumerable_pools=False,
            supports_concentrated_liquidity=True,
        )

    # -- configuration -----------------------------------------------------
    def _contract(self, key: str) -> str:
        address = self.definition.contract(key)
        if not address:
            raise ValueError(f"{self.dex_id}: {key} address is not configured")
        return normalize_address(address)

    @property
    def factory_address(self) -> str:
        return self._contract("factory")

    @property
    def quoter_address(self) -> str:
        return self._contract("quoter_v2")

    def _sources(self, key: str) -> list[str]:
        return self.definition.address_sources().get(key, [])

    def _resolve_block_number(self, block: int | str) -> int:
        """Resolve a block reference into a concrete number."""
        if isinstance(block, int):
            return block
        return self.provider.block_number()

    # -- verification ------------------------------------------------------
    def verify_contracts(self, block: int | str = "latest") -> list[ContractCheck]:
        checks: list[ContractCheck] = []
        for key in ("factory", "router", "quoter_v2", "nft_position_manager"):
            sources = self._sources(key)
            configured = self.definition.contract(key)
            if not configured:
                checks.append(
                    ContractCheck(
                        contract_key=key,
                        address="",
                        check="configured",
                        expected="a valid eSpace address",
                        observed="not configured",
                        ok=False,
                        detail=f"{key} is absent from configuration",
                        sources=sources,
                    )
                )
                continue
            try:
                address = normalize_address(configured)
                code = self.provider.get_code(address, block)
                code_bytes = len(strip_hex(code)) // 2
            except (ValueError, RpcError) as exc:
                checks.append(
                    ContractCheck(
                        contract_key=key,
                        address=str(configured),
                        check="eth_getCode",
                        expected="contract code",
                        observed=f"error: {exc}",
                        ok=False,
                        detail=None,
                        sources=sources,
                    )
                )
                continue
            checks.append(
                ContractCheck(
                    contract_key=key,
                    address=address,
                    check="eth_getCode",
                    expected="contract code",
                    observed=f"{code_bytes} bytes",
                    ok=code_bytes > 0,
                    detail=None if code_bytes else "no code at this address",
                    sources=sources,
                )
            )
        return checks

    def health_check(self, block: int | str = "latest") -> DexHealth:
        checks = self.verify_contracts(block)
        status = "ok" if all(c.ok for c in checks) else "degraded"
        return DexHealth(
            dex_id=self.dex_id,
            status=status,
            checks=checks,
            pools_checked=0,
            sample_pool=None,
            detail=None if status == "ok" else "configured contracts failed verification",
            checked_at=utc_now_iso(),
        )

    # -- discovery ---------------------------------------------------------
    def discover_pools(
        self, tracked_tokens: Sequence[str], block: int | str = "latest"
    ) -> list[Pool]:
        """Discover V3 pools for tracked token pairs across configured fee tiers.

        Uniswap-V3 style factories are not enumerable, so discovery is a
        deterministic lookup of ``factory.getPool(token0, token1, fee)`` for
        every tracked pair and configured tier. The zero address means "no
        pool at this tier" and is skipped.
        """
        tracked = sorted({normalize_address(address) for address in tracked_tokens})
        factory = self.factory_address
        tiers = self.definition.discovery.fee_tiers or list(DEFAULT_FEE_TIERS)
        pools: list[Pool] = []
        for i, token_a in enumerate(tracked):
            for token_b in tracked[i + 1 :]:
                for tier in tiers:
                    try:
                        raw = self.provider.eth_call(
                            factory,
                            function_selector(GET_POOL_SIGNATURE)
                            + encode_address(token_a)
                            + encode_address(token_b)
                            + encode_uint(tier),
                            block,
                        )
                        pool_address = normalize_address(decode_address(raw))
                    except (RpcError, ValueError, IndexError) as exc:
                        logger.info(
                            "getPool lookup failed",
                            extra={
                                "event": "discovery.lookup_error",
                                "dex": self.dex_id,
                                "token_a": token_a,
                                "token_b": token_b,
                                "fee_tier": tier,
                                "error": str(exc),
                            },
                        )
                        continue
                    if int(pool_address, 16) == 0:
                        continue
                    pools.append(
                        Pool(
                            chain_id=self.chain_id,
                            pool_address=pool_address,
                            dex_id=self.dex_id,
                            token0_address=token_a,
                            token1_address=token_b,
                            pool_type=PoolType.CONCENTRATED_LIQUIDITY,
                            fee_tier_raw=tier,
                            fee_bps=tier // 100,
                            tick_spacing=self.definition.discovery.tick_spacing.get(tier),
                            discovery_source=f"factory_getPool(tier={tier})",
                            verification_method="factory.getPool(token0,token1,fee)==pool",
                            verified_at=utc_now_iso(),
                            provenance=Provenance(
                                source="rpc_eth_call",
                                method="factory_getPool",
                                chain_id=self.chain_id,
                                contract_address=factory,
                                observed_at=utc_now_iso(),
                                detail=f"getPool({token_a},{token_b},{tier})",
                            ),
                        )
                    )
        return pools

    def get_pool_metadata(self, pool: Pool, block: int | str = "latest") -> Pool:
        """Read ``fee()`` and ``tickSpacing()`` straight from the pool contract."""
        fee_bps = pool.fee_bps
        tier = pool.fee_tier_raw
        tick_spacing = pool.tick_spacing
        try:
            raw_fee = self.provider.eth_call(
                pool.pool_address, function_selector(POOL_FEE_SIGNATURE), block
            )
            tier = decode_uint(raw_fee)
            fee_bps = tier // 100
        except (RpcError, ValueError, IndexError) as exc:
            logger.info(
                "pool fee() unavailable",
                extra={"event": "pool_metadata.partial", "pool": pool.pool_address, "error": str(exc)},
            )
        else:
            try:
                raw_ts = self.provider.eth_call(
                    pool.pool_address, function_selector(TICK_SPACING_SIGNATURE), block
                )
                tick_spacing = decode_uint(raw_ts)
            except (RpcError, ValueError, IndexError) as exc:
                logger.info(
                    "pool tickSpacing() unavailable",
                    extra={"event": "pool_metadata.partial", "pool": pool.pool_address, "error": str(exc)},
                )
        return Pool(
            chain_id=pool.chain_id,
            pool_address=pool.pool_address,
            dex_id=pool.dex_id,
            token0_address=pool.token0_address,
            token1_address=pool.token1_address,
            pool_type=pool.pool_type,
            fee_bps=fee_bps,
            fee_tier_raw=tier,
            tick_spacing=tick_spacing,
            discovery_source=pool.discovery_source,
            verification_method=pool.verification_method or "pool.fee()",
            verified_at=pool.verified_at or utc_now_iso(),
            active=pool.active,
            status=DataStatus.VERIFIED if fee_bps is not None else DataStatus.PARTIAL,
            detail=pool.detail,
            provenance=pool.provenance,
        )

    # -- state -------------------------------------------------------------
    def get_pool_state(
        self, pool: Pool, block: int | str = "latest", run_id: str | None = None
    ) -> MarketState:
        """Read V3 pool state: ``slot0()``, ``liquidity()`` and ``fee()``."""
        block_number = self._resolve_block_number(block)
        try:
            raw_slot0 = self.provider.eth_call(
                pool.pool_address, function_selector(SLOT0_SIGNATURE), block
            )
            sqrt_price_x96 = decode_uint(raw_slot0, 0)
            tick = decode_int(raw_slot0, 1)
        except (RpcError, ValueError, IndexError) as exc:
            return self._empty_state(
                pool, block_number, f"slot0() failed: {exc.rpc_message or exc}", run_id
            )
        try:
            liquidity = decode_uint(
                self.provider.eth_call(
                    pool.pool_address, function_selector(LIQUIDITY_SIGNATURE), block
                )
            )
        except (RpcError, ValueError, IndexError) as exc:
            return self._empty_state(
                pool, block_number, f"liquidity() failed: {exc.rpc_message or exc}", run_id
            )

        fee_bps = pool.fee_bps
        tick_spacing = pool.tick_spacing
        try:
            tier = decode_uint(
                self.provider.eth_call(
                    pool.pool_address, function_selector(POOL_FEE_SIGNATURE), block
                )
            )
            fee_bps = tier // 100
        except (RpcError, ValueError, IndexError):
            tier = pool.fee_tier_raw
        try:
            tick_spacing = decode_uint(
                self.provider.eth_call(
                    pool.pool_address, function_selector(TICK_SPACING_SIGNATURE), block
                )
            )
        except (RpcError, ValueError, IndexError):
            pass

        return MarketState(
            chain_id=self.chain_id,
            dex_id=self.dex_id,
            pool_address=pool.pool_address,
            pool_type=PoolType.CONCENTRATED_LIQUIDITY,
            token0=self.token_metadata(pool.token0_address, block),
            token1=self.token_metadata(pool.token1_address, block),
            block_number=block_number,
            observed_at=utc_now_iso(),
            run_id=run_id,
            sqrt_price_x96=sqrt_price_x96,
            tick=tick,
            liquidity=liquidity,
            fee_bps=fee_bps,
            fee_tier_raw=tier,
            tick_spacing=tick_spacing,
            raw_state={
                "slot0": {
                    "sqrtPriceX96": str(sqrt_price_x96),
                    "tick": tick,
                },
                "liquidity": str(liquidity),
                "feeTier": tier,
                "tickSpacing": tick_spacing,
            },
            source="rpc_eth_call:slot0+liquidity",
            data_status=DataStatus.VERIFIED,
            provenance=Provenance(
                source="rpc_eth_call",
                method="slot0()+liquidity()",
                chain_id=self.chain_id,
                block_number=block_number,
                contract_address=pool.pool_address,
                observed_at=utc_now_iso(),
                data_status=DataStatus.VERIFIED,
            ),
        )

    def block_timestamp(self, block_number: int) -> int | None:
        """Resolve a block timestamp, or ``None`` when it cannot be read."""
        from ..chain.blocks import fetch_block

        try:
            block = fetch_block(self.provider, block_number)
        except RpcError:
            return None
        return block.timestamp if block else None

    # -- transaction shape (gas estimation only) ---------------------------
    def _pool_fee_tier(self, pool: Pool) -> int | None:
        """Resolve the pool's fee tier in protocol units.

        Prefers the persisted raw tier; falls back to the on-chain-verified
        ``fee_bps`` using the protocol convention ``tier = fee_bps * 100``
        (hundredths of a basis point). Returns ``None`` only when neither is
        known — the quoter call is refused rather than guessed.
        """
        if pool.fee_tier_raw is not None:
            return pool.fee_tier_raw
        if pool.fee_bps is not None:
            return pool.fee_bps * 100
        return None

    def build_gas_transaction(
        self, pool: Pool, token_in: str, token_out: str, amount_in_raw: int
    ) -> dict[str, Any] | None:
        """Build the unsigned ``router.exactInputSingle`` call for this route.

        Used exclusively by ``eth_estimateGas``. The transaction carries no
        sender, nonce or signature, so it cannot be broadcast. The recipient is
        the zero address because the call is never executed.
        """
        router = self.definition.contract("router")
        tier = self._pool_fee_tier(pool)
        if not router or amount_in_raw <= 0 or tier is None:
            return None
        token_in = normalize_address(token_in)
        token_out = normalize_address(token_out)
        if not pool.contains(token_in) or not pool.contains(token_out):
            return None
        params = (
            encode_address(token_in)
            + encode_address(token_out)
            + encode_uint(tier)
            + encode_address("0x0000000000000000000000000000000000000000")
            + encode_uint(amount_in_raw)
            + encode_uint(0)
            + encode_uint(SQRT_PRICE_LIMIT_NONE)
        )
        return {
            "to": normalize_address(router),
            "data": function_selector(EXACT_INPUT_SINGLE_SIGNATURE) + params,
            "value": "0x0",
        }

    # -- fees --------------------------------------------------------------
    def get_fee_configuration(self, pool: Pool, block: int | str = "latest") -> FeeInfo:
        """Fee tier read from the pool contract (``fee()``), never assumed."""
        from ..analytics.fees import parts_to_rate

        tier = pool.fee_tier_raw
        if tier is None:
            try:
                tier = decode_uint(
                    self.provider.eth_call(
                        pool.pool_address, function_selector(POOL_FEE_SIGNATURE), block
                    )
                )
            except (RpcError, ValueError, IndexError) as exc:
                return FeeInfo(
                    source="pool.fee()",
                    data_status=DataStatus.UNAVAILABLE,
                    detail=f"fee() unavailable: {exc}",
                )
        rate = parts_to_rate(tier, V3_FEE_DENOMINATOR)
        return FeeInfo(
            source="pool.fee()",
            fee_bps=int(rate * 10000),
            fee_rate=rate,
            basis="input_amount",
            data_status=DataStatus.VERIFIED,
            detail=f"Uniswap-V3 style fee {tier}/{V3_FEE_DENOMINATOR} read from the pool",
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
        """Quote through the protocol's own QuoterV2 (``quoteExactInputSingle``).

        This is a contract simulation: the DEX's own quoter contract runs the
        swap maths against live pool state and reverts when the route is not
        tradeable. The result is therefore reported as ``contract_simulation``,
        never as ``pool_math``.
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
                method=QuoteMethod.CONTRACT_SIMULATION,
                block_number=block_number,
                success=False,
                failure_reason=reason,
                source=f"quoter:{self.quoter_address}",
                data_status=DataStatus.ERROR,
                detail=reason,
            )

        if amount_in_raw <= 0:
            return failure("amount_in_raw must be positive")
        if not pool.contains(token_in) or not pool.contains(token_out):
            return failure("token pair does not belong to this pool")
        tier = self._pool_fee_tier(pool)
        if tier is None:
            return failure("pool fee tier is unknown; the quoter call requires it")

        params = (
            encode_address(token_in)
            + encode_address(token_out)
            + encode_uint(amount_in_raw)
            + encode_uint(tier)
            + encode_uint(SQRT_PRICE_LIMIT_NONE)
        )
        try:
            raw = self.provider.eth_call(
                self.quoter_address,
                function_selector(QUOTE_EXACT_INPUT_SINGLE_SIGNATURE) + params,
                block,
            )
        except RpcError as exc:
            return failure(f"quoteExactInputSingle reverted: {exc.rpc_message or exc}")

        words = decode_words(raw)
        if len(words) < 3:
            return failure(
                f"quoteExactInputSingle returned {len(words)} words, expected at least 3"
            )
        amount_out = words[0]
        sqrt_price_after = words[1]
        ticks_crossed = words[2]
        gas_estimate = words[3] if len(words) > 3 else None
        if amount_out <= 0:
            return failure("quoter returned a non-positive output amount")

        return Quote(
            dex_id=self.dex_id,
            pool_address=pool.pool_address,
            input_token=token_in,
            output_token=token_out,
            input_amount_raw=amount_in_raw,
            output_amount_raw=amount_out,
            method=QuoteMethod.CONTRACT_SIMULATION,
            block_number=block_number,
            success=True,
            source=f"quoter:{self.quoter_address}",
            data_status=DataStatus.SIMULATED,
            detail="QuoterV2.quoteExactInputSingle at the pinned block",
            sqrt_price_x96_after=sqrt_price_after,
            initialized_ticks_crossed=ticks_crossed,
            raw_response={
                "amountOut": str(amount_out),
                "sqrtPriceX96After": str(sqrt_price_after),
                "initializedTicksCrossed": ticks_crossed,
                "gasEstimate": gas_estimate,
            },
        )

    # -- events ------------------------------------------------------------
    def get_swap_events(self, pool: Pool, from_block: int, to_block: int) -> list[SwapEvent]:
        """Collect V3 ``Swap`` logs for a bounded block range."""
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
        """Decode a V3 ``Swap`` log.

        The V3 ``Swap`` event already reports signed pool deltas, so
        ``amount0``/``amount1`` are used unchanged (see ``models/events.py``).
        """
        try:
            data = log.get("data", "0x")
            amount0 = decode_int(data, 0)
            amount1 = decode_int(data, 1)
            sqrt_price_x96 = decode_uint(data, 2)
            liquidity = decode_uint(data, 3)
            tick = decode_int(data, 4)
        except (ValueError, IndexError):
            logger.warning(
                "discarding malformed V3 Swap log",
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
            pool_type=PoolType.CONCENTRATED_LIQUIDITY,
            block_number=block_number,
            block_hash=log.get("blockHash"),
            block_timestamp=self.block_timestamp(block_number),
            transaction_hash=log.get("transactionHash", ""),
            log_index=log_index_of(log),
            sender=decode_indexed_address(topics[1] if len(topics) > 1 else None),
            recipient=decode_indexed_address(topics[2] if len(topics) > 2 else None),
            amount0_raw=amount0,
            amount1_raw=amount1,
            sqrt_price_x96=sqrt_price_x96,
            liquidity=liquidity,
            tick=tick,
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
        """Collect V3 ``Mint`` and ``Burn`` logs for a bounded block range."""
        fetcher = LogFetcher(self.provider, self.log_chunk_size)
        result = fetcher.fetch(
            address=pool.pool_address,
            topics=[[event_topic(MINT_EVENT), event_topic(BURN_EVENT)]],
            from_block=from_block,
            to_block=to_block,
        )
        mint_topic = event_topic(MINT_EVENT)
        burn_topic = event_topic(BURN_EVENT)
        events: list[LiquidityEvent] = []
        for log in sort_logs(result.logs):
            topic0 = (log.get("topics") or [None])[0]
            if isinstance(topic0, str):
                topic0 = topic0.lower()
            if topic0 == mint_topic:
                event = self._parse_mint_log(pool, log)
            elif topic0 == burn_topic:
                event = self._parse_burn_log(pool, log)
            else:
                continue
            if event is not None:
                events.append(event)
        return events

    def _parse_mint_log(self, pool: Pool, log: dict[str, Any]) -> LiquidityEvent | None:
        """Decode a V3 ``Mint`` log.

        ``sender`` is the only non-indexed address; ``owner`` and both tick
        bounds arrive as indexed topics.
        """
        try:
            data = log.get("data", "0x")
            sender = decode_address(data, 0)
            amount = decode_uint(data, 1)
            amount0 = decode_uint(data, 2)
            amount1 = decode_uint(data, 3)
        except (ValueError, IndexError):
            logger.warning(
                "discarding malformed Mint log",
                extra={"event": "liquidity_events.malformed", "dex": self.dex_id},
            )
            return None
        return self._liquidity_event(
            pool=pool,
            log=log,
            event_type="mint",
            sender=sender,
            amount=amount,
            amount0=amount0,
            amount1=amount1,
        )

    def _parse_burn_log(self, pool: Pool, log: dict[str, Any]) -> LiquidityEvent | None:
        """Decode a V3 ``Burn`` log (owner and tick bounds are indexed)."""
        try:
            data = log.get("data", "0x")
            amount = decode_uint(data, 0)
            amount0 = decode_uint(data, 1)
            amount1 = decode_uint(data, 2)
        except (ValueError, IndexError):
            logger.warning(
                "discarding malformed Burn log",
                extra={"event": "liquidity_events.malformed", "dex": self.dex_id},
            )
            return None
        return self._liquidity_event(
            pool=pool,
            log=log,
            event_type="burn",
            sender=None,
            amount=amount,
            amount0=amount0,
            amount1=amount1,
        )

    def _liquidity_event(
        self,
        *,
        pool: Pool,
        log: dict[str, Any],
        event_type: str,
        sender: str | None,
        amount: int,
        amount0: int,
        amount1: int,
    ) -> LiquidityEvent:
        topics = log.get("topics") or []
        block_number = int(log.get("blockNumber", "0x0"), 16)
        return LiquidityEvent(
            chain_id=self.chain_id,
            dex_id=self.dex_id,
            pool_address=pool.pool_address,
            pool_type=PoolType.CONCENTRATED_LIQUIDITY,
            event_type=event_type,
            block_number=block_number,
            block_hash=log.get("blockHash"),
            block_timestamp=self.block_timestamp(block_number),
            transaction_hash=log.get("transactionHash", ""),
            log_index=log_index_of(log),
            owner=decode_indexed_address(topics[1] if len(topics) > 1 else None),
            sender=sender,
            amount0_raw=amount0,
            amount1_raw=amount1,
            amount_raw=amount,
            tick_lower=_decode_indexed_int(topics[2] if len(topics) > 2 else None),
            tick_upper=_decode_indexed_int(topics[3] if len(topics) > 3 else None),
            observed_at=utc_now_iso(),
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


def _decode_indexed_int(topic: str | None) -> int | None:
    """Decode an indexed ``int24``-style topic into a signed integer."""
    if not topic or not isinstance(topic, str):
        return None
    try:
        value = int(topic, 16)
    except ValueError:
        return None
    return value - (1 << 256) if value >= (1 << 255) else value
