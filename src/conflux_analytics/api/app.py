"""FastAPI application exposing collected Conflux analytics data.

The API is read-only: it serves observations that exist in the SQLite
database and live provider health. It never fabricates data - when an
observation is missing the response says so explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, HTTPException, Query

from ..config import Settings, load_settings
from ..errors import ConfluxAnalyticsError
from ..logging_setup import configure_logging, get_logger
from ..models.common import seconds_since
from ..rpc.provider import RpcProvider
from ..storage.database import connect as connect_database
from ..storage.repositories import CatalogRepository, ObservationRepository, RunRepository

logger = get_logger(__name__)

#: A pool-state observation older than this is reported as ``stale``.
FRESHNESS_SECONDS = 15 * 60


def freshness(observed_at: str | None) -> dict[str, Any]:
    """Data-freshness block attached to observations."""
    age = seconds_since(observed_at)
    if age is None:
        return {"observed_at": observed_at, "data_age_seconds": None, "status": "unavailable"}
    status = "fresh" if age <= FRESHNESS_SECONDS else "stale"
    return {"observed_at": observed_at, "data_age_seconds": round(age), "status": status}


@dataclass
class ApiContext:
    """Everything a request handler needs."""

    settings: Settings
    provider: RpcProvider
    catalog: CatalogRepository
    observations: ObservationRepository
    runs: RunRepository


_context: ApiContext | None = None


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application."""
    global _context
    resolved = settings or load_settings()
    configure_logging(resolved.logging)
    provider = RpcProvider(
        rpc_url=resolved.network.rpc_url,
        chain_id=resolved.network.chain_id,
        timeout_seconds=resolved.network.rpc_timeout_seconds,
        max_retries=resolved.network.rpc_max_retries,
        retry_backoff_seconds=resolved.network.rpc_retry_backoff_seconds,
    )
    conn = connect_database(resolved.db_path)
    _context = ApiContext(
        settings=resolved,
        provider=provider,
        catalog=CatalogRepository(conn),
        observations=ObservationRepository(conn),
        runs=RunRepository(conn),
    )
    app = FastAPI(
        title="Conflux Liquidity & Execution Analytics",
        description=(
            "Read-only analytics API over collected Conflux eSpace market data. "
            "All values carry explicit data-status and freshness metadata."
        ),
        version="0.1.0",
    )
    _register_routes(app, _context)
    if resolved.dashboard.enabled:
        from .dashboard import mount_dashboard

        mount_dashboard(app, resolved, _context)
    return app


def _register_routes(app: FastAPI, ctx: ApiContext) -> None:  # noqa: C901
    chain_id = ctx.settings.network.chain_id

    # ------------------------------------------------------------ health
    @app.get("/health", tags=["system"])
    def health() -> dict[str, Any]:
        latest = ctx.runs.latest_run(successful_only=False)
        return {
            "status": "ok",
            "chain_id": chain_id,
            "database": {"path": str(ctx.settings.db_path)},
            "latest_run": latest.as_dict() if latest else None,
        }

    # ------------------------------------------------------------ network
    @app.get("/api/network", tags=["network"])
    def network() -> dict[str, Any]:
        return {
            "chain_id": chain_id,
            "name": ctx.settings.network.name,
            "rpc_url": ctx.settings.network.rpc_url,
            "explorer_url": ctx.settings.network.explorer_url,
        }

    @app.get("/api/provider", tags=["network"])
    def provider_health() -> dict[str, Any]:
        try:
            return ctx.provider.health().as_dict()
        except ConfluxAnalyticsError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/status", tags=["system"])
    def status() -> dict[str, Any]:
        pools = ctx.catalog.list_pools(chain_id=chain_id, active_only=True)
        dexes = ctx.catalog.list_dexes(chain_id=chain_id)
        latest = ctx.runs.latest_run()
        run_id = latest.run_id if latest else None
        markets_with_state = 0
        if run_id:
            states = ctx.observations.pool_states(run_id=run_id, limit=10_000)
            markets_with_state = len({row["pool_address"] for row in states})
        return {
            "chain_id": chain_id,
            "dexes_registered": len(dexes),
            "active_markets": len(pools),
            "markets_with_state_in_latest_run": markets_with_state,
            "latest_run": latest.as_dict() if latest else None,
        }

    @app.get("/api/metrics", tags=["system"])
    def metrics() -> dict[str, Any]:
        latest = ctx.runs.latest_run()
        pools = ctx.catalog.list_pools(chain_id=chain_id, active_only=True)
        tokens = ctx.catalog.list_tokens(chain_id=chain_id)
        executions = ctx.observations.execution_observations(limit=100_000)
        successes = sum(1 for row in executions if row.get("success"))
        return {
            "active_markets": len(pools),
            "known_tokens": len(tokens),
            "execution_observations": len(executions),
            "execution_success_rate": (successes / len(executions)) if executions else None,
            "latest_run": latest.as_dict() if latest else None,
        }

    # ------------------------------------------------------------ catalog
    @app.get("/api/dexes", tags=["catalog"])
    def dexes() -> list[dict[str, Any]]:
        return ctx.catalog.list_dexes(chain_id=chain_id)

    @app.get("/api/tokens", tags=["catalog"])
    def tokens() -> list[dict[str, Any]]:
        return ctx.catalog.list_tokens(chain_id=chain_id)

    @app.get("/api/pools", tags=["catalog"])
    def pools(
        dex: str | None = Query(None, description="Filter by dex_id"),
        token: str | None = Query(None, description="Filter by token address"),
    ) -> list[dict[str, Any]]:
        return ctx.catalog.list_pools(chain_id=chain_id, dex_id=dex, token=token)

    @app.get("/api/pools/{pool_address}", tags=["catalog"])
    def pool_detail(pool_address: str) -> dict[str, Any]:
        pool = ctx.catalog.get_pool(pool_address)
        if not pool:
            raise HTTPException(status_code=404, detail="pool not found in catalog")
        latest = ctx.runs.latest_run()
        run_id = latest.run_id if latest else None
        state = ctx.observations.latest_pool_state(pool_address, run_id=run_id)
        return {
            "pool": pool,
            "latest_state": state,
            "freshness": freshness(state.get("observed_at")) if state else freshness(None),
        }

    # ------------------------------------------------------------ markets
    @app.get("/api/markets", tags=["markets"])
    def markets(dex: str | None = Query(None)) -> list[dict[str, Any]]:
        """Latest market view: pool + newest pool state, never old state as current."""
        latest = ctx.runs.latest_run()
        run_id = latest.run_id if latest else None
        pools = ctx.catalog.list_pools(chain_id=chain_id, dex_id=dex, active_only=True)
        rows: list[dict[str, Any]] = []
        for pool in pools:
            address = pool["pool_address"]
            state = ctx.observations.latest_pool_state(address, run_id=run_id)
            rows.append(
                {
                    "pool": pool,
                    "state": state,
                    "freshness": freshness(state.get("observed_at")) if state else freshness(None),
                    "state_available": state is not None,
                }
            )
        return rows

    @app.get("/api/markets/{pool_address}", tags=["markets"])
    def market_detail(pool_address: str) -> dict[str, Any]:
        pool = ctx.catalog.get_pool(pool_address)
        if not pool:
            raise HTTPException(status_code=404, detail="pool not found in catalog")
        latest = ctx.runs.latest_run()
        run_id = latest.run_id if latest else None
        state = ctx.observations.latest_pool_state(pool_address, run_id=run_id)
        executions = ctx.observations.execution_observations(
            pool_address=pool_address, run_id=run_id, limit=500
        )
        return {
            "pool": pool,
            "state": state,
            "freshness": freshness(state.get("observed_at")) if state else freshness(None),
            "execution_observations": executions,
        }

    # ------------------------------------------------------------ liquidity
    @app.get("/api/liquidity", tags=["liquidity"])
    def liquidity(dex: str | None = Query(None)) -> list[dict[str, Any]]:
        latest = ctx.runs.latest_run()
        run_id = latest.run_id if latest else None
        pools = ctx.catalog.list_pools(chain_id=chain_id, dex_id=dex, active_only=True)
        rows: list[dict[str, Any]] = []
        for pool in pools:
            state = ctx.observations.latest_pool_state(pool["pool_address"], run_id=run_id)
            rows.append(
                {
                    "pool_address": pool["pool_address"],
                    "dex_id": pool.get("dex_id"),
                    "token0_symbol": pool.get("token0_symbol"),
                    "token1_symbol": pool.get("token1_symbol"),
                    "state": state,
                    "freshness": freshness(state.get("observed_at")) if state else freshness(None),
                }
            )
        return rows

    @app.get("/api/liquidity/{pool_address}", tags=["liquidity"])
    def liquidity_detail(pool_address: str) -> list[dict[str, Any]]:
        states = ctx.observations.pool_states(pool_address=pool_address, limit=1000)
        if not states:
            raise HTTPException(status_code=404, detail="no pool state observations recorded")
        return [{**state, "freshness": freshness(state.get("observed_at"))} for state in states]

    # ------------------------------------------------------------ execution
    @app.get("/api/execution", tags=["execution"])
    def execution(
        dex: str | None = Query(None),
        pool: str | None = Query(None),
        latest: bool = Query(True, description="Only the latest run's observations"),
        limit: int = Query(500, le=5000),
    ) -> list[dict[str, Any]]:
        latest_run_record = ctx.runs.latest_run() if latest else None
        rows = ctx.observations.execution_observations(
            dex_id=dex,
            pool_address=pool,
            run_id=latest_run_record.run_id if latest_run_record else None,
            limit=limit,
        )
        for row in rows:
            row["freshness"] = freshness(row.get("observed_at"))
        return rows

    @app.get("/api/execution/{pool_address}", tags=["execution"])
    def execution_detail(pool_address: str) -> list[dict[str, Any]]:
        latest_run_record = ctx.runs.latest_run()
        rows = ctx.observations.execution_observations(
            pool_address=pool_address,
            run_id=latest_run_record.run_id if latest_run_record else None,
            limit=1000,
        )
        if not rows:
            raise HTTPException(
                status_code=404,
                detail="no execution observations for this pool; run the collector first",
            )
        for row in rows:
            row["freshness"] = freshness(row.get("observed_at"))
        return rows

    # ------------------------------------------------------------ history
    @app.get("/api/swaps", tags=["history"])
    def swaps(
        dex: str | None = Query(None),
        pool: str | None = Query(None),
        limit: int = Query(200, le=5000),
    ) -> list[dict[str, Any]]:
        return ctx.observations.swap_events(dex_id=dex, pool_address=pool, limit=limit)

    @app.get("/api/history", tags=["history"])
    def history(
        pool: str | None = Query(None),
        dex: str | None = Query(None),
        start_block: int | None = Query(None),
        end_block: int | None = Query(None),
        limit: int = Query(1000, le=10_000),
    ) -> dict[str, Any]:
        """Historical observations: swaps and execution history (never current state)."""
        swaps = ctx.observations.swap_events(dex_id=dex, pool_address=pool, limit=limit)
        if start_block is not None:
            swaps = [s for s in swaps if (s.get("block_number") or 0) >= start_block]
        if end_block is not None:
            swaps = [s for s in swaps if (s.get("block_number") or 0) <= end_block]
        executions = ctx.observations.execution_observations(
            dex_id=dex, pool_address=pool, limit=limit
        )
        return {
            "swap_events": swaps,
            "execution_observations": executions,
            "note": "Historical observations only; current market state is under /api/markets.",
        }

    # ------------------------------------------------------------ runs
    @app.get("/api/runs", tags=["runs"])
    def runs(limit: int = Query(50, le=500)) -> list[dict[str, Any]]:
        return ctx.runs.list_runs(limit=limit)

    @app.get("/api/runs/latest", tags=["runs"])
    def latest_run() -> dict[str, Any]:
        run = ctx.runs.latest_run()
        if not run:
            raise HTTPException(status_code=404, detail="no collection run recorded yet")
        return run.as_dict()

