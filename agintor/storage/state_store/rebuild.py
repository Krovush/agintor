from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from ...utils import ensure_directory, now_ts, stable_hash

from .connection import (
    initialize,
    open_state_store,
)
from .layout import (
    INDEX_DIRTY_FILE,
    ensure_state_layout,
    state_db_path,
)
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

def mark_index_dirty(run_root: str | Path, *, reason: str) -> None:
    state_root = ensure_state_layout(run_root)
    _write_json(
        state_root / INDEX_DIRTY_FILE,
        {"dirty": True, "reason": reason, "marked_at": now_ts()},
    )


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


class RebuildMixin:
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
