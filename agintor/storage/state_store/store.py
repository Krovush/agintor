from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from ...utils import ensure_directory, now_ts, stable_hash

from .connection import ConnectionMixin
from .indexers import IndexerMixin
from .layout import (
    INDEX_DIRTY_FILE,
    STATE_STORE_DB_NAME,
    STATE_STORE_DIR_NAME,
    ensure_state_layout,
)
from .memory import MemoryMixin
from .queries import QueryMixin
from .rebuild import RebuildMixin
from .schema import SchemaMixin

class StateStore(RebuildMixin, IndexerMixin, MemoryMixin, QueryMixin, SchemaMixin, ConnectionMixin):
    def __init__(self, run_root: Path) -> None:
        self.run_root = Path(run_root).resolve()
        self.state_root = self.run_root / STATE_STORE_DIR_NAME
        self.db_path = self.state_root / STATE_STORE_DB_NAME
        self._rebuilding_from_canonical = False

    def ensure_current_schema(self) -> None:
        ensure_state_layout(self.run_root)
        with self._connection() as conn:
            self._ensure_schema(conn)
            conn.commit()

    def _dirty_path(self) -> Path:
        return self.state_root / INDEX_DIRTY_FILE
