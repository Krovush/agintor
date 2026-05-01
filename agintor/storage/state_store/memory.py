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

def index_working_memory_snapshot(
    run_root: str | Path,
    snapshot: Any,
    *,
    checkpoint_id: str,
    canonical_path: str | Path | None = None,
) -> None:
    initialize(run_root).index_working_memory_snapshot(
        _payload(snapshot),
        checkpoint_id=checkpoint_id,
        canonical_path=canonical_path,
    )


def write_memory_checkpoint_shards(run_root: str | Path, envelope: Any) -> dict[str, str]:
    payload = _payload(envelope)
    checkpoint_id = _text(payload.get("checkpoint_id")) or "checkpoint"
    state_root = ensure_state_layout(run_root)
    shell_snapshot = _mapping(payload.get("shell_state_snapshot"))
    long_term = _mapping(shell_snapshot.get("long_term_graph"))
    working_state = _mapping(payload.get("working_state"))

    refs: dict[str, str] = {}
    writes_ref = f"state/long_term/writes/{checkpoint_id}.jsonl"
    edges_ref = f"state/long_term/edges/{checkpoint_id}.jsonl"
    retrieval_ref = f"state/long_term/retrieval/{checkpoint_id}.jsonl"
    working_ref = f"state/working_memory/{checkpoint_id}.json"
    _write_jsonl(state_root.parent / writes_ref, _sequence(long_term.get("write_records")))
    _write_jsonl(state_root.parent / edges_ref, _sequence(long_term.get("edges")))
    _write_jsonl(state_root.parent / retrieval_ref, _sequence(long_term.get("retrieval_diagnostics")))
    _write_json(state_root.parent / working_ref, working_state)
    refs.update(
        {
            "long_term_writes_ref": writes_ref,
            "long_term_edges_ref": edges_ref,
            "retrieval_diagnostics_ref": retrieval_ref,
            "working_memory_ref": working_ref,
        }
    )
    return refs


def write_memory_boundary_snapshot(
    run_root: str | Path,
    *,
    boundary_id: str,
    short_term_snapshot: Mapping[str, Any] | None = None,
    long_term_snapshot: Mapping[str, Any] | None = None,
    working_memory_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    state_root = ensure_state_layout(run_root)
    safe_boundary = _slug(boundary_id or "boundary")
    refs: dict[str, str] = {}
    if short_term_snapshot is not None:
        ref = f"state/short_term/{safe_boundary}.json"
        _write_json(state_root.parent / ref, dict(short_term_snapshot))
        refs["short_term_ref"] = ref
    if long_term_snapshot is not None:
        ref = f"state/long_term/{safe_boundary}.json"
        _write_json(state_root.parent / ref, dict(long_term_snapshot))
        refs["long_term_ref"] = ref
    if working_memory_snapshot is not None:
        ref = f"state/working_memory/{safe_boundary}.json"
        _write_json(state_root.parent / ref, dict(working_memory_snapshot))
        refs["working_memory_ref"] = ref
    return refs


class MemoryMixin:
    def index_working_memory_snapshot(
        self,
        snapshot: Mapping[str, Any],
        *,
        checkpoint_id: str,
        canonical_path: str | Path | None = None,
    ) -> None:
        canonical_ref = _relative_ref(self.run_root, canonical_path) if canonical_path else f"state/working_memory/{checkpoint_id}.json"
        with self._transaction() as conn:
            self._delete_canonical(conn, canonical_ref)
            self._index_working_memory_snapshot_rows(
                conn,
                snapshot,
                checkpoint_id=checkpoint_id,
                canonical_ref=canonical_ref,
            )

    def _index_working_memory_snapshot_rows(
        self,
        conn: sqlite3.Connection,
        snapshot: Mapping[str, Any],
        *,
        checkpoint_id: str,
        canonical_ref: str,
    ) -> None:
        if not snapshot:
            return
        snapshot_id = stable_hash(checkpoint_id, snapshot.get("captured_at"), snapshot)[:16]
        self._insert(
            conn,
            "working_memory_snapshots",
            {
                "snapshot_id": snapshot_id,
                "checkpoint_id": checkpoint_id,
                "current_objective": _none_or_text(snapshot.get("current_objective")),
                "active_plan_summary": _none_or_text(snapshot.get("active_plan_summary")),
                "captured_at": _float(snapshot.get("captured_at")),
                **_canonical("working_memory", canonical_ref, snapshot_id),
                "payload_json": _json_dumps(snapshot),
            },
        )
