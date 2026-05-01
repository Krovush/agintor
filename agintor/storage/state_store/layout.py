from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from ...utils import ensure_directory, now_ts, stable_hash

STATE_STORE_DIR_NAME = "state"


STATE_STORE_DB_NAME = "runtime_state.sqlite"


STATE_STORE_SCHEMA_VERSION = 1


SQLITE_BUSY_TIMEOUT_MS = 5000


INDEX_DIRTY_FILE = "index_dirty.json"


STATE_STORE_DIRS = (
    "short_term",
    "long_term",
    "long_term/writes",
    "long_term/edges",
    "long_term/retrieval",
    "recovery",
    "recovery/fingerprints",
    "working_memory",
)


class StateStoreError(RuntimeError):
    pass


def ensure_state_layout(run_root: str | Path) -> Path:
    state_root = ensure_directory(Path(run_root).resolve() / STATE_STORE_DIR_NAME)
    for rel_path in STATE_STORE_DIRS:
        ensure_directory(state_root / rel_path)
    return state_root


def state_db_path(run_root: str | Path) -> Path:
    return Path(run_root).resolve() / STATE_STORE_DIR_NAME / STATE_STORE_DB_NAME
