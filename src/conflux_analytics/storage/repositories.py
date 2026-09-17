"""Data-access layer over the SQLite schema.

All queries used by collectors, the analytics engine, the API and the reports
live here so that SQL never leaks into business logic. Raw quantities cross
this boundary as integer strings; decimals as fixed-point strings.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from ..chain.units import decimal_str
from ..errors import StorageError
from ..logging_setup import get_logger
from ..models.common import utc_now_iso
from ..models.events import LiquidityEvent, SwapEvent
from ..models.execution import ExecutionObservation
from ..models.market import MarketState
from ..models.pool import Pool
from ..models.token import Token

logger = get_logger(__name__)


@dataclass(frozen=True)
class RunRecord:
    """One collection run as stored in ``collection_runs``."""

    run_id: str
    started_at: str
    completed_at: str | None
    status: str
    trigger: str | None
    chain_id: int
    rpc_url: str | None
    start_block: int | None
    end_block: int | None
    blocks_processed: int
    markets_processed: int
    observations_created: int
    errors: int
    error_summary: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "status": self.status,
            "trigger": self.trigger,
            "chain_id": self.chain_id,
            "rpc_url": self.rpc_url,
            "start_block": self.start_block,
            "end_block": self.end_block,
            "blocks_processed": self.blocks_processed,
            "markets_processed": self.markets_processed,
            "observations_created": self.observations_created,
            "errors": self.errors,
            "error_summary": self.error_summary,
        }


def _row_to_run(row: sqlite3.Row | None) -> RunRecord | None:
    if row is None:
        return None
    return RunRecord(
        run_id=row["run_id"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        status=row["status"],
        trigger=row["trigger"],
        chain_id=row["chain_id"],
        rpc_url=row["rpc_url"],
        start_block=row["start_block"],
        end_block=row["end_block"],
        blocks_processed=row["blocks_processed"],
        markets_processed=row["markets_processed"],
        observations_created=row["observations_created"],
        errors=row["errors"],
        error_summary=row["error_summary"],
    )


class QueryResult:
    """Materialised result of a statement.

    The rows are read while the connection lock is held, so a cursor is never
    iterated from a different thread than the one that created it.
    """

    __slots__ = ("_rows", "lastrowid", "rowcount")

    def __init__(
        self, rows: list[sqlite3.Row], lastrowid: int | None, rowcount: int
    ) -> None:
        self._rows = rows
        self.lastrowid = lastrowid
        self.rowcount = rowcount

    def fetchone(self) -> sqlite3.Row | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[sqlite3.Row]:
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)

    def __len__(self) -> int:
        return len(self._rows)


class Repository:
    """Base class holding the shared connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> QueryResult:
        lock = getattr(self.conn, "lock", None)
        try:
            if lock is None:
                cursor = self.conn.execute(sql, params)
            else:
                with lock:
                    cursor = self.conn.execute(sql, params)
            rows = cursor.fetchall()
            return QueryResult(rows, cursor.lastrowid, cursor.rowcount)
        except sqlite3.Error as exc:
            raise StorageError(f"query failed: {exc}") from exc


class RunRepository(Repository):
    """Lifecycle of collection runs."""

    def start_run(
        self,
        run_id: str,
        *,
        chain_id: int,
        rpc_url: str | None,
        trigger: str,
        config_json: str | None = None,
    ) -> str:
        self._execute(
            "INSERT INTO collection_runs (run_id, started_at, status, trigger, chain_id,"
            " rpc_url, config_json) VALUES (?, ?, 'running', ?, ?, ?, ?)",
            (run_id, utc_now_iso(), trigger, chain_id, rpc_url, config_json),
        )
        self.conn.commit()
        return run_id

    def complete_run(
        self,
        run_id: str,
        *,
        status: str = "completed",
        start_block: int | None = None,
        end_block: int | None = None,
        blocks_processed: int = 0,
        markets_processed: int = 0,
        observations_created: int = 0,
        errors: int = 0,
        error_summary: str | None = None,
    ) -> None:
        self._execute(
            "UPDATE collection_runs SET completed_at = ?, status = ?, start_block = ?,"
            " end_block = ?, blocks_processed = ?, markets_processed = ?,"
            " observations_created = ?, errors = ?, error_summary = ? WHERE run_id = ?",
            (
                utc_now_iso(),
                status,
                start_block,
                end_block,
                blocks_processed,
                markets_processed,
                observations_created,
                errors,
                error_summary,
                run_id,
            ),
        )
        self.conn.commit()

    def get_run(self, run_id: str) -> RunRecord | None:
        row = self._execute(
            "SELECT * FROM collection_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return _row_to_run(row)

    def latest_run(self, *, successful_only: bool = True) -> RunRecord | None:
        """The most recent run, optionally restricted to completed ones.

        The latest-run concept is what separates *current* market state from
        historical observations: state rows are "current" only when their
        ``run_id`` matches the latest completed run. A run that finished with
        recorded per-market errors (``completed_with_errors``) is still a
        completed run - it collected data - so it counts here; only runs that
        are still ``running`` or that ``failed`` are excluded.
        """
        clause = (
            "WHERE status IN ('completed','completed_with_errors')" if successful_only else ""
        )
        row = self._execute(
            f"SELECT * FROM collection_runs {clause} ORDER BY started_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        return _row_to_run(row)

    def list_runs(self, limit: int = 50) -> list[RunRecord]:
        rows = self._execute(
            "SELECT * FROM collection_runs ORDER BY started_at DESC, rowid DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [run for run in (_row_to_run(row) for row in rows) if run is not None]


class CatalogRepository(Repository):
    """DEXes, tokens and pools."""

    def upsert_dex(
        self,
        *,
        chain_id: int,
        dex_id: str,
        name: str,
        pool_type: str,
        enabled: bool,
        documentation: str | None,
        contracts_json: str,
        discovery_method: str | None,
        quoting_method: str | None,
        verified: bool,
        verified_detail: str | None = None,
    ) -> None:
        self._execute(
            "INSERT INTO dexes (chain_id, dex_id, name, pool_type, enabled, documentation,"
            " contracts_json, discovery_method, quoting_method, verified, verified_detail,"
            " verified_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (chain_id, dex_id) DO UPDATE SET name=excluded.name,"
            " pool_type=excluded.pool_type, enabled=excluded.enabled,"
            " documentation=excluded.documentation, contracts_json=excluded.contracts_json,"
            " discovery_method=excluded.discovery_method,"
            " quoting_method=excluded.quoting_method, verified=excluded.verified,"
            " verified_detail=excluded.verified_detail, verified_at=excluded.verified_at",
            (
                chain_id,
                dex_id,
                name,
                pool_type,
                int(enabled),
                documentation,
                contracts_json,
                discovery_method,
                quoting_method,
                int(verified),
                verified_detail,
                utc_now_iso() if verified else None,
            ),
        )
        self.conn.commit()

    def list_dexes(self, chain_id: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM dexes"
        params: tuple[Any, ...] = ()
        if chain_id is not None:
            sql += " WHERE chain_id = ?"
            params = (chain_id,)
        return [dict(row) for row in self._execute(sql + " ORDER BY dex_id", params).fetchall()]

    def upsert_token(self, token: Token) -> None:
        self._execute(
            "INSERT INTO tokens (chain_id, address, symbol, name, decimals, total_supply_raw,"
            " metadata_status, metadata_detail, fetched_at, provenance_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (chain_id, address) DO UPDATE SET symbol=excluded.symbol,"
            " name=excluded.name, decimals=excluded.decimals,"
            " total_supply_raw=excluded.total_supply_raw,"
            " metadata_status=excluded.metadata_status,"
            " metadata_detail=excluded.metadata_detail, fetched_at=excluded.fetched_at,"
            " provenance_json=excluded.provenance_json",
            (
                token.chain_id,
                token.address,
                token.symbol,
                token.name,
                token.decimals,
                str(token.total_supply_raw) if token.total_supply_raw is not None else None,
                token.metadata_status.value,
                token.metadata_detail,
                token.fetched_at,
                token.provenance.to_json() if token.provenance else None,
            ),
        )

    def list_tokens(self, chain_id: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM tokens"
        params: tuple[Any, ...] = ()
        if chain_id is not None:
            sql += " WHERE chain_id = ?"
            params = (chain_id,)
        return [dict(row) for row in self._execute(sql + " ORDER BY address", params).fetchall()]

    def upsert_pool(self, pool: Pool) -> None:
        self._execute(
            "INSERT INTO pools (chain_id, dex_id, pool_address, pool_type, token0_address,"
            " token1_address, token0_symbol, token1_symbol, token0_decimals, token1_decimals,"
            " fee_bps, tick_spacing, fee_tier_raw, discovery_source, verified, active,"
            " first_seen_at, last_verified_at, metadata_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (chain_id, pool_address) DO UPDATE SET"
            " fee_bps=excluded.fee_bps, tick_spacing=excluded.tick_spacing,"
            " fee_tier_raw=excluded.fee_tier_raw,"
            " verified=excluded.verified, active=excluded.active,"
            " last_verified_at=excluded.last_verified_at, metadata_json=excluded.metadata_json",
            (
                pool.chain_id,
                pool.dex_id,
                pool.pool_address,
                pool.pool_type.value,
                pool.token0_address,
                pool.token1_address,
                None,
                None,
                None,
                None,
                pool.fee_bps,
                pool.tick_spacing,
                pool.fee_tier_raw,
                pool.discovery_source,
                int(pool.verified_at is not None),
                int(pool.active),
                utc_now_iso(),
                utc_now_iso() if pool.verified_at else None,
                json.dumps({"verification_method": pool.verification_method, "detail": pool.detail}, sort_keys=True),
            ),
        )
        self.conn.commit()

    def update_pool_token(self, pool_address: str, index: int, token: Token) -> None:
        """Attach fetched token metadata to a stored pool row."""
        if index not in (0, 1):
            raise StorageError(f"token index must be 0 or 1, got {index}")
        symbol_col = f"token{index}_symbol"
        decimals_col = f"token{index}_decimals"
        self._execute(
            f"UPDATE pools SET {symbol_col} = ?, {decimals_col} = ? WHERE pool_address = ?",
            (token.symbol, token.decimals, pool_address.lower()),
        )
        self.conn.commit()

    def list_pools(
        self,
        *,
        chain_id: int | None = None,
        dex_id: str | None = None,
        active_only: bool = False,
        token: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if chain_id is not None:
            clauses.append("chain_id = ?")
            params.append(chain_id)
        if dex_id is not None:
            clauses.append("dex_id = ?")
            params.append(dex_id)
        if active_only:
            clauses.append("active = 1")
        if token is not None:
            clauses.append("(token0_address = ? OR token1_address = ?)")
            params.extend([token.lower(), token.lower()])
        sql = "SELECT * FROM pools"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return [
            dict(row)
            for row in self._execute(sql + " ORDER BY dex_id, pool_address", tuple(params)).fetchall()
        ]

    def get_pool(self, pool_address: str) -> dict[str, Any] | None:
        row = self._execute(
            "SELECT * FROM pools WHERE pool_address = ?", (pool_address.lower(),)
        ).fetchone()
        return dict(row) if row else None


class ObservationRepository(Repository):
    """Pool states, execution observations, events, gas, health and results."""

    # -- pool states -------------------------------------------------------
    def insert_pool_state(self, state: MarketState) -> int:
        r0, r1 = state.reserves_decimal if state.is_constant_product else (None, None)
        cursor = self._execute(
            "INSERT INTO pool_states (run_id, chain_id, dex_id, pool_address, pool_type,"
            " block_number, block_hash, observed_at, reserve0_raw, reserve1_raw,"
            " reserve0_dec, reserve1_dec, sqrt_price_x96, tick, liquidity, fee_bps,"
            " data_status, detail, raw_state_json, source, provenance_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                state.run_id,
                state.chain_id,
                state.dex_id,
                state.pool_address,
                state.pool_type.value,
                state.block_number,
                state.block_hash,
                state.observed_at,
                str(state.reserve0_raw) if state.reserve0_raw is not None else None,
                str(state.reserve1_raw) if state.reserve1_raw is not None else None,
                decimal_str(r0),
                decimal_str(r1),
                str(state.sqrt_price_x96) if state.sqrt_price_x96 is not None else None,
                state.tick,
                str(state.liquidity) if state.liquidity is not None else None,
                state.fee_bps,
                state.data_status.value,
                state.detail,
                json.dumps(state.raw_state, sort_keys=True),
                state.source,
                state.provenance.to_json() if state.provenance else None,
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid or 0)

    def latest_pool_state(self, pool_address: str, run_id: str | None = None) -> dict[str, Any] | None:
        """Latest state for a pool, restricted to ``run_id`` when given."""
        sql = "SELECT * FROM pool_states WHERE pool_address = ?"
        params: list[Any] = [pool_address.lower()]
        if run_id is not None:
            sql += " AND run_id = ?"
            params.append(run_id)
        sql += " ORDER BY block_number DESC, state_id DESC LIMIT 1"
        row = self._execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None

    def pool_states(
        self,
        *,
        pool_address: str | None = None,
        run_id: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if pool_address is not None:
            clauses.append("pool_address = ?")
            params.append(pool_address.lower())
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        sql = "SELECT * FROM pool_states"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        rows = self._execute(
            sql + " ORDER BY block_number DESC, state_id DESC LIMIT ?", (*params, int(limit))
        ).fetchall()
        return [dict(row) for row in rows]

    # -- execution observations -------------------------------------------
    def insert_execution_observation(self, observation: ExecutionObservation) -> int:
        row = observation.as_row()
        columns = (
            "run_id, chain_id, dex_id, pool_address, pool_type, input_token, output_token,"
            " trade_size, input_amount_raw, input_amount_dec, output_amount_raw,"
            " output_amount_dec, reference_price, reference_price_method, effective_price,"
            " price_impact_bps, total_deviation_bps, fee_bps, fee_amount_raw, gas_units,"
            " gas_price_wei, gas_cost_native_wei, gas_method, gas_status, quote_method,"
            " quote_source, block_number, block_hash, observed_at, success, failure_reason,"
            " notes, data_status, provenance_json"
        )
        cursor = self._execute(
            f"INSERT INTO swap_observations ({columns}) VALUES ({','.join('?' * 34)})",
            (
                row["run_id"],
                row["chain_id"],
                row["dex_id"],
                row["pool_address"],
                row["pool_type"],
                row["input_token"],
                row["output_token"],
                row["trade_size"],
                row["input_amount_raw"],
                row["input_amount_dec"],
                row["output_amount_raw"],
                row["output_amount_dec"],
                row["reference_price"],
                row["reference_price_method"],
                row["effective_price"],
                row["price_impact_bps"],
                row["total_deviation_bps"],
                row["fee_bps"],
                row["fee_amount_raw"],
                row["gas_units"],
                row["gas_price_wei"],
                row["gas_cost_native_wei"],
                row["gas_method"],
                row["gas_status"],
                row["quote_method"],
                row["quote_source"],
                row["block_number"],
                row["block_hash"],
                row["observed_at"],
                row["success"],
                row["failure_reason"],
                row["notes"],
                row["data_status"],
                row["provenance_json"],
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid or 0)

    def execution_observations(
        self,
        *,
        pool_address: str | None = None,
        dex_id: str | None = None,
        run_id: str | None = None,
        from_block: int | None = None,
        to_block: int | None = None,
        trade_size: str | None = None,
        successful_only: bool = False,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Query execution observations with the API's filter surface."""
        clauses: list[str] = []
        params: list[Any] = []
        if pool_address is not None:
            clauses.append("pool_address = ?")
            params.append(pool_address.lower())
        if dex_id is not None:
            clauses.append("dex_id = ?")
            params.append(dex_id)
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        if from_block is not None:
            clauses.append("block_number >= ?")
            params.append(from_block)
        if to_block is not None:
            clauses.append("block_number <= ?")
            params.append(to_block)
        if trade_size is not None:
            clauses.append("trade_size = ?")
            params.append(trade_size)
        if successful_only:
            clauses.append("success = 1")
        sql = "SELECT * FROM swap_observations"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        rows = self._execute(
            sql + " ORDER BY block_number DESC, observation_id DESC LIMIT ?",
            (*params, int(limit)),
        ).fetchall()
        return [dict(row) for row in rows]

    # -- raw events ---------------------------------------------------------
    def insert_swap_events(self, events: list[SwapEvent]) -> int:
        """Insert swap events, ignoring rows already stored (idempotent)."""
        inserted = 0
        for event in events:
            row = event.as_row()
            cursor = self._execute(
                "INSERT OR IGNORE INTO swap_events (chain_id, transaction_hash, log_index,"
                " run_id, dex_id, pool_address, pool_type, block_number, block_hash,"
                " block_timestamp, sender, recipient, amount0_raw, amount1_raw,"
                " sqrt_price_x96, liquidity, tick, observed_at, data_status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["chain_id"], row["transaction_hash"], row["log_index"],
                    row["run_id"], row["dex_id"], row["pool_address"], row["pool_type"],
                    row["block_number"], row["block_hash"], row["block_timestamp"],
                    row["sender"], row["recipient"], row["amount0_raw"], row["amount1_raw"],
                    row["sqrt_price_x96"], row["liquidity"], row["tick"],
                    row["observed_at"], row["data_status"],
                ),
            )
            inserted += cursor.rowcount if cursor.rowcount > 0 else 0
        self.conn.commit()
        return inserted

    def swap_events(
        self,
        *,
        pool_address: str | None = None,
        dex_id: str | None = None,
        from_block: int | None = None,
        to_block: int | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if pool_address is not None:
            clauses.append("pool_address = ?")
            params.append(pool_address.lower())
        if dex_id is not None:
            clauses.append("dex_id = ?")
            params.append(dex_id)
        if from_block is not None:
            clauses.append("block_number >= ?")
            params.append(from_block)
        if to_block is not None:
            clauses.append("block_number <= ?")
            params.append(to_block)
        sql = "SELECT * FROM swap_events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        rows = self._execute(
            sql + " ORDER BY block_number DESC, log_index DESC LIMIT ?", (*params, int(limit))
        ).fetchall()
        return [dict(row) for row in rows]

    def insert_liquidity_events(self, events: list[LiquidityEvent]) -> int:
        inserted = 0
        for event in events:
            row = event.as_row()
            cursor = self._execute(
                "INSERT OR IGNORE INTO liquidity_events (chain_id, transaction_hash,"
                " log_index, run_id, dex_id, pool_address, pool_type, event_type,"
                " block_number, block_hash, block_timestamp, owner, sender, amount0_raw,"
                " amount1_raw, amount_raw, liquidity, tick_lower, tick_upper, observed_at,"
                " data_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                " ?, ?, ?, ?)",
                (
                    row["chain_id"], row["transaction_hash"], row["log_index"],
                    row["run_id"], row["dex_id"], row["pool_address"], row["pool_type"],
                    row["event_type"], row["block_number"], row["block_hash"],
                    row["block_timestamp"], row["owner"], row["sender"],
                    row["amount0_raw"], row["amount1_raw"], row["amount_raw"],
                    row["liquidity"], row["tick_lower"], row["tick_upper"],
                    row["observed_at"], row["data_status"],
                ),
            )
            inserted += cursor.rowcount if cursor.rowcount > 0 else 0
        self.conn.commit()
        return inserted

    def liquidity_events(
        self,
        *,
        pool_address: str | None = None,
        dex_id: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if pool_address is not None:
            clauses.append("pool_address = ?")
            params.append(pool_address.lower())
        if dex_id is not None:
            clauses.append("dex_id = ?")
            params.append(dex_id)
        sql = "SELECT * FROM liquidity_events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        rows = self._execute(
            sql + " ORDER BY block_number DESC, log_index DESC LIMIT ?", (*params, int(limit))
        ).fetchall()
        return [dict(row) for row in rows]


