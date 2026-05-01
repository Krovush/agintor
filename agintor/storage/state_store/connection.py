from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from ...utils import ensure_directory, now_ts, stable_hash

from .layout import (
    SQLITE_BUSY_TIMEOUT_MS,
    STATE_STORE_DB_NAME,
    ensure_state_layout,
)

def open_state_store(run_root: str | Path) -> "StateStore":
    from .store import StateStore

    return StateStore(Path(run_root).resolve())


def initialize(run_root: str | Path) -> "StateStore":
    store = open_state_store(run_root)
    store.ensure_current_schema()
    return store


class ConnectionMixin:
    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        ensure_state_layout(self.run_root)
        self._rebuild_if_dirty()
        conn = sqlite3.connect(str(self.db_path), timeout=SQLITE_BUSY_TIMEOUT_MS / 1000.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as conn:
            self._ensure_schema(conn)
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
