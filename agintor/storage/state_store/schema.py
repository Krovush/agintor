from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from ...utils import ensure_directory, now_ts, stable_hash

from .layout import (
    STATE_STORE_SCHEMA_VERSION,
    StateStoreError,
)

_SCHEMA_METADATA_TABLE = "state_store_metadata"


_CANONICAL_COLUMNS = """
    canonical_scope TEXT NOT NULL,
    canonical_ref TEXT NOT NULL,
    canonical_record_id TEXT NOT NULL,
    payload_json TEXT NOT NULL
"""


_DDL = [
    f"""CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY,
        run_root TEXT NOT NULL,
        request_id TEXT,
        evaluation_unit_id TEXT,
        request_mode TEXT,
        runtime_hash TEXT,
        runtime_contract_version TEXT,
        runtime_backend TEXT,
        task_id TEXT,
        seed INTEGER,
        current_attempt_id TEXT,
        latest_checkpoint_ref TEXT,
        lifecycle_state TEXT,
        resumable INTEGER,
        prune_eligible INTEGER,
        created_at REAL,
        updated_at REAL,
        {_CANONICAL_COLUMNS}
    )""",
    f"""CREATE TABLE IF NOT EXISTS attempts (
        attempt_id TEXT PRIMARY KEY,
        run_id TEXT,
        sequence_no INTEGER,
        launch_kind TEXT,
        lifecycle_state TEXT,
        resumed_from_checkpoint_ref TEXT,
        workspace_root TEXT,
        latest_checkpoint_ref TEXT,
        failure_kind TEXT,
        started_at REAL,
        updated_at REAL,
        finished_at REAL,
        {_CANONICAL_COLUMNS}
    )""",
    f"""CREATE TABLE IF NOT EXISTS requests (
        request_id TEXT PRIMARY KEY,
        evaluation_unit_id TEXT,
        request_kind TEXT,
        request_mode TEXT,
        runtime_backend TEXT,
        task_id TEXT,
        seed INTEGER,
        plan_id TEXT,
        runtime_hash TEXT,
        {_CANONICAL_COLUMNS}
    )""",
    f"""CREATE TABLE IF NOT EXISTS evaluation_units (
        evaluation_unit_id TEXT,
        request_id TEXT,
        episode_kind TEXT,
        episode_step_index INTEGER,
        {_CANONICAL_COLUMNS},
        PRIMARY KEY (evaluation_unit_id, request_id)
    )""",
    f"""CREATE TABLE IF NOT EXISTS tasks (
        task_id TEXT,
        request_id TEXT,
        evaluation_unit_id TEXT,
        seed INTEGER,
        episode_id TEXT,
        episode_order INTEGER,
        {_CANONICAL_COLUMNS},
        PRIMARY KEY (task_id, request_id)
    )""",
    f"""CREATE TABLE IF NOT EXISTS episodes (
        evaluation_unit_id TEXT,
        request_id TEXT,
        episode_kind TEXT,
        episode_step_index INTEGER,
        task_id TEXT,
        seed INTEGER,
        {_CANONICAL_COLUMNS},
        PRIMARY KEY (evaluation_unit_id, request_id, canonical_record_id)
    )""",
    f"""CREATE TABLE IF NOT EXISTS checkpoints (
        checkpoint_id TEXT PRIMARY KEY,
        run_id TEXT,
        attempt_id TEXT,
        request_id TEXT,
        plan_id TEXT,
        task_id TEXT,
        seed INTEGER,
        sequence_no INTEGER,
        boundary TEXT,
        created_at REAL,
        resume_eligible INTEGER,
        resume_ineligibility_reason TEXT,
        source_checkpoint_ref TEXT,
        environment_fingerprint_id TEXT,
        latest_checkpoint_ref TEXT,
        {_CANONICAL_COLUMNS}
    )""",
    f"""CREATE TABLE IF NOT EXISTS checkpoint_lineage (
        checkpoint_id TEXT,
        source_checkpoint_ref TEXT,
        {_CANONICAL_COLUMNS},
        PRIMARY KEY (checkpoint_id, source_checkpoint_ref)
    )""",
    f"""CREATE TABLE IF NOT EXISTS branches (
        branch_id TEXT,
        checkpoint_id TEXT,
        request_id TEXT,
        status TEXT,
        {_CANONICAL_COLUMNS},
        PRIMARY KEY (branch_id, checkpoint_id)
    )""",
    f"""CREATE TABLE IF NOT EXISTS branch_publications (
        publication_id TEXT PRIMARY KEY,
        branch_id TEXT,
        checkpoint_id TEXT,
        request_id TEXT,
        {_CANONICAL_COLUMNS}
    )""",
    f"""CREATE TABLE IF NOT EXISTS receipts (
        side_effect_id TEXT PRIMARY KEY,
        request_id TEXT,
        checkpoint_id TEXT,
        branch_id TEXT,
        node_id TEXT,
        action_kind TEXT,
        status TEXT,
        result_ref_json TEXT,
        created_at REAL,
        {_CANONICAL_COLUMNS}
    )""",
    f"""CREATE TABLE IF NOT EXISTS artifacts (
        artifact_id TEXT PRIMARY KEY,
        checkpoint_id TEXT,
        receipt_id TEXT,
        artifact_kind TEXT,
        artifact_ref TEXT,
        {_CANONICAL_COLUMNS}
    )""",
    f"""CREATE TABLE IF NOT EXISTS runtime_events (
        event_id TEXT PRIMARY KEY,
        request_id TEXT,
        plan_id TEXT,
        sequence_no INTEGER,
        event TEXT,
        branch_id TEXT,
        node_id TEXT,
        created_at REAL,
        {_CANONICAL_COLUMNS}
    )""",
    f"""CREATE TABLE IF NOT EXISTS short_term_nodes (
        node_id TEXT,
        checkpoint_id TEXT,
        node_type TEXT,
        label TEXT,
        {_CANONICAL_COLUMNS},
        PRIMARY KEY (node_id, checkpoint_id)
    )""",
    f"""CREATE TABLE IF NOT EXISTS short_term_edges (
        edge_id TEXT,
        checkpoint_id TEXT,
        src TEXT,
        dst TEXT,
        edge_type TEXT,
        {_CANONICAL_COLUMNS},
        PRIMARY KEY (edge_id, checkpoint_id)
    )""",
    f"""CREATE TABLE IF NOT EXISTS long_term_nodes (
        node_id TEXT,
        checkpoint_id TEXT,
        node_type TEXT,
        label TEXT,
        source_task_id TEXT,
        tombstoned INTEGER,
        {_CANONICAL_COLUMNS},
        PRIMARY KEY (node_id, checkpoint_id)
    )""",
    f"""CREATE TABLE IF NOT EXISTS long_term_writes (
        write_id TEXT PRIMARY KEY,
        checkpoint_id TEXT,
        target_node_id TEXT,
        action TEXT,
        source_task_id TEXT,
        source_attempt_id TEXT,
        source_checkpoint_ref TEXT,
        prior_write_id TEXT,
        contradiction_target_write_id TEXT,
        payload_ref TEXT,
        written_at REAL,
        {_CANONICAL_COLUMNS}
    )""",
    f"""CREATE TABLE IF NOT EXISTS long_term_edges (
        edge_id TEXT PRIMARY KEY,
        checkpoint_id TEXT,
        source_node_id TEXT,
        target_node_id TEXT,
        edge_type TEXT,
        introducing_write_id TEXT,
        tombstoned INTEGER,
        tombstone_write_id TEXT,
        written_at REAL,
        {_CANONICAL_COLUMNS}
    )""",
    f"""CREATE TABLE IF NOT EXISTS retrieval_diagnostics (
        diagnostic_id TEXT PRIMARY KEY,
        checkpoint_id TEXT,
        query_hash TEXT,
        task_id TEXT,
        seed INTEGER,
        request_id TEXT,
        scope_id TEXT,
        exact_first_preserved INTEGER,
        retrieved_at REAL,
        {_CANONICAL_COLUMNS}
    )""",
    f"""CREATE TABLE IF NOT EXISTS retrieval_signal_rows (
        signal_id TEXT PRIMARY KEY,
        diagnostic_id TEXT,
        node_id TEXT,
        rank INTEGER,
        exact_file_path_hit INTEGER,
        exact_symbol_hit INTEGER,
        node_id_match INTEGER,
        verifier_support_score REAL,
        lexical_overlap_score REAL,
        embedding_similarity_score REAL,
        same_task_affinity_score REAL,
        synthesized_neighbor_expansion INTEGER,
        {_CANONICAL_COLUMNS}
    )""",
    f"""CREATE TABLE IF NOT EXISTS recovery_attempts (
        recovery_attempt_id TEXT PRIMARY KEY,
        run_id TEXT,
        attempt_id TEXT,
        selected_checkpoint_ref TEXT,
        source_checkpoint_ref TEXT,
        origin_request_id TEXT,
        rebound_request_id TEXT,
        reconciliation_policy TEXT,
        compatibility_result TEXT,
        source_fingerprint_id TEXT,
        current_fingerprint_id TEXT,
        attempted_at REAL,
        completed_at REAL,
        {_CANONICAL_COLUMNS}
    )""",
    f"""CREATE TABLE IF NOT EXISTS environment_fingerprints (
        fingerprint_id TEXT PRIMARY KEY,
        runtime_backend TEXT,
        runtime_hash TEXT,
        runtime_contract_version TEXT,
        captured_at REAL,
        source_attempt_id TEXT,
        source_checkpoint_ref TEXT,
        {_CANONICAL_COLUMNS}
    )""",
    f"""CREATE TABLE IF NOT EXISTS trace_call_refs (
        call_id TEXT,
        checkpoint_id TEXT,
        request_id TEXT,
        session_id TEXT,
        runtime_task_key TEXT,
        {_CANONICAL_COLUMNS},
        PRIMARY KEY (call_id, checkpoint_id)
    )""",
    f"""CREATE TABLE IF NOT EXISTS grouped_trace_refs (
        record_id TEXT PRIMARY KEY,
        session_id TEXT,
        evaluation_unit_id TEXT,
        request_id TEXT,
        runtime_task_key TEXT,
        episode_kind TEXT,
        episode_step_index INTEGER,
        call_count INTEGER,
        materialization_state_ref TEXT,
        {_CANONICAL_COLUMNS}
    )""",
    f"""CREATE TABLE IF NOT EXISTS working_memory_snapshots (
        snapshot_id TEXT PRIMARY KEY,
        checkpoint_id TEXT,
        current_objective TEXT,
        active_plan_summary TEXT,
        captured_at REAL,
        {_CANONICAL_COLUMNS}
    )""",
    "CREATE INDEX IF NOT EXISTS idx_checkpoints_request ON checkpoints(request_id, resume_eligible, sequence_no)",
    "CREATE INDEX IF NOT EXISTS idx_long_term_writes_target ON long_term_writes(target_node_id, written_at)",
    "CREATE INDEX IF NOT EXISTS idx_retrieval_query ON retrieval_diagnostics(query_hash, retrieved_at)",
    "CREATE INDEX IF NOT EXISTS idx_events_request ON runtime_events(request_id, sequence_no)",
]


_COLUMN_MIGRATIONS = {
    "recovery_attempts": {
        "compatibility_result": "TEXT",
    },
    "retrieval_signal_rows": {
        "verifier_support_score": "REAL",
        "lexical_overlap_score": "REAL",
        "embedding_similarity_score": "REAL",
        "same_task_affinity_score": "REAL",
    },
}


_CANONICAL_TABLES = (
    "runs",
    "attempts",
    "requests",
    "evaluation_units",
    "tasks",
    "episodes",
    "checkpoints",
    "checkpoint_lineage",
    "branches",
    "branch_publications",
    "receipts",
    "artifacts",
    "runtime_events",
    "short_term_nodes",
    "short_term_edges",
    "long_term_nodes",
    "long_term_writes",
    "long_term_edges",
    "retrieval_diagnostics",
    "retrieval_signal_rows",
    "recovery_attempts",
    "environment_fingerprints",
    "trace_call_refs",
    "grouped_trace_refs",
    "working_memory_snapshots",
)


class SchemaMixin:
    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_SCHEMA_METADATA_TABLE} (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        stored_version = self._stored_schema_version(conn)
        if stored_version > STATE_STORE_SCHEMA_VERSION:
            raise StateStoreError(
                f"state store schema {stored_version} is newer than supported schema {STATE_STORE_SCHEMA_VERSION}"
            )
        self._create_tables(conn)
        self._ensure_added_columns(conn)
        if stored_version < STATE_STORE_SCHEMA_VERSION:
            self._set_schema_version(conn, STATE_STORE_SCHEMA_VERSION)

    def _stored_schema_version(self, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            f"SELECT value FROM {_SCHEMA_METADATA_TABLE} WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            return 0
        try:
            return int(row["value"])
        except Exception as exc:
            raise StateStoreError("invalid state store schema version metadata") from exc

    def _set_schema_version(self, conn: sqlite3.Connection, version: int) -> None:
        conn.execute(
            f"INSERT OR REPLACE INTO {_SCHEMA_METADATA_TABLE} (key, value) VALUES ('schema_version', ?)",
            (str(version),),
        )

    def _ensure_added_columns(self, conn: sqlite3.Connection) -> None:
        for table, columns in _COLUMN_MIGRATIONS.items():
            existing = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for column_name, column_type in columns.items():
                if column_name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_name} {column_type}")

    def _create_tables(self, conn: sqlite3.Connection) -> None:
        for ddl in _DDL:
            conn.execute(ddl)

    def _delete_canonical(self, conn: sqlite3.Connection, canonical_ref: str) -> None:
        for table_name in _CANONICAL_TABLES:
            conn.execute(f"DELETE FROM {table_name} WHERE canonical_ref = ?", (canonical_ref,))

    def _clear_index_tables(self) -> None:
        with self._transaction() as conn:
            for table_name in reversed(_CANONICAL_TABLES):
                conn.execute(f"DELETE FROM {table_name}")
