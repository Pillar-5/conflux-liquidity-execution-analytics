"""Per-collector checkpoints so a restart resumes from the last success."""

from __future__ import annotations

import sqlite3
from typing import Any

from ..errors import StorageError
from ..logging_setup import get_logger
from ..models.common import utc_now_iso

logger = get_logger(__name__)


class CheckpointRepository:
    """Reads and writes ``collection_checkpoints`` rows.

    One row per ``(chain_id, dex_id, collector)``. The stored ``last_block`` is
    the *last block that was fully processed*, so a resumed collection starts
    at ``last_block + 1`` and never re-issues or skips work silently.
    """

    def __init__(self, conn: sqlite3.Connection, chain_id: int) -> None:
        self.conn = conn
        self.chain_id = chain_id

    def get(self, dex_id: str, collector: str) -> int | None:
        """Alias for :meth:`last_block`, the read side of the round trip."""
        return self.last_block(dex_id, collector)

    def save(self, dex_id: str, collector: str, last_block: int, detail: str | None = None) -> None:
        """Alias for :meth:`set_last_block`, the write side of the round trip."""
        self.set_last_block(dex_id, collector, last_block, detail)

    def last_block(self, dex_id: str, collector: str) -> int | None:
        try:
            row = self.conn.execute(
                "SELECT last_block FROM collection_checkpoints"
                " WHERE chain_id = ? AND dex_id = ? AND collector = ?",
                (self.chain_id, dex_id, collector),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StorageError(f"checkpoint read failed: {exc}") from exc
        return int(row["last_block"]) if row else None

    def set_last_block(
        self, dex_id: str, collector: str, last_block: int, detail: str | None = None
    ) -> None:
        try:
            self.conn.execute(
                "INSERT INTO collection_checkpoints (chain_id, dex_id, collector,"
                " last_block, updated_at, detail) VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (chain_id, dex_id, collector) DO UPDATE SET"
                " last_block=excluded.last_block, updated_at=excluded.updated_at,"
                " detail=excluded.detail",
                (self.chain_id, dex_id, collector, int(last_block), utc_now_iso(), detail),
            )
            self.conn.commit()
        except sqlite3.Error as exc:
            raise StorageError(f"checkpoint write failed: {exc}") from exc
        logger.info(
            "checkpoint advanced",
            extra={
                "event": "checkpoint.advanced",
                "component": "checkpoints",
                "dex": dex_id,
                "collector": collector,
                "last_block": int(last_block),
            },
        )

    def all_checkpoints(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT chain_id, dex_id, collector, last_block, updated_at, detail"
            " FROM collection_checkpoints ORDER BY dex_id, collector"
        ).fetchall()
        return [dict(row) for row in rows]
