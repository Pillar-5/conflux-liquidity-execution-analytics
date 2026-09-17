"""Reproducible reporting: datasets gathered from the database.

Every value written by this package is read back from SQLite. Nothing is typed
in by hand and nothing is synthesised for a market that was not collected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..storage.database import connect
from ..storage.repositories import CatalogRepository, ObservationRepository, RunRepository


@dataclass
class AnalyticsDataset:
    """One reproducible snapshot of everything the report needs."""

    settings: Settings
    chain_id: int
    pool_states: list[dict[str, Any]]
    execution: list[dict[str, Any]]
    swaps: list[dict[str, Any]]
    liquidity_events: list[dict[str, Any]]
    pools: list[dict[str, Any]]
    tokens: list[dict[str, Any]]
    dexes: list[dict[str, Any]]
    runs: list[dict[str, Any]]

    @property
    def observed_at_range(self) -> tuple[str | None, str | None]:
        stamps = [
            row.get("observed_at")
            for row in self.pool_states + self.execution
            if row.get("observed_at")
        ]
        if not stamps:
            return (None, None)
        return (min(stamps), max(stamps))

    @property
    def block_range(self) -> tuple[int | None, int | None]:
        blocks = [
            int(row["block_number"])
            for row in self.pool_states + self.execution + self.swaps
            if row.get("block_number") is not None
        ]
        if not blocks:
            return (None, None)
        return (min(blocks), max(blocks))


def load_dataset(settings: Settings, *, limit: int = 20_000) -> AnalyticsDataset:
    """Read all report inputs from the analytics database."""
    conn = connect(settings.db_path)
    try:
        catalog = CatalogRepository(conn)
        observations = ObservationRepository(conn)
        runs = RunRepository(conn)
        chain_id = settings.network.chain_id
        return AnalyticsDataset(
            settings=settings,
            chain_id=chain_id,
            pool_states=observations.pool_states(limit=limit),
            execution=observations.execution_observations(limit=limit),
            swaps=observations.swap_events(limit=limit),
            liquidity_events=observations.liquidity_events(limit=limit),
            pools=catalog.list_pools(chain_id=chain_id),
            tokens=catalog.list_tokens(chain_id=chain_id),
            dexes=catalog.list_dexes(chain_id=chain_id),
            runs=[run.as_dict() for run in runs.list_runs(limit=200)],
        )
    finally:
        conn.close()


def quality_summary(dataset: AnalyticsDataset) -> dict[str, Any]:
    """Data-quality statistics derived strictly from stored observations."""
    execution_total = len(dataset.execution)
    execution_success = sum(1 for row in dataset.execution if row.get("success"))
    impact_available = sum(
        1 for row in dataset.execution if row.get("price_impact_bps") is not None
    )
    gas_available = sum(
        1 for row in dataset.execution if row.get("gas_cost_native_wei") is not None
    )
    fee_available = sum(1 for row in dataset.execution if row.get("fee_bps") is not None)
    statuses: dict[str, int] = {}
    for row in dataset.pool_states:
        key = row.get("data_status") or "unknown"
        statuses[key] = statuses.get(key, 0) + 1
    methods: dict[str, int] = {}
    for row in dataset.execution:
        key = row.get("quote_method") or "unknown"
        methods[key] = methods.get(key, 0) + 1
    return {
        "pool_state_observations": len(dataset.pool_states),
        "execution_observations": execution_total,
        "execution_successes": execution_success,
        "execution_success_rate": (execution_success / execution_total)
        if execution_total
        else None,
        "price_impact_available": impact_available,
        "gas_cost_available": gas_available,
        "fee_available": fee_available,
        "pool_state_data_status": statuses,
        "quote_methods": methods,
        "swap_events": len(dataset.swaps),
        "liquidity_events": len(dataset.liquidity_events),
        "markets_known": len(dataset.pools),
        "tokens_known": len(dataset.tokens),
        "dexes_known": len(dataset.dexes),
    }