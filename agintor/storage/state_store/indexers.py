from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from ...utils import ensure_directory, now_ts, stable_hash

from .connection import initialize
from .schema import _CANONICAL_TABLES
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


class IndexerMixin:
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
