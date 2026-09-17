"""Schema migrations for the analytics database.

The schema is migration-based: every change is an ordered, idempotent script
recorded in ``schema_migrations``. Version 1 creates the full MVP schema.
Raw quantities are stored as INTEGER/TEXT integer strings - never as floats -
so that no on-chain value can lose precision in storage.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ..errors import MigrationError
from ..logging_setup import get_logger
from ..models.common import utc_now_iso

logger = get_logger(__name__)


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    statements: tuple[str, ...]


MIGRATION_001 = Migration(
    version=1,
    description="initial schema",
    statements=(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """,
        # ---------------------------------------------------------- runs
        """
        CREATE TABLE IF NOT EXISTS collection_runs (
            run_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL CHECK (status IN ('running','completed','failed')),
            trigger TEXT,
            chain_id INTEGER NOT NULL,
            rpc_url TEXT,
            start_block INTEGER,
            end_block INTEGER,
            blocks_processed INTEGER NOT NULL DEFAULT 0,
            markets_processed INTEGER NOT NULL DEFAULT 0,
            observations_created INTEGER NOT NULL DEFAULT 0,
            errors INTEGER NOT NULL DEFAULT 0,
            error_summary TEXT,
            config_json TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_runs_started ON collection_runs (started_at DESC)",
        # -------------------------------------------------------- dexes
        """
        CREATE TABLE IF NOT EXISTS dexes (
            chain_id INTEGER NOT NULL,
            dex_id TEXT NOT NULL,
            name TEXT NOT NULL,
            pool_type TEXT NOT NULL,
            enabled INTEGER NOT NULL,
            documentation TEXT,
            contracts_json TEXT NOT NULL,
            discovery_method TEXT,
            quoting_method TEXT,
            verified INTEGER NOT NULL DEFAULT 0,
            verified_detail TEXT,
            verified_at TEXT,
            PRIMARY KEY (chain_id, dex_id)
        )
        """,
        # -------------------------------------------------------- tokens
        """
        CREATE TABLE IF NOT EXISTS tokens (
            chain_id INTEGER NOT NULL,
            address TEXT NOT NULL,
            symbol TEXT,
            name TEXT,
            decimals INTEGER,
            total_supply_raw TEXT,
            metadata_status TEXT NOT NULL,
            metadata_detail TEXT,
            fetched_at TEXT,
            provenance_json TEXT,
            PRIMARY KEY (chain_id, address)
        )
        """,
        # --------------------------------------------------------- pools
        """
        CREATE TABLE IF NOT EXISTS pools (
            chain_id INTEGER NOT NULL,
            dex_id TEXT NOT NULL,
            pool_address TEXT NOT NULL,
            pool_type TEXT NOT NULL,
            token0_address TEXT,
            token1_address TEXT,
            token0_symbol TEXT,
            token1_symbol TEXT,
            token0_decimals INTEGER,
            token1_decimals INTEGER,
            fee_bps INTEGER,
            tick_spacing INTEGER,
            discovery_source TEXT NOT NULL,
            verified INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            first_seen_at TEXT NOT NULL,
            last_verified_at TEXT,
            metadata_json TEXT,
            PRIMARY KEY (chain_id, pool_address)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_pools_dex ON pools (chain_id, dex_id)",
        "CREATE INDEX IF NOT EXISTS idx_pools_tokens ON pools (token0_address, token1_address)",
    ),
)

MIGRATION_002 = Migration(
    version=2,
    description="state, observations and events",
    statements=(
        # ---------------------------------------------------- pool_states
        """
        CREATE TABLE IF NOT EXISTS pool_states (
            state_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            chain_id INTEGER NOT NULL,
            dex_id TEXT NOT NULL,
            pool_address TEXT NOT NULL,
            pool_type TEXT NOT NULL,
            block_number INTEGER NOT NULL,
            block_hash TEXT,
            observed_at TEXT NOT NULL,
            reserve0_raw TEXT,
            reserve1_raw TEXT,
            reserve0_dec TEXT,
            reserve1_dec TEXT,
            sqrt_price_x96 TEXT,
            tick INTEGER,
            liquidity TEXT,
            fee_bps INTEGER,
            data_status TEXT NOT NULL,
            detail TEXT,
            raw_state_json TEXT,
            source TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES collection_runs (run_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_states_pool ON pool_states (chain_id, pool_address, block_number DESC)",
        "CREATE INDEX IF NOT EXISTS idx_states_run ON pool_states (run_id)",
        # --------------------------------------------- swap_observations
        """
        CREATE TABLE IF NOT EXISTS swap_observations (
            observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            chain_id INTEGER NOT NULL,
            dex_id TEXT NOT NULL,
            pool_address TEXT NOT NULL,
            pool_type TEXT NOT NULL,
            input_token TEXT NOT NULL,
            output_token TEXT NOT NULL,
            trade_size TEXT NOT NULL,
            input_amount_raw TEXT NOT NULL,
            input_amount_dec TEXT,
            output_amount_raw TEXT,
            output_amount_dec TEXT,
            reference_price TEXT,
            reference_price_method TEXT,
            effective_price TEXT,
            price_impact_bps TEXT,
            total_deviation_bps TEXT,
            fee_bps INTEGER,
            fee_amount_raw TEXT,
            gas_units INTEGER,
            gas_price_wei TEXT,
            gas_cost_native_wei TEXT,
            gas_method TEXT,
            gas_status TEXT,
            quote_method TEXT NOT NULL,
            quote_source TEXT,
            block_number INTEGER NOT NULL,
            block_hash TEXT,
            observed_at TEXT NOT NULL,
            success INTEGER NOT NULL,
            failure_reason TEXT,
            notes TEXT,
            data_status TEXT NOT NULL,
            provenance_json TEXT,
            UNIQUE (run_id, dex_id, pool_address, input_token, output_token, trade_size, block_number),
            FOREIGN KEY (run_id) REFERENCES collection_runs (run_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_swap_obs_pool ON swap_observations (chain_id, pool_address, observed_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_swap_obs_run ON swap_observations (run_id)",
        # ------------------------------------------------- swap_events
        """
        CREATE TABLE IF NOT EXISTS swap_events (
            chain_id INTEGER NOT NULL,
            transaction_hash TEXT NOT NULL,
            log_index INTEGER NOT NULL,
            run_id TEXT,
            dex_id TEXT NOT NULL,
            pool_address TEXT NOT NULL,
            pool_type TEXT NOT NULL,
            block_number INTEGER NOT NULL,
            block_hash TEXT,
            block_timestamp INTEGER,
            sender TEXT,
            recipient TEXT,
            amount0_raw TEXT NOT NULL,
            amount1_raw TEXT NOT NULL,
            sqrt_price_x96 TEXT,
            liquidity TEXT,
            tick INTEGER,
            observed_at TEXT NOT NULL,
            data_status TEXT NOT NULL,
            PRIMARY KEY (chain_id, transaction_hash, log_index)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_swap_events_pool ON swap_events (chain_id, pool_address, block_number)",
        "CREATE INDEX IF NOT EXISTS idx_swap_events_block ON swap_events (chain_id, block_number)",
    ),
)

MIGRATION_003 = Migration(
    version=3,
    description="liquidity events, gas observations, prices, checkpoints, health, results",
    statements=(
        # ------------------------------------------------ liquidity_events
        """
        CREATE TABLE IF NOT EXISTS liquidity_events (
            chain_id INTEGER NOT NULL,
            transaction_hash TEXT NOT NULL,
            log_index INTEGER NOT NULL,
            run_id TEXT,
            dex_id TEXT NOT NULL,
            pool_address TEXT NOT NULL,
            pool_type TEXT NOT NULL,
            event_type TEXT NOT NULL,
            block_number INTEGER NOT NULL,
            block_hash TEXT,
            block_timestamp INTEGER,
            owner TEXT,
            sender TEXT,
            amount0_raw TEXT,
            amount1_raw TEXT,
            amount_raw TEXT,
            liquidity TEXT,
            tick_lower INTEGER,
            tick_upper INTEGER,
            observed_at TEXT NOT NULL,
            data_status TEXT NOT NULL,
            PRIMARY KEY (chain_id, transaction_hash, log_index)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_liq_events_pool ON liquidity_events (chain_id, pool_address, block_number)",
        # ------------------------------------------------ gas_observations
        """
        CREATE TABLE IF NOT EXISTS gas_observations (
            observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            chain_id INTEGER NOT NULL,
            transaction_hash TEXT NOT NULL,
            block_number INTEGER NOT NULL,
            block_timestamp INTEGER,
            dex_id TEXT,
            pool_address TEXT,
            gas_used INTEGER NOT NULL,
            effective_gas_price_wei TEXT,
            gas_cost_wei TEXT,
            transaction_type INTEGER,
            method_selector TEXT,
            status INTEGER,
            observed_at TEXT NOT NULL,
            source TEXT NOT NULL,
            data_status TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_gas_obs_tx ON gas_observations (chain_id, transaction_hash)",
        "CREATE INDEX IF NOT EXISTS idx_gas_obs_pool ON gas_observations (chain_id, pool_address, block_number)",
        # --------------------------------------------------------- prices
        """
        CREATE TABLE IF NOT EXISTS prices (
            price_id INTEGER PRIMARY KEY AUTOINCREMENT,
            chain_id INTEGER NOT NULL,
            token_address TEXT NOT NULL,
            quote_asset TEXT NOT NULL,
            price TEXT,
            source TEXT NOT NULL,
            method TEXT,
            block_number INTEGER,
            observed_at TEXT NOT NULL,
            data_status TEXT NOT NULL,
            detail TEXT,
            UNIQUE (chain_id, token_address, quote_asset, block_number, source)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_prices_token ON prices (chain_id, token_address, observed_at DESC)",
    ),
)

MIGRATION_004 = Migration(
    version=4,
    description="checkpoints, provider health and analytics results",
    statements=(
        # ------------------------------------------- collection_checkpoints
        """
        CREATE TABLE IF NOT EXISTS collection_checkpoints (
            checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
            chain_id INTEGER NOT NULL,
            dex_id TEXT NOT NULL,
            collector TEXT NOT NULL,
            last_block INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            detail TEXT,
            UNIQUE (chain_id, dex_id, collector)
        )
        """,
        # ------------------------------------------------- provider_health
        """
        CREATE TABLE IF NOT EXISTS provider_health (
            health_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            rpc_url TEXT NOT NULL,
            reachable INTEGER NOT NULL,
            status TEXT NOT NULL,
            latency_ms REAL,
            chain_id_observed INTEGER,
            chain_id_expected INTEGER NOT NULL,
            chain_matches INTEGER NOT NULL,
            latest_block INTEGER,
            eth_call_ok INTEGER NOT NULL,
            detail TEXT,
            checked_at TEXT NOT NULL
        )
        """,
        # ----------------------------------------------- analytics_results
        """
        CREATE TABLE IF NOT EXISTS analytics_results (
            result_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            subject TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (run_id, kind, subject)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_results_run ON analytics_results (run_id, kind)",
    ),
)

MIGRATION_005 = Migration(

    version=5,
    description="allow a run to complete with recorded errors",
    statements=(
        # A run that processed data but recorded per-market failures is neither a
        # clean success nor a failure. SQLite cannot alter a CHECK constraint in
        # place, so the table is rebuilt following the documented
        # create-copy-drop-rename order: building under a temporary name and
        # renaming it into position avoids rewriting the foreign-key clause that
        # swap_observations/liquidity_events hold against collection_runs.
        """
        CREATE TABLE collection_runs_new (
            run_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL CHECK (
                status IN ('running','completed','completed_with_errors','failed')
            ),
            trigger TEXT,
            chain_id INTEGER NOT NULL,
            rpc_url TEXT,
            start_block INTEGER,
            end_block INTEGER,
            blocks_processed INTEGER NOT NULL DEFAULT 0,
            markets_processed INTEGER NOT NULL DEFAULT 0,
            observations_created INTEGER NOT NULL DEFAULT 0,
            errors INTEGER NOT NULL DEFAULT 0,
            error_summary TEXT,
            config_json TEXT
        )
        """,
        """
        INSERT INTO collection_runs_new (
            run_id, started_at, completed_at, status, trigger, chain_id, rpc_url,
            start_block, end_block, blocks_processed, markets_processed,
            observations_created, errors, error_summary, config_json
        )
        SELECT
            run_id, started_at, completed_at, status, trigger, chain_id, rpc_url,
            start_block, end_block, blocks_processed, markets_processed,
            observations_created, errors, error_summary, config_json
        FROM collection_runs
        """,
        "DROP TABLE collection_runs",
        "ALTER TABLE collection_runs_new RENAME TO collection_runs",
        "CREATE INDEX IF NOT EXISTS idx_runs_started ON collection_runs (started_at DESC)",
    ),
)

MIGRATION_006 = Migration(
    version=6,
    description="pool-state provenance",
    statements=(
        # Every pool-state observation keeps the provenance needed to reproduce
        # where it came from (RPC endpoint, method, block reference).
        "ALTER TABLE pool_states ADD COLUMN provenance_json TEXT",
    ),
)

MIGRATION_007 = Migration(
    version=7,
    description="pool raw fee tier (protocol units)",
    statements=(
        # Concentrated-liquidity protocols identify fee tiers in their own
        # units (e.g. Uniswap-style 500/3000/10000); persist the raw value the
        # adapter read from the pool contract alongside fee_bps.
        "ALTER TABLE pools ADD COLUMN fee_tier_raw INTEGER",
    ),
)

ALL_MIGRATIONS: tuple[Migration, ...] = (
    MIGRATION_001,
    MIGRATION_002,
    MIGRATION_003,
    MIGRATION_004,
    MIGRATION_005,
    MIGRATION_006,
    MIGRATION_007,
)


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply all pending migrations in order and return applied versions.

    Idempotent: already-applied versions are skipped, and each migration runs
    inside a transaction so a failure cannot leave a half-applied schema.
    """
    applied: list[int] = []
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, description TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    existing = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    for migration in ALL_MIGRATIONS:
        if migration.version in existing:
            continue
        # Foreign keys are suspended for the duration of a migration. SQLite
        # cannot alter a CHECK constraint or a column in place, so such
        # migrations rebuild a table (create-copy-drop-rename) and the
        # intermediate DROP would otherwise fail against the child rows that
        # reference it. The pragma is a no-op inside a transaction, which is why
        # it is issued outside the `with conn:` block below.
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            with conn:
                for statement in migration.statements:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations (version, description, applied_at) "
                    "VALUES (?, ?, ?)",
                    (migration.version, migration.description, utc_now_iso()),
                )
        except sqlite3.Error as exc:
            raise MigrationError(
                f"migration {migration.version} ({migration.description}) failed: {exc}"
            ) from exc
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
        applied.append(migration.version)
        logger.info(
            "migration applied",
            extra={
                "event": "db.migration",
                "version": migration.version,
                "description": migration.description,
            },
        )
    return applied


def schema_version(conn: sqlite3.Connection) -> int:
    """Return the currently applied schema version (0 for an empty database)."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, description TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0]) if row and row[0] is not None else 0


