from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from .utils import ensure_directory, now_ts, stable_hash


STATE_STORE_DIR_NAME = "state"
STATE_STORE_DB_NAME = "runtime_state.sqlite"
STATE_STORE_SCHEMA_VERSION = 1
SQLITE_BUSY_TIMEOUT_MS = 5000
INDEX_DIRTY_FILE = "index_dirty.json"
_SCHEMA_METADATA_TABLE = "state_store_metadata"
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


def open_state_store(run_root: str | Path) -> "StateStore":
    return StateStore(Path(run_root).resolve())


def initialize(run_root: str | Path) -> "StateStore":
    store = open_state_store(run_root)
    store.ensure_current_schema()
    return store


def mark_index_dirty(run_root: str | Path, *, reason: str) -> None:
    state_root = ensure_state_layout(run_root)
    _write_json(
        state_root / INDEX_DIRTY_FILE,
        {"dirty": True, "reason": reason, "marked_at": now_ts()},
    )


def index_run_manifest(manifest: Any) -> None:
    payload = _payload(manifest)
    initialize(_run_root_from_payload(payload)).index_run_manifest(payload)


def index_attempt_manifest(attempt: Any) -> None:
    payload = _payload(attempt)
    initialize(_run_root_from_payload(payload)).index_attempt_manifest(payload)


def index_request_bundle(
    manifest: Any,
    *,
    request_envelope: Mapping[str, Any],
    plan_payload: Mapping[str, Any] | None = None,
    task_payload: Mapping[str, Any] | None = None,
    runtime_identity: Mapping[str, Any] | None = None,
) -> None:
    manifest_payload = _payload(manifest)
    initialize(_run_root_from_payload(manifest_payload)).index_request_bundle(
        manifest_payload,
        request_envelope=request_envelope,
        plan_payload=plan_payload,
        task_payload=task_payload,
        runtime_identity=runtime_identity,
    )


def index_checkpoint(envelope: Any, reference: Any | None = None) -> None:
    payload = _payload(envelope)
    initialize(_run_root_from_payload(payload)).index_checkpoint(
        payload,
        _payload(reference) if reference is not None else None,
    )


def index_runtime_event(
    run_root: str | Path,
    event: Any,
    *,
    canonical_path: str | Path | None = None,
) -> None:
    initialize(run_root).index_runtime_event(_payload(event), canonical_path=canonical_path)


def index_side_effect_receipt(
    run_root: str | Path,
    receipt: Any,
    *,
    canonical_path: str | Path | None = None,
) -> None:
    initialize(run_root).index_side_effect_receipt(_payload(receipt), canonical_path=canonical_path)


def index_environment_fingerprint(run_root: str | Path, fingerprint: Any) -> None:
    payload = _payload(fingerprint)
    initialize(run_root).index_environment_fingerprint(payload)


def index_recovery_attempt(run_root: str | Path, recovery_attempt: Any) -> None:
    payload = _payload(recovery_attempt)
    initialize(run_root).index_recovery_attempt(payload)


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


def rebuild_from_canonical(run_root: str | Path) -> "StateStore":
    resolved = Path(run_root).resolve()
    ensure_state_layout(resolved)
    db_path = state_db_path(resolved)
    for path in (db_path, db_path.with_name(db_path.name + "-wal"), db_path.with_name(db_path.name + "-shm")):
        if path.exists():
            path.unlink()
    store = open_state_store(resolved)
    store.ensure_current_schema()
    store.rebuild_from_canonical()
    return store


class StateStore:
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

    def _rebuild_if_dirty(self) -> None:
        if self._rebuilding_from_canonical or not self._dirty_path().exists():
            return
        self.rebuild_from_canonical()

    def rebuild_from_canonical(self) -> None:
        self._rebuilding_from_canonical = True
        try:
            self._rebuild_from_canonical()
        finally:
            self._rebuilding_from_canonical = False
        dirty_path = self._dirty_path()
        if dirty_path.exists():
            dirty_path.unlink()

    def _rebuild_from_canonical(self) -> None:
        self._clear_index_tables()
        manifest_path = self.run_root / "run_manifest.json"
        manifest = _load_optional_json(manifest_path) or {}
        if manifest:
            self.index_run_manifest(manifest)

        request_dir = self.run_root / "request"
        request_payload = _load_optional_json(request_dir / "request.json")
        if isinstance(request_payload, Mapping):
            self.index_request_bundle(
                manifest,
                request_envelope=request_payload,
                plan_payload=_load_optional_json(request_dir / "plan.json"),
                task_payload=_load_optional_json(request_dir / "task.json"),
                runtime_identity=_load_optional_json(request_dir / "runtime_identity.json"),
            )

        for attempt_path in sorted((self.run_root / "attempts").glob("*/attempt_manifest.json")):
            payload = _load_optional_json(attempt_path)
            if isinstance(payload, Mapping):
                self.index_attempt_manifest(payload)

        for checkpoint_path in sorted((self.run_root / "checkpoints").glob("*.json")):
            if checkpoint_path.name in {"index.json", "LATEST.json"}:
                continue
            payload = _load_optional_json(checkpoint_path)
            if isinstance(payload, Mapping) and payload.get("checkpoint_id"):
                self.index_checkpoint(payload, {"ref": str(checkpoint_path.resolve())})

        for working_memory_path in sorted((self.state_root / "working_memory").glob("*.json")):
            payload = _load_optional_json(working_memory_path)
            if isinstance(payload, Mapping):
                self.index_working_memory_snapshot(
                    payload,
                    checkpoint_id=working_memory_path.stem,
                    canonical_path=working_memory_path,
                )

        for event_path in sorted((self.run_root / "events").glob("*.json")):
            payload = _load_optional_json(event_path)
            if isinstance(payload, Mapping):
                self.index_runtime_event(payload, canonical_path=event_path)

        for receipt_path in sorted((self.run_root / "side_effects").glob("*.json")):
            payload = _load_optional_json(receipt_path)
            if isinstance(payload, Mapping):
                self.index_side_effect_receipt(payload, canonical_path=receipt_path)

        for fingerprint_path in sorted((self.state_root / "recovery" / "fingerprints").glob("*.json")):
            payload = _load_optional_json(fingerprint_path)
            if isinstance(payload, Mapping):
                self.index_environment_fingerprint(payload, canonical_path=fingerprint_path)

        for recovery_path in sorted((self.state_root / "recovery").glob("*.json")):
            payload = _load_optional_json(recovery_path)
            if isinstance(payload, Mapping) and payload.get("recovery_attempt_id"):
                self.index_recovery_attempt(payload, canonical_path=recovery_path)

    def index_run_manifest(self, manifest: Mapping[str, Any]) -> None:
        canonical_ref = "run_manifest.json"
        with self._transaction() as conn:
            self._delete_canonical(conn, canonical_ref)
            self._insert(
                conn,
                "runs",
                {
                    "run_id": _text(manifest.get("run_id")),
                    "run_root": str(self.run_root),
                    "request_id": _text(manifest.get("request_id")),
                    "evaluation_unit_id": _text(manifest.get("evaluation_unit_id") or manifest.get("request_id")),
                    "request_mode": _text(manifest.get("request_mode")),
                    "runtime_hash": _text(manifest.get("runtime_hash")),
                    "runtime_contract_version": _text(manifest.get("runtime_contract_version")),
                    "runtime_backend": _text(manifest.get("runtime_backend")),
                    "task_id": _none_or_text(manifest.get("task_id")),
                    "seed": _optional_int(manifest.get("seed")),
                    "current_attempt_id": _none_or_text(manifest.get("current_attempt_id")),
                    "latest_checkpoint_ref": _none_or_text(manifest.get("latest_checkpoint_ref")),
                    "lifecycle_state": _text(manifest.get("lifecycle_state")),
                    "resumable": _bool(manifest.get("resumable")),
                    "prune_eligible": _bool(manifest.get("prune_eligible")),
                    "created_at": _float(manifest.get("created_at")),
                    "updated_at": _float(manifest.get("updated_at")),
                    **_canonical("run", canonical_ref, _text(manifest.get("run_id")) or "$"),
                    "payload_json": _json_dumps(manifest),
                },
            )

    def index_attempt_manifest(self, attempt: Mapping[str, Any]) -> None:
        attempt_id = _text(attempt.get("attempt_id"))
        canonical_ref = f"attempts/{attempt_id}/attempt_manifest.json"
        with self._transaction() as conn:
            self._delete_canonical(conn, canonical_ref)
            self._insert(
                conn,
                "attempts",
                {
                    "attempt_id": attempt_id,
                    "run_id": _text(attempt.get("run_id")),
                    "sequence_no": _int(attempt.get("sequence_no")),
                    "launch_kind": _text(attempt.get("launch_kind")),
                    "lifecycle_state": _text(attempt.get("lifecycle_state")),
                    "resumed_from_checkpoint_ref": _none_or_text(attempt.get("resumed_from_checkpoint_ref")),
                    "workspace_root": _text(attempt.get("workspace_root")),
                    "latest_checkpoint_ref": _none_or_text(attempt.get("latest_checkpoint_ref")),
                    "failure_kind": _none_or_text(attempt.get("failure_kind")),
                    "started_at": _float(attempt.get("started_at")),
                    "updated_at": _float(attempt.get("updated_at")),
                    "finished_at": _optional_float(attempt.get("finished_at")),
                    **_canonical("run", canonical_ref, attempt_id or "$"),
                    "payload_json": _json_dumps(attempt),
                },
            )

    def index_request_bundle(
        self,
        manifest: Mapping[str, Any],
        *,
        request_envelope: Mapping[str, Any],
        plan_payload: Mapping[str, Any] | None = None,
        task_payload: Mapping[str, Any] | None = None,
        runtime_identity: Mapping[str, Any] | None = None,
    ) -> None:
        canonical_ref = "request/request.json"
        payload = _request_envelope_payload(request_envelope)
        execution_rows = _request_bundle_execution_rows(request_envelope, fallback_task_payload=task_payload)
        primary_row = execution_rows[0] if execution_rows else {}
        primary_payload = _mapping(primary_row.get("payload")) or payload
        primary_task_payload = _mapping(primary_row.get("task_payload")) or _mapping(task_payload)
        primary_trace_context = _mapping(primary_row.get("trace_context")) or _trace_context_from_payload(primary_payload)
        request_id = _text(_first_present(request_envelope.get("request_id"), payload.get("request_id"), manifest.get("request_id")))
        evaluation_unit_id = _text(
            _first_present(
                request_envelope.get("evaluation_unit_id"),
                payload.get("evaluation_unit_id"),
                primary_trace_context.get("evaluation_unit_id"),
                manifest.get("evaluation_unit_id"),
                request_id,
            )
        )
        request_kind = _text(request_envelope.get("request_kind"))
        request_mode = _text(_first_present(request_envelope.get("request_mode"), payload.get("mode"), manifest.get("request_mode")))
        plan_payload = _mapping(plan_payload)
        runtime_identity = _mapping(runtime_identity)
        row_task_ids = [
            _text(_mapping(row.get("task_payload")).get("task_id"))
            for row in execution_rows
            if _text(_mapping(row.get("task_payload")).get("task_id"))
        ]
        unique_task_ids = sorted(set(row_task_ids))
        task_id = _none_or_text(
            _first_present(
                manifest.get("task_id"),
                primary_task_payload.get("task_id") if len(unique_task_ids) <= 1 else None,
                unique_task_ids[0] if len(unique_task_ids) == 1 else None,
            )
        )
        seed = _optional_int(_first_present(primary_payload.get("seed"), request_envelope.get("seed"), manifest.get("seed")))
        episode_kind = _none_or_text(
            _first_present(
                primary_row.get("episode_kind"),
                _episode_kind_from_task(primary_task_payload, default=None),
            )
        )
        episode_step_index = _optional_int(primary_row.get("episode_step_index"))
        if episode_kind != "transfer_episode":
            episode_kind = None
            episode_step_index = None
        with self._transaction() as conn:
            self._delete_canonical(conn, canonical_ref)
            self._insert(
                conn,
                "requests",
                {
                    "request_id": request_id,
                    "evaluation_unit_id": evaluation_unit_id,
                    "request_kind": request_kind,
                    "request_mode": request_mode,
                    "runtime_backend": _text(_first_present(primary_payload.get("runtime_backend"), request_envelope.get("runtime_backend"), manifest.get("runtime_backend"))),
                    "task_id": task_id,
                    "seed": seed,
                    "plan_id": _none_or_text(plan_payload.get("plan_id")),
                    "runtime_hash": _none_or_text(runtime_identity.get("runtime_hash") or manifest.get("runtime_hash")),
                    **_canonical("request", canonical_ref, request_id or "$"),
                    "payload_json": _json_dumps(request_envelope),
                },
            )
            self._insert(
                conn,
                "evaluation_units",
                {
                    "evaluation_unit_id": evaluation_unit_id,
                    "request_id": request_id,
                    "episode_kind": episode_kind,
                    "episode_step_index": episode_step_index,
                    **_canonical("request", canonical_ref, evaluation_unit_id or request_id or "$"),
                    "payload_json": _json_dumps({"request": request_envelope, "plan": plan_payload, "task": task_payload}),
                },
            )
            for row in execution_rows:
                row_payload = _mapping(row.get("payload"))
                row_task_payload = _mapping(row.get("task_payload"))
                row_trace_context = _mapping(row.get("trace_context"))
                row_request_id = _text(_first_present(row.get("request_id"), row_payload.get("request_id"), row_trace_context.get("request_id"), request_id))
                row_evaluation_unit_id = _text(_first_present(row.get("evaluation_unit_id"), row_payload.get("evaluation_unit_id"), row_trace_context.get("evaluation_unit_id"), evaluation_unit_id, row_request_id))
                row_task_id = _none_or_text(row_task_payload.get("task_id"))
                row_seed = _optional_int(_first_present(row.get("seed"), row_payload.get("seed"), row_trace_context.get("seed"), seed))
                row_episode_kind = _none_or_text(
                    _first_present(
                        row.get("episode_kind"),
                        row_payload.get("episode_kind"),
                        row_trace_context.get("episode_kind"),
                        _episode_kind_from_task(row_task_payload, default=None),
                    )
                )
                row_episode_step_index = _optional_int(
                    _first_present(
                        row.get("episode_step_index"),
                        row_payload.get("episode_step_index"),
                        row_trace_context.get("episode_step_index"),
                        _episode_step_index_from_task(row_task_payload) if row_episode_kind == "transfer_episode" else None,
                    )
                )
                if row_episode_kind != "transfer_episode":
                    row_episode_kind = None
                    row_episode_step_index = None
                if row_task_id:
                    self._insert(
                        conn,
                        "tasks",
                        {
                            "task_id": row_task_id,
                            "request_id": row_request_id,
                            "evaluation_unit_id": row_evaluation_unit_id,
                            "seed": row_seed,
                            "episode_id": _none_or_text(row_task_payload.get("episode_id")),
                            "episode_order": _optional_int(row_task_payload.get("episode_order")),
                            **_canonical("request", canonical_ref, f"{row_task_id}.{row_request_id}"),
                            "payload_json": _json_dumps(row_task_payload),
                        },
                    )
                if not row_episode_kind and row_episode_step_index is None:
                    continue
                self._insert(
                    conn,
                    "episodes",
                    {
                        "evaluation_unit_id": row_evaluation_unit_id,
                        "request_id": row_request_id,
                        "episode_kind": row_episode_kind,
                        "episode_step_index": row_episode_step_index,
                        "task_id": row_task_id,
                        "seed": row_seed,
                        **_canonical("request", canonical_ref, stable_hash(row_evaluation_unit_id, row_request_id, row_task_id, row_episode_step_index)[:16]),
                        "payload_json": _json_dumps(row_payload),
                    },
                )

    def index_checkpoint(
        self,
        envelope: Mapping[str, Any],
        reference: Mapping[str, Any] | None = None,
    ) -> None:
        checkpoint_id = _text(envelope.get("checkpoint_id"))
        canonical_ref = _canonical_ref_from_reference(
            reference,
            fallback=f"checkpoints/{checkpoint_id}.json",
            run_root=self.run_root,
        )
        shell = _mapping(envelope.get("shell_state_snapshot"))
        runtime_state = _mapping(envelope.get("runtime_state_snapshot"))
        short_term = _mapping(shell.get("short_term_graph"))
        long_term = _mapping(shell.get("long_term_graph"))
        trace_cursor = _mapping(envelope.get("trace_cursor"))
        with self._transaction() as conn:
            self._delete_canonical(conn, canonical_ref)
            self._insert(
                conn,
                "checkpoints",
                {
                    "checkpoint_id": checkpoint_id,
                    "run_id": _text(envelope.get("run_id")),
                    "attempt_id": _text(envelope.get("attempt_id")),
                    "request_id": _text(envelope.get("request_id")),
                    "plan_id": _text(envelope.get("plan_id")),
                    "task_id": _text(envelope.get("task_id")),
                    "seed": _int(envelope.get("seed")),
                    "sequence_no": _int(envelope.get("sequence_no")),
                    "boundary": _text(envelope.get("boundary")),
                    "created_at": _float(envelope.get("created_at")),
                    "resume_eligible": _bool(envelope.get("resume_eligible", True)),
                    "resume_ineligibility_reason": _none_or_text(envelope.get("resume_ineligibility_reason")),
                    "source_checkpoint_ref": _none_or_text(envelope.get("source_checkpoint_ref")),
                    "environment_fingerprint_id": _none_or_text(envelope.get("environment_fingerprint_id")),
                    "latest_checkpoint_ref": _none_or_text(reference.get("ref") if reference else None),
                    **_canonical("checkpoint", canonical_ref, checkpoint_id or "$"),
                    "payload_json": _json_dumps(envelope),
                },
            )
            source_ref = _none_or_text(envelope.get("source_checkpoint_ref"))
            if source_ref:
                self._insert(
                    conn,
                    "checkpoint_lineage",
                    {
                        "checkpoint_id": checkpoint_id,
                        "source_checkpoint_ref": source_ref,
                        **_canonical("checkpoint", canonical_ref, f"{checkpoint_id}.source"),
                        "payload_json": _json_dumps({"checkpoint_id": checkpoint_id, "source_checkpoint_ref": source_ref}),
                    },
                )
            for branch_id, payload in _mapping(runtime_state.get("branch_states")).items():
                self._insert(
                    conn,
                    "branches",
                    {
                        "branch_id": str(branch_id),
                        "checkpoint_id": checkpoint_id,
                        "request_id": _text(envelope.get("request_id")),
                        "status": _text(_mapping(payload).get("status")),
                        **_canonical("checkpoint", canonical_ref, f"branch.{branch_id}"),
                        "payload_json": _json_dumps(payload),
                    },
                )
            for item in _sequence(runtime_state.get("branch_publications")):
                publication = _mapping(item)
                publication_id = _text(publication.get("publication_id")) or stable_hash(checkpoint_id, publication)[:16]
                self._insert(
                    conn,
                    "branch_publications",
                    {
                        "publication_id": publication_id,
                        "branch_id": _none_or_text(publication.get("branch_id")),
                        "checkpoint_id": checkpoint_id,
                        "request_id": _text(envelope.get("request_id")),
                        **_canonical("checkpoint", canonical_ref, publication_id),
                        "payload_json": _json_dumps(publication),
                    },
                )
            for node_id, node in _mapping(short_term.get("nodes")).items():
                payload = _mapping(node)
                self._insert(
                    conn,
                    "short_term_nodes",
                    {
                        "node_id": str(node_id),
                        "checkpoint_id": checkpoint_id,
                        "node_type": _text(payload.get("type")),
                        "label": _text(payload.get("label")),
                        **_canonical("checkpoint", canonical_ref, str(node_id)),
                        "payload_json": _json_dumps(payload),
                    },
                )
            for edge in _sequence(short_term.get("edges")):
                payload = _mapping(edge)
                edge_id = stable_hash(checkpoint_id, payload.get("src"), payload.get("dst"), payload.get("type"), payload)[:16]
                self._insert(
                    conn,
                    "short_term_edges",
                    {
                        "edge_id": edge_id,
                        "checkpoint_id": checkpoint_id,
                        "src": _text(payload.get("src")),
                        "dst": _text(payload.get("dst")),
                        "edge_type": _text(payload.get("type")),
                        **_canonical("checkpoint", canonical_ref, edge_id),
                        "payload_json": _json_dumps(payload),
                    },
                )
            for node in _sequence(long_term.get("nodes")):
                payload = _mapping(node)
                node_id = _text(payload.get("node_id"))
                self._insert(
                    conn,
                    "long_term_nodes",
                    {
                        "node_id": node_id,
                        "checkpoint_id": checkpoint_id,
                        "node_type": _text(payload.get("type")),
                        "label": _text(payload.get("label")),
                        "source_task_id": _none_or_text(payload.get("source_task_id")),
                        "tombstoned": _bool(payload.get("tombstoned")),
                        **_canonical("checkpoint", canonical_ref, node_id or "$"),
                        "payload_json": _json_dumps(payload),
                    },
                )
            for record in _sequence(long_term.get("write_records")):
                self._index_long_term_write(conn, _mapping(record), checkpoint_id, canonical_ref)
            for edge in _sequence(long_term.get("edges")):
                self._index_long_term_edge(conn, _mapping(edge), checkpoint_id, canonical_ref)
            for diagnostic in _sequence(long_term.get("retrieval_diagnostics")):
                self._index_retrieval_diagnostic(conn, _mapping(diagnostic), checkpoint_id, canonical_ref)
            self._index_working_memory_snapshot_rows(
                conn,
                _mapping(envelope.get("working_state")),
                checkpoint_id=checkpoint_id,
                canonical_ref=canonical_ref,
            )
            for call_id in _sequence(trace_cursor.get("linked_call_ids")):
                self._insert(
                    conn,
                    "trace_call_refs",
                    {
                        "call_id": str(call_id),
                        "checkpoint_id": checkpoint_id,
                        "request_id": _text(envelope.get("request_id")),
                        "session_id": _none_or_text(trace_cursor.get("last_session_id")),
                        "runtime_task_key": _none_or_text(trace_cursor.get("last_runtime_task_key")),
                        **_canonical("checkpoint", canonical_ref, f"trace.{call_id}"),
                        "payload_json": _json_dumps(trace_cursor),
                    },
                )

    def index_runtime_event(
        self,
        event: Mapping[str, Any],
        *,
        canonical_path: str | Path | None = None,
    ) -> None:
        canonical_ref = _relative_ref(self.run_root, canonical_path) if canonical_path else f"events/{_int(event.get('sequence_no')):06d}.{_text(event.get('event'))}.json"
        event_id = _text(event.get("event_id")) or stable_hash(canonical_ref, event)[:16]
        with self._transaction() as conn:
            self._delete_canonical(conn, canonical_ref)
            self._insert(
                conn,
                "runtime_events",
                {
                    "event_id": event_id,
                    "request_id": _text(event.get("request_id")),
                    "plan_id": _text(event.get("plan_id")),
                    "sequence_no": _int(event.get("sequence_no")),
                    "event": _text(event.get("event")),
                    "branch_id": _none_or_text(event.get("branch_id")),
                    "node_id": _none_or_text(event.get("node_id")),
                    "created_at": _float(event.get("created_at")),
                    **_canonical("runtime_event", canonical_ref, event_id),
                    "payload_json": _json_dumps(event),
                },
            )

    def index_side_effect_receipt(
        self,
        receipt: Mapping[str, Any],
        *,
        canonical_path: str | Path | None = None,
    ) -> None:
        receipt_id = _text(receipt.get("side_effect_id")) or stable_hash(receipt)[:16]
        canonical_ref = _relative_ref(self.run_root, canonical_path) if canonical_path else f"side_effects/{receipt_id}.json"
        result_ref = _mapping(receipt.get("result_ref"))
        with self._transaction() as conn:
            self._delete_canonical(conn, canonical_ref)
            self._insert(
                conn,
                "receipts",
                {
                    "side_effect_id": receipt_id,
                    "request_id": _text(receipt.get("request_id")),
                    "checkpoint_id": _none_or_text(receipt.get("checkpoint_id")),
                    "branch_id": _none_or_text(receipt.get("branch_id")),
                    "node_id": _none_or_text(receipt.get("node_id")),
                    "action_kind": _text(receipt.get("action_kind")),
                    "status": _text(receipt.get("status")),
                    "result_ref_json": _json_dumps(result_ref),
                    "created_at": _float(receipt.get("created_at")),
                    **_canonical("receipt", canonical_ref, receipt_id),
                    "payload_json": _json_dumps(receipt),
                },
            )
            for key in ("artifact_ref", "artifact_path", "path", "stdout_path", "stderr_path"):
                artifact_ref = str(result_ref.get(key) or "").strip()
                if not artifact_ref:
                    continue
                self._insert(
                    conn,
                    "artifacts",
                    {
                        "artifact_id": stable_hash(receipt_id, key, artifact_ref)[:16],
                        "checkpoint_id": _none_or_text(receipt.get("checkpoint_id")),
                        "receipt_id": receipt_id,
                        "artifact_kind": key,
                        "artifact_ref": artifact_ref,
                        **_canonical("receipt", canonical_ref, f"{receipt_id}.{key}"),
                        "payload_json": _json_dumps({"receipt_id": receipt_id, key: artifact_ref}),
                    },
                )

    def index_environment_fingerprint(
        self,
        fingerprint: Mapping[str, Any],
        *,
        canonical_path: str | Path | None = None,
    ) -> None:
        fingerprint_id = _text(fingerprint.get("fingerprint_id")) or stable_hash(fingerprint)[:24]
        canonical_ref = _relative_ref(self.run_root, canonical_path) if canonical_path else f"state/recovery/fingerprints/{fingerprint_id}.json"
        with self._transaction() as conn:
            self._delete_canonical(conn, canonical_ref)
            self._insert(
                conn,
                "environment_fingerprints",
                {
                    "fingerprint_id": fingerprint_id,
                    "runtime_backend": _text(fingerprint.get("runtime_backend")),
                    "runtime_hash": _text(fingerprint.get("runtime_hash")),
                    "runtime_contract_version": _text(fingerprint.get("runtime_contract_version")),
                    "captured_at": _float(fingerprint.get("captured_at")),
                    "source_attempt_id": _none_or_text(fingerprint.get("source_attempt_id")),
                    "source_checkpoint_ref": _none_or_text(fingerprint.get("source_checkpoint_ref")),
                    **_canonical("fingerprint", canonical_ref, fingerprint_id),
                    "payload_json": _json_dumps(fingerprint),
                },
            )

    def index_recovery_attempt(
        self,
        recovery_attempt: Mapping[str, Any],
        *,
        canonical_path: str | Path | None = None,
    ) -> None:
        recovery_attempt_id = _text(recovery_attempt.get("recovery_attempt_id")) or stable_hash(recovery_attempt)[:16]
        canonical_ref = _relative_ref(self.run_root, canonical_path) if canonical_path else f"state/recovery/{recovery_attempt_id}.json"
        with self._transaction() as conn:
            self._delete_canonical(conn, canonical_ref)
            self._insert(
                conn,
                "recovery_attempts",
                {
                    "recovery_attempt_id": recovery_attempt_id,
                    "run_id": _text(recovery_attempt.get("run_id")),
                    "attempt_id": _text(recovery_attempt.get("attempt_id")),
                    "selected_checkpoint_ref": _text(recovery_attempt.get("selected_checkpoint_ref")),
                    "source_checkpoint_ref": _none_or_text(recovery_attempt.get("source_checkpoint_ref")),
                    "origin_request_id": _none_or_text(recovery_attempt.get("origin_request_id")),
                    "rebound_request_id": _none_or_text(recovery_attempt.get("rebound_request_id")),
                    "reconciliation_policy": _text(recovery_attempt.get("reconciliation_policy")),
                    "compatibility_result": _text(recovery_attempt.get("compatibility_result")),
                    "source_fingerprint_id": _none_or_text(recovery_attempt.get("source_fingerprint_id")),
                    "current_fingerprint_id": _text(recovery_attempt.get("current_fingerprint_id")),
                    "attempted_at": _float(recovery_attempt.get("attempted_at")),
                    "completed_at": _optional_float(recovery_attempt.get("completed_at")),
                    **_canonical("recovery", canonical_ref, recovery_attempt_id),
                    "payload_json": _json_dumps(recovery_attempt),
                },
            )

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

    def index_grouped_trace_ref(self, payload: Mapping[str, Any]) -> None:
        session_id = _text(payload.get("session_id"))
        runtime_task_key = _text(payload.get("runtime_task_key"))
        record_id = stable_hash(session_id, runtime_task_key, payload.get("call_count"))[:16]
        canonical_ref = _text(payload.get("canonical_ref")) or f"openai_api_traces/sessions/{session_id}/index.json"
        episode_kind = _none_or_text(payload.get("episode_kind"))
        episode_step_index = _optional_int(payload.get("episode_step_index"))
        if episode_kind != "transfer_episode":
            episode_kind = None
            episode_step_index = None
        with self._transaction() as conn:
            self._insert(
                conn,
                "grouped_trace_refs",
                {
                    "record_id": record_id,
                    "session_id": session_id,
                    "evaluation_unit_id": _none_or_text(payload.get("evaluation_unit_id")),
                    "request_id": _none_or_text(payload.get("request_id")),
                    "runtime_task_key": runtime_task_key,
                    "episode_kind": episode_kind,
                    "episode_step_index": episode_step_index,
                    "call_count": _int(payload.get("call_count")),
                    "materialization_state_ref": _none_or_text(payload.get("materialization_state_ref")),
                    **_canonical("trace", canonical_ref, record_id),
                    "payload_json": _json_dumps(payload),
                },
            )

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

    def _insert(self, conn: sqlite3.Connection, table: str, values: Mapping[str, Any]) -> None:
        keys = list(values.keys())
        placeholders = ", ".join("?" for _ in keys)
        columns = ", ".join(keys)
        conn.execute(
            f"INSERT OR REPLACE INTO {table} ({columns}) VALUES ({placeholders})",
            [values[key] for key in keys],
        )

    def _index_long_term_write(
        self,
        conn: sqlite3.Connection,
        record: Mapping[str, Any],
        checkpoint_id: str,
        canonical_ref: str,
    ) -> None:
        write_id = _text(record.get("write_id")) or stable_hash(checkpoint_id, record)[:16]
        self._insert(
            conn,
            "long_term_writes",
            {
                "write_id": write_id,
                "checkpoint_id": checkpoint_id,
                "target_node_id": _text(record.get("target_node_id")),
                "action": _text(record.get("action")),
                "source_task_id": _none_or_text(record.get("source_task_id")),
                "source_attempt_id": _text(record.get("source_attempt_id")),
                "source_checkpoint_ref": _none_or_text(record.get("source_checkpoint_ref")),
                "prior_write_id": _none_or_text(record.get("prior_write_id")),
                "contradiction_target_write_id": _none_or_text(record.get("contradiction_target_write_id")),
                "payload_ref": _text(record.get("payload_ref")),
                "written_at": _float(record.get("written_at")),
                **_canonical("long_term_write", canonical_ref, write_id),
                "payload_json": _json_dumps(record),
            },
        )

    def _index_long_term_edge(
        self,
        conn: sqlite3.Connection,
        edge: Mapping[str, Any],
        checkpoint_id: str,
        canonical_ref: str,
    ) -> None:
        edge_id = _text(edge.get("edge_id")) or stable_hash(checkpoint_id, edge)[:16]
        self._insert(
            conn,
            "long_term_edges",
            {
                "edge_id": edge_id,
                "checkpoint_id": checkpoint_id,
                "source_node_id": _text(edge.get("source_node_id")),
                "target_node_id": _text(edge.get("target_node_id")),
                "edge_type": _text(edge.get("edge_type")),
                "introducing_write_id": _text(edge.get("introducing_write_id")),
                "tombstoned": _bool(edge.get("tombstoned")),
                "tombstone_write_id": _none_or_text(edge.get("tombstone_write_id")),
                "written_at": _float(edge.get("written_at")),
                **_canonical("long_term_edge", canonical_ref, edge_id),
                "payload_json": _json_dumps(edge),
            },
        )

    def _index_retrieval_diagnostic(
        self,
        conn: sqlite3.Connection,
        diagnostic: Mapping[str, Any],
        checkpoint_id: str,
        canonical_ref: str,
    ) -> None:
        diagnostic_id = _text(diagnostic.get("diagnostic_id")) or stable_hash(checkpoint_id, diagnostic)[:16]
        self._insert(
            conn,
            "retrieval_diagnostics",
            {
                "diagnostic_id": diagnostic_id,
                "checkpoint_id": checkpoint_id,
                "query_hash": _text(diagnostic.get("query_hash")),
                "task_id": _none_or_text(diagnostic.get("task_id")),
                "seed": _optional_int(diagnostic.get("seed")),
                "request_id": _none_or_text(diagnostic.get("request_id")),
                "scope_id": _none_or_text(diagnostic.get("scope_id")),
                "exact_first_preserved": _bool(diagnostic.get("exact_first_preserved")),
                "retrieved_at": _float(diagnostic.get("retrieved_at")),
                **_canonical("retrieval_diagnostic", canonical_ref, diagnostic_id),
                "payload_json": _json_dumps(diagnostic),
            },
        )
        for signal in _sequence(diagnostic.get("signals")):
            signal_payload = _mapping(signal)
            signal_id = stable_hash(diagnostic_id, signal_payload.get("node_id"), signal_payload.get("rank"))[:16]
            self._insert(
                conn,
                "retrieval_signal_rows",
                {
                    "signal_id": signal_id,
                    "diagnostic_id": diagnostic_id,
                    "node_id": _text(signal_payload.get("node_id")),
                    "rank": _int(signal_payload.get("rank")),
                    "exact_file_path_hit": _bool(signal_payload.get("exact_file_path_hit")),
                    "exact_symbol_hit": _bool(signal_payload.get("exact_symbol_hit")),
                    "node_id_match": _bool(signal_payload.get("node_id_match")),
                    "verifier_support_score": _float(signal_payload.get("verifier_support_score")),
                    "lexical_overlap_score": _float(signal_payload.get("lexical_overlap_score")),
                    "embedding_similarity_score": _float(signal_payload.get("embedding_similarity_score")),
                    "same_task_affinity_score": _float(signal_payload.get("same_task_affinity_score")),
                    "synthesized_neighbor_expansion": _bool(signal_payload.get("synthesized_neighbor_expansion")),
                    **_canonical("retrieval_diagnostic", canonical_ref, signal_id),
                    "payload_json": _json_dumps(signal_payload),
                },
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


def _request_envelope_payload(envelope: Mapping[str, Any]) -> dict[str, Any]:
    payload = _mapping(envelope.get("payload"))
    return payload if payload else _mapping(envelope)


def _trace_context_from_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    return _mapping(payload.get("trace_context"))


def _task_payload_from_execution_payload(
    payload: Mapping[str, Any],
    fallback_task_payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    task_payload = _mapping(payload.get("task"))
    if task_payload:
        return task_payload
    fallback = _mapping(fallback_task_payload)
    if fallback:
        return fallback
    if payload.get("task_id") is not None and payload.get("prompt") is not None:
        return _mapping(payload)
    return {}


def _request_bundle_execution_rows(
    envelope: Mapping[str, Any],
    *,
    fallback_task_payload: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    member_payloads = [_mapping(item) for item in _sequence(envelope.get("member_invocations")) if isinstance(item, Mapping)]
    payloads = member_payloads or [_request_envelope_payload(envelope)]
    rows: list[dict[str, Any]] = []
    for payload in payloads:
        trace_context = _trace_context_from_payload(payload)
        task_payload = _task_payload_from_execution_payload(payload, fallback_task_payload)
        episode_kind = _first_present(
            payload.get("episode_kind"),
            trace_context.get("episode_kind"),
            _episode_kind_from_task(task_payload, default=None),
        )
        episode_step_index = _first_present(
            payload.get("episode_step_index"),
            trace_context.get("episode_step_index"),
            _episode_step_index_from_task(task_payload) if episode_kind == "transfer_episode" else None,
        )
        if episode_kind != "transfer_episode":
            episode_kind = None
            episode_step_index = None
        rows.append(
            {
                "payload": payload,
                "task_payload": task_payload,
                "trace_context": trace_context,
                "request_id": _first_present(payload.get("request_id"), trace_context.get("request_id")),
                "evaluation_unit_id": _first_present(payload.get("evaluation_unit_id"), trace_context.get("evaluation_unit_id")),
                "seed": _first_present(payload.get("seed"), trace_context.get("seed")),
                "episode_kind": episode_kind,
                "episode_step_index": episode_step_index,
            }
        )
    return rows


def _episode_kind_from_task(task_payload: Mapping[str, Any], *, default: str | None = None) -> str | None:
    if _bool(task_payload.get("transfer_scored")) and _text(task_payload.get("episode_id")):
        return "transfer_episode"
    return default


def _episode_step_index_from_task(task_payload: Mapping[str, Any]) -> int | None:
    if _bool(task_payload.get("transfer_scored")) and _text(task_payload.get("episode_id")):
        return _int(task_payload.get("episode_order"))
    return None


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _payload(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if hasattr(value, "model_dump") or hasattr(value, "dict"):
        return _jsonable((value).model_dump())
    if hasattr(value, "__dict__"):
        return _jsonable(vars(value))
    return {}


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "model_dump") or hasattr(value, "dict"):
        return _jsonable((value).model_dump())
    return str(value)


def _run_root_from_payload(payload: Mapping[str, Any]) -> Path:
    run_root = str(payload.get("run_root") or "").strip()
    if not run_root:
        raise StateStoreError("state store indexing requires run_root in canonical payload")
    return Path(run_root).resolve()


def _canonical(scope: str, ref: str, record_id: str) -> dict[str, str]:
    return {
        "canonical_scope": scope,
        "canonical_ref": ref,
        "canonical_record_id": record_id,
    }


def _canonical_ref_from_reference(
    reference: Mapping[str, Any] | None,
    *,
    fallback: str,
    run_root: Path,
) -> str:
    ref = str((reference or {}).get("ref") or (reference or {}).get("checkpoint_ref") or "").strip()
    return _relative_ref(run_root, ref) if ref else fallback


def _relative_ref(run_root: Path, value: str | Path | None) -> str:
    if value is None:
        return ""
    path = Path(str(value))
    try:
        return str(path.resolve().relative_to(run_root.resolve())).replace("\\", "/")
    except Exception:
        return str(value).replace("\\", "/")


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


def _text(value: Any) -> str:
    return str(value or "").strip()


def _none_or_text(value: Any) -> str | None:
    text = _text(value)
    return text or None


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return _int(value)


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return _float(value)


def _bool(value: Any) -> int:
    return 1 if bool(value) else 0


def _json_dumps(value: Any) -> str:
    return json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":"))


def _write_json(path: Path, payload: Any) -> None:
    ensure_directory(path.parent)
    path.write_text(json.dumps(_jsonable(payload), indent=2, sort_keys=True), encoding="utf-8")


def _write_jsonl(path: Path, rows: Sequence[Any]) -> None:
    ensure_directory(path.parent)
    path.write_text("\n".join(_json_dumps(row) for row in rows) + ("\n" if rows else ""), encoding="utf-8")


def _load_optional_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _slug(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(value or "").strip())
    return (cleaned.strip("._") or "item")[:96]
