"""SQLite connection management and migration bootstrap."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from ..errors import StorageError
from ..logging_setup import get_logger
from .migrations import migrate, schema_version

logger = get_logger(__name__)

#: Pragmas applied to every connection: WAL for concurrent readers, FULL
#: foreign-key enforcement, and NORMAL synchronous durability.
PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA synchronous=NORMAL",
)


class ThreadSafeConnection(sqlite3.Connection):
    """A SQLite connection that may be shared by several threads.

    FastAPI executes synchronous endpoints in a worker thread, so a connection
    created on the main thread would otherwise raise
    ``SQLite objects created in a thread can only be used in that same thread``.
    The connection is therefore opened with ``check_same_thread=False`` and
    every statement is serialised by :attr:`lock`. SQLite is still the only
    writer, which is exactly the intended deployment model for this MVP.
    """

    lock: threading.RLock

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.lock = threading.RLock()

    def execute(self, sql: str, parameters: object = (), /):  # type: ignore[override]
        with self.lock:
            return super().execute(sql, parameters)

    def executemany(self, sql: str, seq: object, /):  # type: ignore[override]
        with self.lock:
            return super().executemany(sql, seq)

    def executescript(self, script: str, /):  # type: ignore[override]
        with self.lock:
            return super().executescript(script)

    def commit(self) -> None:  # type: ignore[override]
        with self.lock:
            super().commit()

    def cursor(self, factory: object = None):  # type: ignore[override]
        with self.lock:
            return super().cursor(factory)


def connect(db_path: Path | str, *, apply_migrations: bool = True) -> ThreadSafeConnection:
    """Open (creating if needed) the analytics database and apply migrations."""
    path = Path(db_path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(
            str(path),
            detect_types=0,
            check_same_thread=False,
            factory=ThreadSafeConnection,
        )
        conn.row_factory = sqlite3.Row
        for pragma in PRAGMAS:
            conn.execute(pragma)
        if apply_migrations:
            applied = migrate(conn)
            if applied:
                logger.info(
                    "database ready",
                    extra={
                        "event": "db.ready",
                        "path": str(path),
                        "applied_migrations": applied,
                        "schema_version": schema_version(conn),
                    },
                )
        return conn
    except sqlite3.Error as exc:
        raise StorageError(f"cannot open database {path}: {exc}") from exc
