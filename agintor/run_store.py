from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Mapping

from . import state_store
from .schemas import (
    AttemptManifest,
    CheckpointEnvelope,
    CheckpointReference,
    EnvironmentFingerprint,
    RecoveryAttempt,
    RunManifest,
    RuntimeEvent,
    SideEffectReceipt,
    WorkingMemorySnapshot,
)
from .utils import ensure_directory, now_ts, stable_hash


def _fsync_directory(path: Path) -> None:
    try:
        directory_fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    except OSError:
        pass
    finally:
        os.close(directory_fd)


def _write_json_atomic(path: Path, payload: Any) -> None:
    ensure_directory(path.parent)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(path.parent)) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
        temp_path = Path(handle.name)
    temp_path.replace(path)
    _fsync_directory(path.parent)


@dataclass(frozen=True)
class ResumeTarget:
    run_manifest: RunManifest
    checkpoint_path: Path
    checkpoint_store_dir: Path


class RunStore:
    def __init__(self, workspace: str | Path, *, run_root: str | Path | None = None) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.runs_root = ensure_directory(self.workspace / "runs").resolve()
        self.run_root = Path(run_root).resolve() if run_root is not None else None

    def _index_state_after_canonical(self, run_root: str | Path, reason: str, hook: Any) -> None:
        try:
            hook()
        except Exception:
            state_store.mark_index_dirty(run_root, reason=reason)

    @classmethod
    def from_run_root(cls, run_root: str | Path) -> "RunStore":
        resolved = Path(run_root).resolve()
        return cls(resolved.parent.parent, run_root=resolved)

    def create_run(
        self,
        *,
        request_id: str,
        evaluation_unit_id: str,
        request_mode: str,
        runtime_backend: str,
        trace_context: Mapping[str, Any] | None = None,
        task_id: str | None = None,
        seed: int | None = None,
        runtime_hash: str = "",
        runtime_contract_version: str = "",
    ) -> RunManifest:
        created_at = now_ts()
        run_key = str(evaluation_unit_id or request_id).strip() or request_id
        run_id = f"run.{int(created_at * 1000):013d}.{stable_hash(run_key, request_mode, created_at)[:12]}"
        run_root = ensure_directory(self.runs_root / run_id).resolve()
        for name in ("request", "attempts", "checkpoints", "traces", "events", "artifacts", "side_effects"):
            ensure_directory(run_root / name)
        state_store.ensure_state_layout(run_root)
        manifest = RunManifest(
            run_id=run_id,
            run_root=str(run_root),
            request_id=request_id,
            evaluation_unit_id=str(evaluation_unit_id or request_id).strip() or request_id,
            request_mode=request_mode,
            runtime_hash=runtime_hash,
            runtime_contract_version=runtime_contract_version,
            runtime_backend=runtime_backend,
            task_id=task_id,
            seed=seed,
            trace_context=trace_context,
            created_at=created_at,
            updated_at=created_at,
        )
        return self.write_run_manifest(manifest)

    def write_run_manifest(self, manifest: RunManifest) -> RunManifest:
        run_root = self.resolve_run_root(manifest.run_root)
        canonical_manifest = manifest.model_copy(update={"run_root": str(run_root.resolve())})
        _write_json_atomic(run_root / "run_manifest.json", (canonical_manifest).model_dump())
        self._index_state_after_canonical(
            run_root,
            "index_run_manifest_failed",
            lambda: state_store.index_run_manifest(canonical_manifest),
        )
        return canonical_manifest

    def load_run_manifest(self, run_ref: str | Path) -> RunManifest:
        payload = json.loads((self.resolve_run_root(run_ref) / "run_manifest.json").read_text(encoding="utf-8"))
        return (RunManifest).model_validate(payload)

    def load_attempt_manifest(self, run_ref: str | Path, attempt_id: str) -> AttemptManifest:
        payload = json.loads(
            (
                self.resolve_run_root(run_ref)
                / "attempts"
                / str(attempt_id).strip()
                / "attempt_manifest.json"
            ).read_text(encoding="utf-8")
        )
        return (AttemptManifest).model_validate(payload)

    def resolve_run_root(self, run_ref: str | Path) -> Path:
        candidate = Path(str(run_ref)).expanduser()
        if candidate.exists():
            resolved = candidate.resolve()
            if resolved.is_file():
                resolved = resolved.parent
            if (resolved / "run_manifest.json").exists() or resolved.name.startswith("run."):
                return resolved
        if self.run_root is not None:
            run_ref_text = str(run_ref).strip()
            ref_names = {Path(run_ref_text).name, PureWindowsPath(run_ref_text).name}
            if run_ref_text == str(self.run_root) or self.run_root.name in ref_names:
                return self.run_root
        resolved = (self.runs_root / str(run_ref).strip()).resolve()
        if not resolved.exists():
            raise FileNotFoundError(f"unknown run_ref: {run_ref}")
        return resolved

    def begin_attempt(
        self,
        manifest: RunManifest,
        *,
        launch_kind: str,
        resumed_from_checkpoint_ref: str | None = None,
    ) -> AttemptManifest:
        run_root = self.resolve_run_root(manifest.run_root)
        attempts_dir = ensure_directory(run_root / "attempts")
        existing = sorted(path for path in attempts_dir.glob("attempt_*") if path.is_dir())
        sequence_no = len(existing) + 1
        attempt_id = f"attempt_{sequence_no:04d}"
        attempt_dir = ensure_directory(attempts_dir / attempt_id)
        workspace_root = ensure_directory(attempt_dir / "workspace")
        timestamp = now_ts()
        attempt = AttemptManifest(
            attempt_id=attempt_id,
            run_id=manifest.run_id,
            run_root=manifest.run_root,
            sequence_no=sequence_no,
            launch_kind=launch_kind,
            resumed_from_checkpoint_ref=resumed_from_checkpoint_ref,
            workspace_root=str(workspace_root),
            started_at=timestamp,
            updated_at=timestamp,
        )
        _write_json_atomic(attempt_dir / "attempt_manifest.json", (attempt).model_dump())
        self._index_state_after_canonical(
            run_root,
            "index_attempt_manifest_failed",
            lambda: state_store.index_attempt_manifest(attempt),
        )
        self.write_run_manifest(
            manifest.model_copy(update={"current_attempt_id": attempt_id, "updated_at": timestamp})
        )
        return attempt

    def finish_attempt(
        self,
        attempt: AttemptManifest,
        *,
        lifecycle_state: str,
        latest_checkpoint_ref: str | None = None,
        failure_kind: str | None = None,
    ) -> AttemptManifest:
        timestamp = now_ts()
        updated = attempt.model_copy(
            update={
                "lifecycle_state": lifecycle_state,
                "latest_checkpoint_ref": latest_checkpoint_ref,
                "failure_kind": failure_kind,
                "updated_at": timestamp,
                "finished_at": timestamp,
            }
        )
        _write_json_atomic(
            self.resolve_run_root(updated.run_root) / "attempts" / updated.attempt_id / "attempt_manifest.json",
            (updated).model_dump(),
        )
        self._index_state_after_canonical(
            self.resolve_run_root(updated.run_root),
            "index_attempt_manifest_failed",
            lambda: state_store.index_attempt_manifest(updated),
        )
        return updated

    def write_request_bundle(
        self,
        manifest: RunManifest,
        *,
        request_envelope: Mapping[str, Any],
        plan_payload: Mapping[str, Any] | None = None,
        task_payload: Mapping[str, Any] | None = None,
        runtime_identity: Mapping[str, Any] | None = None,
    ) -> None:
        request_dir = ensure_directory(self.resolve_run_root(manifest.run_root) / "request")
        envelope = dict(request_envelope)
        envelope.setdefault("request_id", manifest.request_id)
        envelope.setdefault("evaluation_unit_id", manifest.evaluation_unit_id or manifest.request_id)
        envelope.setdefault("request_mode", manifest.request_mode)
        _write_json_atomic(request_dir / "request.json", envelope)
        if plan_payload is not None:
            _write_json_atomic(request_dir / "plan.json", dict(plan_payload))
        if task_payload is not None:
            _write_json_atomic(request_dir / "task.json", dict(task_payload))
        if runtime_identity is not None:
            _write_json_atomic(request_dir / "runtime_identity.json", dict(runtime_identity))
        self._index_state_after_canonical(
            self.resolve_run_root(manifest.run_root),
            "index_request_bundle_failed",
            lambda: state_store.index_request_bundle(
                manifest,
                request_envelope=envelope,
                plan_payload=plan_payload,
                task_payload=task_payload,
                runtime_identity=runtime_identity,
            ),
        )

    def load_request_bundle(self, run_ref: str | Path | RunManifest) -> dict[str, Any]:
        manifest = run_ref if isinstance(run_ref, RunManifest) else self.load_run_manifest(run_ref)
        request_dir = self.resolve_run_root(manifest.run_root) / "request"
        bundle: dict[str, Any] = {}
        for name in ("request", "plan", "task", "runtime_identity"):
            path = request_dir / f"{name}.json"
            if path.exists():
                bundle[name] = json.loads(path.read_text(encoding="utf-8"))
        return bundle

    def load_checkpoint_envelope(self, checkpoint_ref: str | Path) -> CheckpointEnvelope:
        checkpoint_path = Path(str(checkpoint_ref)).expanduser().resolve()
        payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        return (CheckpointEnvelope).model_validate(payload)

    def write_checkpoint(self, envelope: CheckpointEnvelope) -> CheckpointReference:
        run_root = self.resolve_run_root(envelope.run_root)
        checkpoints_dir = ensure_directory(run_root / "checkpoints")
        checkpoint_path = checkpoints_dir / f"{envelope.checkpoint_id}.json"
        _write_json_atomic(checkpoint_path, (envelope).model_dump())
        self._index_state_after_canonical(
            run_root,
            "write_memory_checkpoint_shards_failed",
            lambda: state_store.write_memory_checkpoint_shards(run_root, envelope),
        )
        index_path = checkpoints_dir / "index.json"
        rows = []
        if index_path.exists():
            payload = json.loads(index_path.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                rows = [dict(row) for row in payload if isinstance(row, dict)]
        ref = CheckpointReference(
            ref=str(checkpoint_path.resolve()),
            run_id=envelope.run_id,
            run_root=envelope.run_root,
            attempt_id=envelope.attempt_id,
            task_id=envelope.task_id,
            seed=envelope.seed,
            request_id=envelope.request_id,
            plan_id=envelope.plan_id,
            checkpoint_id=envelope.checkpoint_id,
            sequence_no=envelope.sequence_no,
            boundary=envelope.boundary,
            created_at=envelope.created_at,
            checkpoint_count=len(rows) + 1,
            latest=False,
            resume_eligible=bool(envelope.resume_eligible),
            resume_ineligibility_reason=envelope.resume_ineligibility_reason,
        )
        rows = [row for row in rows if row.get("checkpoint_id") != envelope.checkpoint_id]
        rows.append((ref).model_dump())
        rows.sort(key=lambda row: (int(row.get("sequence_no", 0) or 0), float(row.get("created_at", 0.0) or 0.0)))
        latest_eligible_ref: str | None = None
        latest_eligible_id: str | None = None
        for row in reversed(rows):
            if not bool(row.get("resume_eligible", True)):
                continue
            latest_eligible_ref = str(row.get("ref") or "").strip() or None
            latest_eligible_id = str(row.get("checkpoint_id") or "").strip() or None
            if latest_eligible_ref:
                break
        for row in rows:
            row["latest"] = bool(latest_eligible_id) and str(row.get("checkpoint_id") or "").strip() == latest_eligible_id
        _write_json_atomic(index_path, rows)
        latest_path = checkpoints_dir / "LATEST.json"
        if latest_eligible_ref:
            latest_row = next(
                row for row in rows if str(row.get("checkpoint_id") or "").strip() == latest_eligible_id
            )
            _write_json_atomic(latest_path, latest_row)
        elif latest_path.exists():
            latest_path.unlink()
        manifest = self.load_run_manifest(run_root)
        self.write_run_manifest(
            manifest.model_copy(
                update={
                    "current_attempt_id": envelope.attempt_id,
                    "latest_checkpoint_ref": latest_eligible_ref,
                    "resumable": bool(latest_eligible_ref),
                    "updated_at": now_ts(),
                }
            )
        )
        self._index_state_after_canonical(
            run_root,
            "index_checkpoint_failed",
            lambda: state_store.index_checkpoint(envelope, ref),
        )
        return ref

    def write_runtime_event(self, run_ref: str | Path, event: RuntimeEvent) -> Path:
        run_root = self.resolve_run_root(run_ref)
        event_dir = ensure_directory(run_root / "events")
        sequence_no = int(event.sequence_no or 0)
        event_name = str(event.event or "runtime_event").strip() or "runtime_event"
        path = event_dir / f"{sequence_no:06d}.{event_name}.json"
        _write_json_atomic(path, (event).model_dump())
        self._index_state_after_canonical(
            run_root,
            "index_runtime_event_failed",
            lambda: state_store.index_runtime_event(run_root, event, canonical_path=path),
        )
        return path

    def write_side_effect_receipt(self, run_ref: str | Path, receipt: SideEffectReceipt) -> Path:
        run_root = self.resolve_run_root(run_ref)
        receipt_dir = ensure_directory(run_root / "side_effects")
        path = receipt_dir / f"{receipt.side_effect_id}.json"
        _write_json_atomic(path, (receipt).model_dump())
        self._index_state_after_canonical(
            run_root,
            "index_side_effect_receipt_failed",
            lambda: state_store.index_side_effect_receipt(run_root, receipt, canonical_path=path),
        )
        return path

    def write_environment_fingerprint(
        self,
        run_ref: str | Path,
        fingerprint: EnvironmentFingerprint,
    ) -> Path:
        run_root = self.resolve_run_root(run_ref)
        path = ensure_directory(run_root / "state" / "recovery" / "fingerprints") / f"{fingerprint.fingerprint_id}.json"
        _write_json_atomic(path, (fingerprint).model_dump())
        self._index_state_after_canonical(
            run_root,
            "index_environment_fingerprint_failed",
            lambda: state_store.index_environment_fingerprint(run_root, fingerprint),
        )
        return path

    def load_environment_fingerprint(
        self,
        run_ref: str | Path,
        fingerprint_id: str | None,
    ) -> EnvironmentFingerprint | None:
        text = str(fingerprint_id or "").strip()
        if not text:
            return None
        path = self.resolve_run_root(run_ref) / "state" / "recovery" / "fingerprints" / f"{text}.json"
        if not path.exists():
            return None
        return (EnvironmentFingerprint).model_validate(json.loads(path.read_text(encoding="utf-8")))

    def write_recovery_attempt(self, run_ref: str | Path, recovery_attempt: RecoveryAttempt) -> Path:
        run_root = self.resolve_run_root(run_ref)
        path = ensure_directory(run_root / "state" / "recovery") / f"{recovery_attempt.recovery_attempt_id}.json"
        _write_json_atomic(path, (recovery_attempt).model_dump())
        self._index_state_after_canonical(
            run_root,
            "index_recovery_attempt_failed",
            lambda: state_store.index_recovery_attempt(run_root, recovery_attempt),
        )
        return path

    def write_working_memory_snapshot(
        self,
        run_ref: str | Path,
        snapshot: WorkingMemorySnapshot,
        *,
        checkpoint_id: str,
    ) -> Path:
        run_root = self.resolve_run_root(run_ref)
        path = ensure_directory(run_root / "state" / "working_memory") / f"{checkpoint_id}.json"
        _write_json_atomic(path, (snapshot).model_dump())
        self._index_state_after_canonical(
            run_root,
            "index_working_memory_snapshot_failed",
            lambda: state_store.index_working_memory_snapshot(
                run_root,
                snapshot,
                checkpoint_id=checkpoint_id,
                canonical_path=path,
            ),
        )
        return path

    def write_memory_checkpoint_shards(self, run_ref: str | Path, envelope: CheckpointEnvelope) -> dict[str, str]:
        run_root = self.resolve_run_root(run_ref)
        return state_store.write_memory_checkpoint_shards(run_root, envelope)

    def write_memory_boundary_snapshot(
        self,
        run_ref: str | Path,
        *,
        boundary_id: str,
        short_term_snapshot: Mapping[str, Any] | None = None,
        long_term_snapshot: Mapping[str, Any] | None = None,
        working_memory_snapshot: Mapping[str, Any] | None = None,
    ) -> dict[str, str]:
        return state_store.write_memory_boundary_snapshot(
            self.resolve_run_root(run_ref),
            boundary_id=boundary_id,
            short_term_snapshot=short_term_snapshot,
            long_term_snapshot=long_term_snapshot,
            working_memory_snapshot=working_memory_snapshot,
        )

    def latest_checkpoint_ref(self, run_ref: str | Path) -> str | None:
        latest_path = self.resolve_run_root(run_ref) / "checkpoints" / "LATEST.json"
        if not latest_path.exists():
            return None
        payload = json.loads(latest_path.read_text(encoding="utf-8"))
        return str(payload.get("ref") or "").strip() or None

    def latest_usable_checkpoint_ref(self, run_ref: str | Path) -> str | None:
        run_root = self.resolve_run_root(run_ref)
        try:
            manifest = self.load_run_manifest(run_root)
        except Exception:
            manifest = None
        candidates: list[str] = []
        seen: set[str] = set()

        def add_candidate(candidate: str | Path | None) -> None:
            text = str(candidate or "").strip()
            if not text:
                return
            normalized = str(Path(text).expanduser())
            if normalized in seen:
                return
            seen.add(normalized)
            candidates.append(text)

        if manifest is not None:
            add_candidate(manifest.latest_checkpoint_ref)
            current_attempt_id = str(manifest.current_attempt_id or "").strip()
            if current_attempt_id:
                try:
                    attempt = self.load_attempt_manifest(run_root, current_attempt_id)
                except Exception:
                    attempt = None
                if attempt is not None:
                    add_candidate(attempt.latest_checkpoint_ref)
        add_candidate(self.latest_checkpoint_ref(run_root))
        index_path = run_root / "checkpoints" / "index.json"
        if index_path.exists():
            payload = json.loads(index_path.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                rows = sorted(
                    (dict(row) for row in payload if isinstance(row, dict)),
                    key=lambda row: (int(row.get("sequence_no", 0) or 0), float(row.get("created_at", 0.0) or 0.0)),
                    reverse=True,
                )
                for row in rows:
                    if "resume_eligible" in row and not bool(row.get("resume_eligible")):
                        continue
                    add_candidate(str(row.get("ref") or row.get("checkpoint_ref") or "").strip())
        manifest_latest_ref = str(getattr(manifest, "latest_checkpoint_ref", "") or "").strip() if manifest is not None else ""
        for candidate in candidates:
            try:
                envelope = self.load_checkpoint_envelope(candidate)
            except Exception:
                continue
            if not bool(getattr(envelope, "resume_eligible", True)):
                continue
            checkpoint_path = Path(str(candidate)).expanduser().resolve()
            if not checkpoint_path.exists():
                continue
            if manifest is not None and str(envelope.run_id or "").strip() == str(manifest.run_id):
                return str(checkpoint_path)
            if manifest_latest_ref:
                try:
                    if checkpoint_path == Path(manifest_latest_ref).expanduser().resolve():
                        return str(checkpoint_path)
                except Exception:
                    pass
            envelope_run_root = str(envelope.run_root or "").strip()
            if envelope_run_root:
                try:
                    if self.resolve_run_root(envelope_run_root) == run_root:
                        return str(checkpoint_path)
                except Exception:
                    pass
            if run_root in checkpoint_path.parents:
                return str(checkpoint_path)
        return None

    def checkpoint_ref_is_resume_eligible(self, checkpoint_ref: str | Path | None) -> bool:
        text = str(checkpoint_ref or "").strip()
        if not text:
            return False
        try:
            envelope = self.load_checkpoint_envelope(text)
        except Exception:
            return False
        return bool(getattr(envelope, "resume_eligible", True))

    def resolve_resume_target(
        self,
        *,
        run_ref: str | Path | None = None,
        checkpoint_ref: str | Path | None = None,
    ) -> ResumeTarget:
        if checkpoint_ref is not None and str(checkpoint_ref).strip():
            checkpoint_path = Path(str(checkpoint_ref)).expanduser().resolve()
            checkpoint_store_dir = checkpoint_path.parent.resolve()
            try:
                run_root = self._find_run_root_for_checkpoint(checkpoint_path)
                return ResumeTarget(self.load_run_manifest(run_root), checkpoint_path, checkpoint_store_dir)
            except FileNotFoundError:
                envelope = self.load_checkpoint_envelope(checkpoint_path)
                manifest_refs: list[str] = []
                embedded_run_root = str(envelope.run_root or "").strip()
                if embedded_run_root:
                    try:
                        manifest_refs.append(str(self.resolve_run_root(embedded_run_root)))
                    except FileNotFoundError:
                        pass
                embedded_run_id = str(envelope.run_id or "").strip()
                if embedded_run_id and embedded_run_id not in manifest_refs:
                    manifest_refs.append(embedded_run_id)
                last_error: FileNotFoundError | None = None
                for manifest_ref in manifest_refs:
                    try:
                        manifest = self.load_run_manifest(manifest_ref)
                    except FileNotFoundError as exc:
                        last_error = exc
                        continue
                    if embedded_run_id and str(manifest.run_id or "").strip() != embedded_run_id:
                        continue
                    return ResumeTarget(manifest, checkpoint_path, checkpoint_store_dir)
                if last_error is not None:
                    raise last_error
                raise FileNotFoundError(
                    f"checkpoint {checkpoint_path} does not identify a durable run manifest"
                )
        if run_ref is None or not str(run_ref).strip():
            raise FileNotFoundError("resume requires run_ref or checkpoint_ref")
        manifest = self.load_run_manifest(run_ref)
        latest_ref = self.latest_usable_checkpoint_ref(manifest.run_root)
        if not latest_ref:
            raise FileNotFoundError(f"run {manifest.run_id} has no usable published checkpoint")
        checkpoint_path = Path(latest_ref).resolve()
        return ResumeTarget(manifest, checkpoint_path, checkpoint_path.parent.resolve())

    def finish_run(
        self,
        manifest: RunManifest,
        *,
        lifecycle_state: str,
        latest_checkpoint_ref: str | None,
        resumable: bool,
        failure_kind: str | None = None,
    ) -> RunManifest:
        updated = manifest.model_copy(
            update={
                "lifecycle_state": lifecycle_state,
                "latest_checkpoint_ref": latest_checkpoint_ref,
                "resumable": resumable,
                "prune_eligible": lifecycle_state == "failed" and not resumable,
                "last_failure_kind": failure_kind,
                "updated_at": now_ts(),
            }
        )
        return self.write_run_manifest(updated)

    def prune_run(self, manifest: RunManifest) -> RunManifest:
        run_root = self.resolve_run_root(manifest.run_root)
        for name in ("checkpoints", "traces", "events", "artifacts", "side_effects", "state"):
            shutil.rmtree(run_root / name, ignore_errors=True)
        for attempt_dir in (run_root / "attempts").glob("attempt_*"):
            shutil.rmtree(attempt_dir / "workspace", ignore_errors=True)
        updated = manifest.model_copy(
            update={
                "lifecycle_state": "pruned",
                "latest_checkpoint_ref": None,
                "resumable": False,
                "prune_eligible": False,
                "updated_at": now_ts(),
            }
        )
        return self.write_run_manifest(updated)

    def _find_run_root_for_checkpoint(self, checkpoint_path: Path) -> Path:
        for parent in checkpoint_path.parents:
            if (parent / "run_manifest.json").exists():
                return parent
        raise FileNotFoundError(f"checkpoint {checkpoint_path} is not inside a durable run root")
