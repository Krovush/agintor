from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from ...utils import ensure_directory, now_ts, stable_hash

from .connection import initialize
from .layout import ensure_state_layout
from .serializers import (
    _bool,
    _canonical,
    _canonical_ref_from_reference,
    _episode_kind_from_task,
    _episode_step_index_from_task,
    _first_present,
    _float,
    _int,
    _json_dumps,
    _jsonable,
    _load_optional_json,
    _mapping,
    _none_or_text,
    _optional_float,
    _optional_int,
    _payload,
    _relative_ref,
    _request_bundle_execution_rows,
    _request_envelope_payload,
    _run_root_from_payload,
    _sequence,
    _slug,
    _task_payload_from_execution_payload,
    _text,
    _trace_context_from_payload,
    _write_json,
    _write_jsonl,
)

def latest_usable_checkpoint(
    run_root: str | Path,
    *,
    request_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any] | None:
    return initialize(run_root).latest_usable_checkpoint(request_id=request_id, run_id=run_id)


def checkpoints_by_branch_or_boundary(run_root: str | Path, **kwargs: Any) -> list[dict[str, Any]]:
    return initialize(run_root).checkpoints_by_branch_or_boundary(**kwargs)


def artifacts_for_checkpoint_or_receipt(run_root: str | Path, **kwargs: Any) -> list[dict[str, Any]]:
    return initialize(run_root).artifacts_for_checkpoint_or_receipt(**kwargs)


def branch_publication_lineage(run_root: str | Path, **kwargs: Any) -> list[dict[str, Any]]:
    return initialize(run_root).branch_publication_lineage(**kwargs)


def recovery_outcomes(run_root: str | Path, **kwargs: Any) -> list[dict[str, Any]]:
    return initialize(run_root).recovery_outcomes(**kwargs)


def long_term_writes_for_node(run_root: str | Path, node_id: str) -> list[dict[str, Any]]:
    return initialize(run_root).long_term_writes_for_node(node_id)


def retrieval_diagnostics(run_root: str | Path, **kwargs: Any) -> list[dict[str, Any]]:
    return initialize(run_root).retrieval_diagnostics(**kwargs)


def grouped_trace_status(run_root: str | Path, **kwargs: Any) -> list[dict[str, Any]]:
    return initialize(run_root).grouped_trace_status(**kwargs)


class QueryMixin:
    def latest_usable_checkpoint(
        self,
        *,
        request_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any] | None:
        clauses = ["resume_eligible = 1"]
        params: list[Any] = []
        if request_id:
            clauses.append("request_id = ?")
            params.append(request_id)
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        query = (
            "SELECT * FROM checkpoints WHERE "
            + " AND ".join(clauses)
            + " ORDER BY sequence_no DESC, created_at DESC, checkpoint_id DESC LIMIT 1"
        )
        with self._connection() as conn:
            row = conn.execute(query, params).fetchone()
        return dict(row) if row is not None else None

    def checkpoints_by_branch_or_boundary(
        self,
        *,
        branch_id: str | None = None,
        boundary: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._connection() as conn:
            if branch_id:
                rows = conn.execute(
                    """
                    SELECT DISTINCT c.* FROM checkpoints c
                    JOIN branches b ON b.checkpoint_id = c.checkpoint_id
                    WHERE b.branch_id = ?
                    ORDER BY c.sequence_no, c.created_at
                    """,
                    (branch_id,),
                ).fetchall()
            elif boundary:
                rows = conn.execute(
                    "SELECT * FROM checkpoints WHERE boundary = ? ORDER BY sequence_no, created_at",
                    (boundary,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM checkpoints ORDER BY sequence_no, created_at").fetchall()
        return [dict(row) for row in rows]

    def artifacts_for_checkpoint_or_receipt(
        self,
        *,
        checkpoint_id: str | None = None,
        receipt_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if checkpoint_id:
            clauses.append("checkpoint_id = ?")
            params.append(checkpoint_id)
        if receipt_id:
            clauses.append("receipt_id = ?")
            params.append(receipt_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection() as conn:
            rows = conn.execute(f"SELECT * FROM artifacts {where} ORDER BY artifact_kind, artifact_ref", params).fetchall()
        return [dict(row) for row in rows]

    def branch_publication_lineage(self, *, branch_id: str | None = None) -> list[dict[str, Any]]:
        with self._connection() as conn:
            if branch_id:
                rows = conn.execute(
                    "SELECT * FROM branch_publications WHERE branch_id = ? ORDER BY publication_id",
                    (branch_id,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM branch_publications ORDER BY branch_id, publication_id").fetchall()
        return [dict(row) for row in rows]

    def recovery_outcomes(self, *, compatibility_result: str | None = None) -> list[dict[str, Any]]:
        with self._connection() as conn:
            if compatibility_result:
                rows = conn.execute(
                    "SELECT * FROM recovery_attempts WHERE compatibility_result = ? ORDER BY attempted_at",
                    (compatibility_result,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM recovery_attempts ORDER BY attempted_at").fetchall()
        return [dict(row) for row in rows]

    def long_term_writes_for_node(self, node_id: str) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM long_term_writes WHERE target_node_id = ? ORDER BY written_at, write_id",
                (node_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def retrieval_diagnostics(self, *, query_hash: str | None = None) -> list[dict[str, Any]]:
        with self._connection() as conn:
            if query_hash:
                rows = conn.execute(
                    "SELECT * FROM retrieval_diagnostics WHERE query_hash = ? ORDER BY retrieved_at",
                    (query_hash,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM retrieval_diagnostics ORDER BY retrieved_at").fetchall()
        return [dict(row) for row in rows]

    def grouped_trace_status(self, *, session_id: str | None = None) -> list[dict[str, Any]]:
        with self._connection() as conn:
            if session_id:
                rows = conn.execute(
                    "SELECT * FROM grouped_trace_refs WHERE session_id = ? ORDER BY evaluation_unit_id, runtime_task_key",
                    (session_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM grouped_trace_refs ORDER BY session_id, evaluation_unit_id, runtime_task_key"
                ).fetchall()
        return [dict(row) for row in rows]
