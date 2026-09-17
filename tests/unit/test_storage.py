"""Database schema, persistence, latest-run selection and checkpoints."""

from __future__ import annotations

from pathlib import Path

import pytest

from conflux_analytics.collectors.checkpoints import CheckpointRepository
from conflux_analytics.models.common import DataStatus, PoolType, Provenance
from conflux_analytics.models.events import SwapEvent
from conflux_analytics.models.market import MarketState
from conflux_analytics.models.pool import Pool
from conflux_analytics.models.token import Token
from conflux_analytics.storage.database import connect
from conflux_analytics.storage.migrations import ALL_MIGRATIONS, schema_version
from conflux_analytics.storage.repositories import (
    CatalogRepository,
    ObservationRepository,
    RunRepository,
)

CHAIN_ID = 1030
TOKEN0 = "0x" + "11" * 20
TOKEN1 = "0x" + "22" * 20
POOL = "0x" + "33" * 20


@pytest.fixture()
def conn(tmp_path: Path):
    connection = connect(tmp_path / "analytics.db")
    yield connection
    connection.close()


def make_pool() -> Pool:
    return Pool(
        chain_id=CHAIN_ID,
        pool_address=POOL,
        dex_id="swappi_v2",
        token0_address=TOKEN0,
        token1_address=TOKEN1,
        pool_type=PoolType.CONSTANT_PRODUCT,
        fee_bps=30,
        discovery_source="factory_getPair",
        verification_method="factory.getPair == pool",
    )


def make_state(run_id: str, block: int = 100, observed_at: str = "2026-09-15T00:00:00Z") -> MarketState:
    return MarketState(
        chain_id=CHAIN_ID,
        dex_id="swappi_v2",
        pool_address=POOL,
        pool_type=PoolType.CONSTANT_PRODUCT,
        token0=Token(chain_id=CHAIN_ID, address=TOKEN0, symbol="WCFX", decimals=18),
        token1=Token(chain_id=CHAIN_ID, address=TOKEN1, symbol="USDT", decimals=6),
        block_number=block,
        observed_at=observed_at,
        run_id=run_id,
        reserve0_raw=1_000_000 * 10**18,
        reserve1_raw=250_000 * 10**6,
        fee_bps=30,
        data_status=DataStatus.VERIFIED,
    )


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------
def test_migrations_apply_in_order_and_are_idempotent(conn) -> None:
    assert schema_version(conn) == max(m.version for m in ALL_MIGRATIONS)
    from conflux_analytics.storage.migrations import migrate

    assert migrate(conn) == []


def test_expected_tables_exist(conn) -> None:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    names = {row["name"] for row in rows}
    for table in (
        "dexes",
        "tokens",
        "pools",
        "pool_states",
        "swap_observations",
        "swap_events",
        "liquidity_events",
        "gas_observations",
        "prices",
        "collection_runs",
        "collection_checkpoints",
        "provider_health",
        "analytics_results",
    ):
        assert table in names


def test_raw_quantities_are_stored_as_integer_strings(conn) -> None:
    runs = RunRepository(conn)
    catalog = CatalogRepository(conn)
    observations = ObservationRepository(conn)
    runs.start_run("r1", chain_id=CHAIN_ID, rpc_url=None, trigger="test")
    catalog.upsert_pool(make_pool())
    observations.insert_pool_state(make_state("r1"))

    row = conn.execute("SELECT reserve0_raw, reserve1_raw FROM pool_states").fetchone()
    assert isinstance(row["reserve0_raw"], str)
    assert row["reserve0_raw"] == str(1_000_000 * 10**18)
    assert row["reserve1_raw"] == str(250_000 * 10**6)


def test_pool_state_keeps_provenance(conn) -> None:
    observations = ObservationRepository(conn)
    RunRepository(conn).start_run("r1", chain_id=CHAIN_ID, rpc_url=None, trigger="test")
    state = MarketState(
        chain_id=CHAIN_ID,
        dex_id="swappi_v2",
        pool_address=POOL,
        pool_type=PoolType.CONSTANT_PRODUCT,
        token0=Token(chain_id=CHAIN_ID, address=TOKEN0, decimals=18),
        token1=Token(chain_id=CHAIN_ID, address=TOKEN1, decimals=6),
        block_number=100,
        block_hash="0x" + "ab" * 32,
        observed_at="2026-09-15T00:00:00Z",
        run_id="r1",
        reserve0_raw=10**18,
        reserve1_raw=10**6,
        provenance=Provenance(
            source="rpc_eth_call",
            method="getReserves()",
            chain_id=CHAIN_ID,
            block_number=100,
            contract_address=POOL,
        ),
    )
    observations.insert_pool_state(state)
    row = conn.execute("SELECT provenance_json FROM pool_states").fetchone()
    assert "getReserves()" in row["provenance_json"]


# ---------------------------------------------------------------------------
# catalog persistence
# ---------------------------------------------------------------------------
def test_pool_upsert_is_idempotent_and_filters_work(conn) -> None:
    catalog = CatalogRepository(conn)
    catalog.upsert_pool(make_pool())
    catalog.upsert_pool(make_pool())
    assert len(catalog.list_pools(chain_id=CHAIN_ID)) == 1
    assert catalog.list_pools(chain_id=CHAIN_ID, dex_id="swappi_v2")
    assert catalog.list_pools(chain_id=CHAIN_ID, dex_id="vswap_v3") == []
    assert catalog.list_pools(chain_id=CHAIN_ID, token=TOKEN1)
    assert catalog.get_pool(POOL.upper()) is not None


def test_token_upsert_keeps_nullable_metadata(conn) -> None:
    catalog = CatalogRepository(conn)
    catalog.upsert_token(Token(chain_id=CHAIN_ID, address=TOKEN0, symbol="WCFX", decimals=18))
    catalog.upsert_token(Token(chain_id=CHAIN_ID, address=TOKEN1))
    rows = {row["address"]: row for row in catalog.list_tokens(chain_id=CHAIN_ID)}
    assert rows[TOKEN0]["decimals"] == 18
    assert rows[TOKEN1]["decimals"] is None
    assert rows[TOKEN1]["symbol"] is None


def test_swap_events_are_deduplicated_by_primary_key(conn) -> None:
    observations = ObservationRepository(conn)
    event = SwapEvent(
        chain_id=CHAIN_ID,
        dex_id="swappi_v2",
        pool_address=POOL,
        pool_type=PoolType.CONSTANT_PRODUCT,
        block_number=100,
        transaction_hash="0x" + "cd" * 32,
        log_index=3,
        amount0_raw=10**18,
        amount1_raw=-(250 * 10**6),
        observed_at="2026-09-15T00:00:00Z",
    )
    assert observations.insert_swap_events([event]) == 1
    assert observations.insert_swap_events([event]) == 0
    assert len(observations.swap_events(pool_address=POOL)) == 1


# ---------------------------------------------------------------------------
# run lifecycle and the latest-run concept
# ---------------------------------------------------------------------------
def test_latest_run_ignores_running_and_failed_runs(conn) -> None:
    runs = RunRepository(conn)
    runs.start_run("run-1", chain_id=CHAIN_ID, rpc_url=None, trigger="test")
    runs.complete_run("run-1", status="completed", markets_processed=1)
    runs.start_run("run-2", chain_id=CHAIN_ID, rpc_url=None, trigger="test")
    runs.complete_run("run-2", status="failed")
    runs.start_run("run-3", chain_id=CHAIN_ID, rpc_url=None, trigger="test")

    latest = runs.latest_run()
    assert latest is not None and latest.run_id == "run-1"
    assert runs.latest_run(successful_only=False).run_id == "run-3"
    assert len(runs.list_runs()) == 3


def test_completed_with_errors_still_counts_as_the_latest_run(conn) -> None:
    runs = RunRepository(conn)
    runs.start_run("run-1", chain_id=CHAIN_ID, rpc_url=None, trigger="test")
    runs.complete_run("run-1", status="completed")
    runs.start_run("run-2", chain_id=CHAIN_ID, rpc_url=None, trigger="test")
    runs.complete_run("run-2", status="completed_with_errors", errors=2)

    latest = runs.latest_run()
    assert latest is not None
    assert latest.run_id == "run-2"
    assert latest.status == "completed_with_errors"
    assert latest.errors == 2


def test_old_observations_are_not_returned_as_the_latest_run(conn) -> None:
    """A stale observation must not masquerade as current market state."""
    runs = RunRepository(conn)
    observations = ObservationRepository(conn)
    runs.start_run("old", chain_id=CHAIN_ID, rpc_url=None, trigger="test")
    runs.complete_run("old", status="completed")
    observations.insert_pool_state(make_state("old", block=10))
    runs.start_run("new", chain_id=CHAIN_ID, rpc_url=None, trigger="test")
    runs.complete_run("new", status="completed")
    observations.insert_pool_state(make_state("new", block=20))

    latest_run_id = runs.latest_run().run_id
    current = observations.pool_states(run_id=latest_run_id)
    assert [row["block_number"] for row in current] == [20]
    everything = observations.pool_states()
    assert len(everything) == 2


# ---------------------------------------------------------------------------
# checkpoints
# ---------------------------------------------------------------------------
def test_checkpoint_round_trip_and_resume(conn) -> None:
    checkpoints = CheckpointRepository(conn, CHAIN_ID)
    assert checkpoints.get("swappi_v2", "swap_events") is None
    checkpoints.save("swappi_v2", "swap_events", 1_000)
    assert checkpoints.get("swappi_v2", "swap_events") == 1_000
    checkpoints.save("swappi_v2", "swap_events", 1_500)
    assert checkpoints.get("swappi_v2", "swap_events") == 1_500
    assert checkpoints.get("swappi_v2", "liquidity_events") is None
