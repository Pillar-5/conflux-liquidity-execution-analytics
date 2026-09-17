"""Collection/analytics pipeline orchestrator.

This module ties the adapters, collectors, analytics engine and storage into a
single deterministic pipeline. Every run is recorded in ``collection_runs`` and
every market failure is attributed, so the CLI, API and reports can all explain
what happened and why.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .analytics.execution import ExecutionAnalyzer
from .analytics.gas import estimate_route_gas
from .chain.blocks import resolve_block_range
from .collectors.checkpoints import CheckpointRepository
from .collectors.discovery import discover_for_adapter
from .collectors.market_state import collect_states_for_adapter
from .collectors.swaps import collect_events_for_adapter
from .config import Settings
from .dex.base import DexAdapter
from .dex.registry import build_enabled_adapters
from .errors import ConfluxAnalyticsError, error_category
from .logging_setup import get_logger
from .models.common import DataStatus, PoolType
from .models.pool import Pool
from .rpc.provider import RpcProvider
from .storage.database import connect as connect_database
from .storage.repositories import CatalogRepository, ObservationRepository, RunRepository

if TYPE_CHECKING:  # annotations only; avoids import cycles at runtime
    from .models.execution import Quote
    from .models.market import MarketState

logger = get_logger(__name__)


def _run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


@dataclass
class PipelineResult:
    """Outcome of one collection run, persisted and returned."""

    run_id: str
    status: str
    chain_id: int
    start_block: int | None = None
    end_block: int | None = None
    markets_processed: int = 0
    states_collected: int = 0
    execution_observations: int = 0
    execution_successes: int = 0
    swaps_persisted: int = 0
    liquidity_events_persisted: int = 0
    blocks_processed: int = 0
    error_count: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)
    dexes: dict[str, dict[str, Any]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "chain_id": self.chain_id,
            "start_block": self.start_block,
            "end_block": self.end_block,
            "markets_processed": self.markets_processed,
            "states_collected": self.states_collected,
            "execution_observations": self.execution_observations,
            "execution_successes": self.execution_successes,
            "swaps_persisted": self.swaps_persisted,
            "liquidity_events_persisted": self.liquidity_events_persisted,
            "blocks_processed": self.blocks_processed,
            "error_count": self.error_count,
            "errors": list(self.errors),
            "dexes": dict(self.dexes),
        }


class AnalyticsPipeline:
    """Orchestrates verification, discovery, collection and analytics."""

    def __init__(self, settings: Settings, provider: RpcProvider) -> None:
        self.settings = settings
        self.provider = provider
        self.conn = connect_database(settings.db_path)
        self.runs = RunRepository(self.conn)
        self.catalog = CatalogRepository(self.conn)
        self.observations = ObservationRepository(self.conn)
        self.checkpoints = CheckpointRepository(self.conn, settings.network.chain_id)

    def close(self) -> None:
        self.conn.close()
        self.provider.close()

    # -- network -----------------------------------------------------------
    def check_network(self) -> dict[str, Any]:
        """Provider health check, persisted into ``provider_health``."""
        health = self.provider.health()
        with self.conn:
            self.conn.execute(
                "INSERT INTO provider_health (rpc_url, reachable, status, latency_ms,"
                " chain_id_observed, chain_id_expected, chain_matches, latest_block,"
                " eth_call_ok, detail, checked_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    health.rpc_url,
                    1 if health.reachable else 0,
                    health.status,
                    health.latency_ms,
                    health.chain_id_observed,
                    health.chain_id_expected,
                    1 if health.chain_matches else 0,
                    health.latest_block,
                    1 if health.eth_call_ok else 0,
                    health.detail,
                    health.checked_at,
                ),
            )
        return health.as_dict()

    # -- main entry --------------------------------------------------------
    def run_collection(
        self,
        *,
        dex_ids: list[str] | None = None,
        start_block: int | None = None,
        end_block: int | None = None,
        collect_events: bool = True,
        with_execution: bool = True,
        trigger: str = "cli",
    ) -> PipelineResult:
        """Execute a full collection run and persist its result."""
        network = self.check_network()
        chain_id = self.settings.network.chain_id
        if not network["chain_matches"] or not network["reachable"]:
            raise ConfluxAnalyticsError(
                f"RPC unhealthy before collection: {network['status']} ({network.get('detail')})"
            )
        head = network["latest_block"]
        run_id = _run_id()
        self.runs.start_run(
            run_id,
            chain_id=chain_id,
            rpc_url=self.provider.rpc_url,
            trigger=trigger,
            config_json=json.dumps(
                {
                    "dex_ids": dex_ids or "all-enabled",
                    "start_block": start_block,
                    "end_block": end_block,
                    "collect_events": collect_events,
                    "with_execution": with_execution,
                }
            ),
        )
        result = PipelineResult(run_id=run_id, status="running", chain_id=chain_id)
        adapters = build_enabled_adapters(self.settings, self.provider, only=dex_ids)
        event_range: tuple[int, int] | None = None
        if collect_events:
            rng = resolve_block_range(
                head=head,
                start_block=start_block,
                end_block=end_block,
                default_lookback_blocks=self.settings.collector.default_lookback_blocks,
                max_blocks_per_run=self.settings.collector.max_blocks_per_run,
                chunk_size=self.settings.collector.log_chunk_size,
            )
            event_range = (rng.start, rng.end)
            result.start_block, result.end_block = event_range
        try:
            for adapter in adapters:
                self._run_adapter(adapter, result, event_range, with_execution)
        except ConfluxAnalyticsError as exc:
            result.status = "failed"
            result.errors.append({"category": error_category(exc), "detail": exc.message})
            logger.error("run failed", extra={"event": "run.failed", "run_id": run_id})
        status = "failed" if result.status == "failed" else (
            "completed" if result.error_count == 0 else "completed_with_errors"
        )
        result.status = status
        self.runs.complete_run(
            run_id,
            status=status,
            start_block=result.start_block,
            end_block=result.end_block,
            blocks_processed=result.blocks_processed,
            markets_processed=result.markets_processed,
            observations_created=(result.states_collected + result.execution_observations),
            errors=result.error_count,
            error_summary=json.dumps(result.errors)[:2000] if result.errors else None,
        )
        logger.info(
            "run complete",
            extra={
                "event": "run.complete",
                "run_id": run_id,
                "status": status,
                "markets_processed": result.markets_processed,
                "observations_created": result.states_collected + result.execution_observations,
            },
        )
        return result

    # -- discovery entry -----------------------------------------------------
    def discover(self, dex_ids: list[str] | None = None) -> dict[str, Any]:
        """Run pool discovery for the enabled adapters and register the DEXs."""
        adapters = build_enabled_adapters(self.settings, self.provider, only=dex_ids)
        tracked = self.settings.tracked_token_addresses()
        summary: dict[str, Any] = {"dexes": {}, "errors": []}
        for adapter in adapters:
            self._register_dex(adapter, summary)
            discovery = discover_for_adapter(
                adapter,
                tracked,
                self.catalog,
                max_pools=self.settings.collector.max_pools,
            )
            summary["dexes"][adapter.dex_id] = discovery.as_dict()
            summary["errors"].extend(discovery.errors)
        logger.info(
            "discovery run complete",
            extra={
                "event": "discovery.run_complete",
                "dexes": list(summary["dexes"]),
                "error_count": len(summary["errors"]),
            },
        )
        return summary

    def _register_dex(self, adapter: DexAdapter, summary: dict[str, Any]) -> None:
        """Verify the adapter's contracts on-chain, then record the DEX row."""
        identity = adapter.identify()
        try:
            checks = adapter.verify_contracts()
        except ConfluxAnalyticsError as exc:
            summary["errors"].append(
                {"dex": adapter.dex_id, "category": error_category(exc), "detail": exc.message}
            )
            checks = []
        failed = [c for c in checks if not c.ok]
        verified = bool(checks) and not failed
        if failed:
            detail = "; ".join(
                f"{c.contract_key}:{c.check} expected {c.expected} observed {c.observed}"
                for c in failed
            )
        else:
            detail = (
                "; ".join(f"{c.contract_key}:{c.check}=ok" for c in checks)
                or adapter.definition.documentation
            )
        try:
            self.catalog.upsert_dex(
                chain_id=self.settings.network.chain_id,
                dex_id=adapter.dex_id,
                name=adapter.name,
                pool_type=adapter.pool_type.value,
                enabled=adapter.definition.enabled,
                documentation=adapter.definition.documentation,
                contracts_json=json.dumps(identity["contracts"]),
                discovery_method=identity["discovery"]["method"],
                quoting_method=identity["quoting_method"],
                verified=verified,
                verified_detail=detail,
            )
        except ConfluxAnalyticsError as exc:
            summary["errors"].append(
                {"dex": adapter.dex_id, "category": error_category(exc), "detail": exc.message}
            )
        if not verified:
            summary["errors"].append(
                {
                    "dex": adapter.dex_id,
                    "category": "verification",
                    "detail": f"contract verification failed: {detail}",
                }
            )

    # -- per-adapter collection ----------------------------------------------
    def _run_adapter(
        self,
        adapter: DexAdapter,
        result: PipelineResult,
        event_range: tuple[int, int] | None,
        with_execution: bool,
    ) -> None:
        """Load pools, collect state, run analytics and events for one DEX."""
        dex_summary: dict[str, Any] = {
            "dex_id": adapter.dex_id,
            "pool_type": adapter.pool_type.value,
            "capabilities": adapter.get_supported_features(),
            "states_collected": 0,
            "states_failed": 0,
            "execution_observations": 0,
            "execution_successes": 0,
            "swaps_persisted": 0,
            "liquidity_events_persisted": 0,
            "errors": [],
        }
        run_id = result.run_id
        chain_id = self.settings.network.chain_id
        pool_rows = self.catalog.list_pools(chain_id=chain_id, dex_id=adapter.dex_id, active_only=True)
        pools = [_pool_from_row(row, adapter) for row in pool_rows]
        dex_summary["pools_loaded"] = len(pools)
        if not pools:
            dex_summary["detail"] = (
                "no pools in catalog for this DEX; run 'conflux-analytics discover' first"
            )
            result.dexes[adapter.dex_id] = dex_summary
            return

        # Market state (always).
        states, state_result = collect_states_for_adapter(
            adapter, pools, self.observations, block="latest", run_id=run_id
        )
        result.states_collected += state_result.states_collected
        result.markets_processed += len(pools)
        dex_summary["states_collected"] = state_result.states_collected
        dex_summary["states_failed"] = state_result.states_failed
        dex_summary["average_state_latency_ms"] = state_result.average_latency_ms
        dex_summary["errors"].extend(state_result.errors)
        result.errors.extend({"dex": adapter.dex_id, **error} for error in state_result.errors)
        result.error_count += len(state_result.errors)

        # Execution analytics (when the adapter can quote and state was read).
        if with_execution and states:
            execution = self._run_execution(adapter, pools, states, run_id)
            dex_summary["execution_observations"] = execution["observations"]
            dex_summary["execution_successes"] = execution["successes"]
            result.execution_observations += execution["observations"]
            result.execution_successes += execution["successes"]
            result.error_count += execution["failures"] + len(execution["errors"])
            dex_summary["errors"].extend(execution["errors"])
            result.errors.extend({"dex": adapter.dex_id, **e} for e in execution["errors"])

        # Historical events (bounded range).
        if event_range is not None:
            events = collect_events_for_adapter(
                adapter,
                pools,
                self.observations,
                from_block=event_range[0],
                to_block=event_range[1],
                run_id=run_id,
            )
            result.swaps_persisted += events.swaps_persisted
            result.liquidity_events_persisted += events.liquidity_events_persisted
            result.blocks_processed += event_range[1] - event_range[0] + 1
            dex_summary["swaps_persisted"] = events.swaps_persisted
            dex_summary["liquidity_events_persisted"] = events.liquidity_events_persisted
            dex_summary["event_skips"] = events.skipped
            dex_summary["errors"].extend(events.errors)
            result.errors.extend({"dex": adapter.dex_id, **e} for e in events.errors)
            result.error_count += len(events.errors)

        result.dexes[adapter.dex_id] = dex_summary

    def _run_execution(
        self,
        adapter: DexAdapter,
        pools: list[Pool],
        states: list[MarketState],
        run_id: str,
    ) -> dict[str, Any]:
        """Run execution analytics for every pool that has a fresh state.

        Quotes and gas estimation come from the adapter's own interfaces;
        failures are recorded per pool and never abort the run.
        """
        summary: dict[str, Any] = {
            "observations": 0,
            "successes": 0,
            "failures": 0,
            "errors": [],
        }
        trade_sizes = self.settings.trade_sizes
        if not trade_sizes:
            summary["errors"].append(
                {"pool": "*", "category": "configuration", "detail": "no trade sizes configured"}
            )
            return summary

        if isinstance(states, dict):
            states_by_pool = dict(states)
        else:
            states_by_pool = {s.pool_address: s for s in states}
        gas_supported = bool(
            adapter.get_supported_features().get("supports_gas_estimation", False)
        )
        for pool in pools:
            state = states_by_pool.get(pool.pool_address)
            if state is None:
                continue

            def quote_fn(
                token_in: str,
                token_out: str,
                amount_in_raw: int,
                block: int,
                _pool: Pool = pool,
            ) -> Quote:
                return adapter.get_quote(_pool, token_in, token_out, amount_in_raw, block)

            analyzer = ExecutionAnalyzer(
                chain_id=self.settings.network.chain_id,
                dex_id=adapter.dex_id,
                pool_address=pool.pool_address,
                pool_type=pool.pool_type,
                gas_estimator=(
                    (lambda tx: estimate_route_gas(self.provider, tx)) if gas_supported else None
                ),
                gas_tx_builder=(
                    _adapter_gas_tx_builder(adapter, pool) if gas_supported else None
                ),
                gas_price_provider=self.provider,
            )
            try:
                analyses = analyzer.analyze_pool(state, quote_fn, trade_sizes, run_id=run_id)
            except ConfluxAnalyticsError as exc:
                summary["errors"].append(
                    {
                        "pool": pool.pool_address,
                        "category": error_category(exc),
                        "detail": exc.message,
                    }
                )
                continue
            for analysis in analyses:
                summary["errors"].extend(
                    {
                        "pool": pool.pool_address,
                        "category": "skipped",
                        "detail": f"{skip.get('reason')}: {skip.get('detail')}",
                    }
                    for skip in analysis.skipped
                )
                for observation in analysis.observations:
                    try:
                        self.observations.insert_execution_observation(observation)
                    except ConfluxAnalyticsError as exc:
                        summary["errors"].append(
                            {
                                "pool": pool.pool_address,
                                "category": error_category(exc),
                                "detail": f"persist failed: {exc.message}",
                            }
                        )
                        continue
                    summary["observations"] += 1
                    if observation.success:
                        summary["successes"] += 1
                    else:
                        summary["failures"] += 1
        return summary


def _pool_from_row(row: dict[str, Any], adapter: DexAdapter) -> Pool:
    """Rebuild a :class:`Pool` from a catalog row for one adapter."""
    _ = adapter  # reserved for adapter-specific row interpretation
    return Pool(
        chain_id=int(row["chain_id"]),
        pool_address=str(row["pool_address"]).lower(),
        dex_id=str(row["dex_id"]),
        token0_address=str(row["token0_address"]).lower(),
        token1_address=str(row["token1_address"]).lower(),
        pool_type=PoolType(str(row["pool_type"])),
        fee_bps=row["fee_bps"],
        tick_spacing=row["tick_spacing"],
        fee_tier_raw=row.get("fee_tier_raw") if isinstance(row, dict) else row["fee_tier_raw"],
        discovery_source=str(row["discovery_source"]),
        verified_at=row["last_verified_at"],
        active=bool(row["active"]),
        status=DataStatus.VERIFIED if row["verified"] else DataStatus.PARTIAL,
    )


def _adapter_gas_tx_builder(
    adapter: DexAdapter, pool: Pool
) -> Callable[[str, str, int], dict[str, Any] | None]:
    """Wrap the adapter's transaction builder with the pool bound."""

    def build(token_in: str, token_out: str, amount_in_raw: int) -> dict[str, Any] | None:
        return adapter.build_gas_transaction(pool, token_in, token_out, amount_in_raw)

    return build

