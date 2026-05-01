from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from .....storage import state_store
from .....storage.artifacts import ArtifactMode, ArtifactPolicy
from .....providers import (
    ModelProvider,
    provider_environment_names_for_instance,
    provider_payload,
    provider_payload_file_paths,
    rewrite_provider_payload_file_paths,
)
from ....loader import resolve_docker_launch_policy
from ....profile import RuntimeProfile
from ....sdk import KERNEL_BUNDLE_DIR
from .....storage.run_store import RunStore
from .....contracts import (
    AsyncHandle,
    AttemptManifest,
    BenchmarkTask,
    CapabilityExchange,
    CheckpointEnvelope,
    CheckpointReference,
    InspectRequest,
    OpenAITraceContext,
    OpenHandleTableSnapshot,
    RequestFileRef,
    ResumeRequest,
    RunManifest,
    RunResult,
    RuntimeBatchRequest,
    RuntimeBatchResponse,
    RuntimeResumeRequest,
    RuntimeSolveRequest,
    RuntimeSolveResponse,
    RuntimeTaskInvocation,
    ShellStateSnapshot,
    SideEffectReceipt,
)
from ....api import compile_request_file_ref, normalize_benchmark_request_id
from .....utils import ensure_directory, file_digest, stable_hash

from .path_mapping import DockerPathMappingMixin

class DockerCheckpointRewriteMixin:
    @classmethod
    def _container_resume_request(
        cls,
        request: RuntimeResumeRequest,
        *,
        runtime_path: Path | None = None,
    ) -> tuple[RuntimeResumeRequest, Path | None, Path | None]:
        run_mount_root = cls._common_run_mount_root([request.run_root])
        checkpoint_store_dir: Path | None = None
        checkpoint_ref = request.checkpoint_ref
        checkpoint_store = str(request.checkpoint_store_dir or "").strip()
        if str(checkpoint_ref or "").strip():
            rewritten = (
                cls._container_run_path(checkpoint_ref, run_mount_root)
                if run_mount_root is not None
                else None
            )
            if rewritten is not None and rewritten != checkpoint_ref:
                checkpoint_ref = rewritten
                checkpoint_store_dir = run_mount_root
                checkpoint_store = cls.RUNS_MOUNT_ROOT
            else:
                checkpoint_path = Path(request.checkpoint_ref).resolve()
                checkpoint_store_dir = (
                    Path(checkpoint_store).resolve()
                    if checkpoint_store
                    else checkpoint_path.parent
                )
                try:
                    relative_ref = checkpoint_path.relative_to(checkpoint_store_dir)
                except ValueError:
                    checkpoint_store_dir = checkpoint_path.parent
                    relative_ref = Path(checkpoint_path.name)
                checkpoint_ref = f"/mnt/checkpoints/{relative_ref.as_posix()}"
                checkpoint_store = "/mnt/checkpoints"
        trace_context = cls._containerize_resume_trace_context(
            request.trace_context,
            runtime_path=runtime_path,
        )
        return (
            request.model_copy(
                update={
                    "checkpoint_ref": checkpoint_ref,
                    "checkpoint_store_dir": checkpoint_store,
                    "run_root": cls._container_run_path(request.run_root, run_mount_root) or "",
                    "trace_context": trace_context,
                }
            ),
            checkpoint_store_dir,
            run_mount_root,
        )

    @classmethod
    def _materialize_container_resume_checkpoint(
        cls,
        container_request: RuntimeResumeRequest,
        host_request: RuntimeResumeRequest,
        *,
        run_dir: Path,
        run_mount_root: Path | None,
        checkpoint_store_dir: Path | None,
    ) -> tuple[RuntimeResumeRequest, Path | None, Path | None]:
        checkpoint_ref = str(host_request.checkpoint_ref or "").strip()
        if not checkpoint_ref:
            return container_request, checkpoint_store_dir, checkpoint_store_dir
        host_checkpoint_path = Path(checkpoint_ref).resolve()
        host_checkpoint_store = (
            Path(host_request.checkpoint_store_dir).resolve()
            if str(host_request.checkpoint_store_dir or "").strip()
            else checkpoint_store_dir.resolve()
            if checkpoint_store_dir is not None
            else host_checkpoint_path.parent
        )
        try:
            relative_ref = host_checkpoint_path.relative_to(host_checkpoint_store)
        except ValueError:
            relative_ref = Path(host_checkpoint_path.name)
        payload = json.loads(host_checkpoint_path.read_text(encoding="utf-8"))
        envelope = (CheckpointEnvelope).model_validate_persisted(payload)
        containerized = cls._containerize_checkpoint_envelope_paths(
            envelope,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=host_checkpoint_store,
        )
        temp_checkpoint_store = ensure_directory(run_dir / "checkpoint_store")
        temp_checkpoint_path = temp_checkpoint_store / relative_ref
        ensure_directory(temp_checkpoint_path.parent)
        temp_checkpoint_path.write_text(
            json.dumps((containerized).model_dump(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return (
            container_request.model_copy(
                update={
                    "checkpoint_ref": f"/mnt/checkpoints/{relative_ref.as_posix()}",
                    "checkpoint_store_dir": "/mnt/checkpoints",
                }
            ),
            temp_checkpoint_store,
            host_checkpoint_store,
        )

    def _resolve_resume_checkpoint_request(
        self,
        request: RuntimeResumeRequest,
    ) -> RuntimeResumeRequest:
        checkpoint_ref = str(request.checkpoint_ref or "").strip()
        run_ref = str(request.run_ref or "").strip()
        run_root = str(request.run_root or "").strip()
        if not checkpoint_ref and not run_ref and not run_root:
            return request
        stores: list[RunStore] = []
        if run_root:
            stores.append(RunStore.from_run_root(run_root))
        stores.append(RunStore(self.run_store_workspace))
        errors: list[str] = []
        target = None
        resolved_run_root = None
        for store in stores:
            try:
                target = store.resolve_resume_target(
                    run_ref=run_ref or run_root or None,
                    checkpoint_ref=checkpoint_ref or None,
                )
                resolved_run_root = store.resolve_run_root(target.run_manifest.run_root)
                break
            except Exception as exc:
                errors.append(str(exc))
        if target is None or resolved_run_root is None:
            if checkpoint_ref:
                return request
            detail = f": {'; '.join(error for error in errors if error)}" if errors else ""
            raise FileNotFoundError(
                f"docker resume could not resolve a checkpoint for run_ref={run_ref!r} run_root={run_root!r}{detail}"
            )
        updates = {
            "checkpoint_ref": str(target.checkpoint_path.resolve()),
            "checkpoint_store_dir": str(target.checkpoint_store_dir.resolve()),
            "run_root": str(resolved_run_root.resolve()),
        }
        if not str(request.run_id or "").strip():
            updates["run_id"] = target.run_manifest.run_id
        return request.model_copy(update=updates)

    @classmethod
    def _checkpoint_request_file_refs(
        cls,
        checkpoint_ref: str | Path | None,
    ) -> list[RequestFileRef]:
        text = str(checkpoint_ref or "").strip()
        if not text:
            return []
        checkpoint_path = Path(text).expanduser().resolve()
        if not checkpoint_path.exists():
            return []
        payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        envelope = (CheckpointEnvelope).model_validate_persisted(payload)
        request_file_refs_payload = envelope.plan_snapshot.get("file_ref_specs", [])
        if isinstance(request_file_refs_payload, list) and request_file_refs_payload:
            return [
                (RequestFileRef).model_validate(row)
                for row in request_file_refs_payload
                if isinstance(row, Mapping)
            ]
        task_metadata = envelope.task_payload.get("metadata", {}) if isinstance(envelope.task_payload, Mapping) else {}
        metadata_refs = task_metadata.get("request_file_refs", []) if isinstance(task_metadata, Mapping) else []
        if isinstance(metadata_refs, list) and metadata_refs:
            return [
                (RequestFileRef).model_validate(row)
                for row in metadata_refs
                if isinstance(row, Mapping)
            ]
        task_file_paths = envelope.task_payload.get("file_paths", []) if isinstance(envelope.task_payload, Mapping) else []
        absolute_paths = [str(path).strip() for path in task_file_paths if Path(str(path)).is_absolute()]
        return [compile_request_file_ref(path) for path in absolute_paths]

    @classmethod
    def _container_checkpoint_path(cls, path_text: str | None, checkpoint_store_dir: Path | None) -> str | None:
        if not path_text or checkpoint_store_dir is None:
            return path_text
        path_text = str(path_text)
        if path_text.startswith("/mnt/"):
            return path_text
        try:
            path = Path(path_text).resolve()
            relative = path.relative_to(checkpoint_store_dir.resolve())
        except (OSError, ValueError):
            return path_text
        if str(relative) == ".":
            return "/mnt/checkpoints"
        return f"/mnt/checkpoints/{relative.as_posix()}"

    @classmethod
    def _rewrite_async_handle_paths(
        cls,
        handle: AsyncHandle,
        *,
        run_mount_root: Path | None = None,
        checkpoint_store_dir: Path | None = None,
    ) -> AsyncHandle:
        payload = (handle).model_dump()
        payload["working_directory"] = cls._rewrite_known_path(
            handle.working_directory,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        ) or ""
        payload["stdout_path"] = cls._rewrite_known_path(
            handle.stdout_path,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        )
        payload["stderr_path"] = cls._rewrite_known_path(
            handle.stderr_path,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        )
        payload["artifact_refs"] = [
            cls._rewrite_known_path(
                ref,
                run_mount_root=run_mount_root,
                checkpoint_store_dir=checkpoint_store_dir,
            )
            or ref
            for ref in handle.artifact_refs
        ]
        return (AsyncHandle).model_validate(payload)

    @classmethod
    def _containerize_async_handle_paths(
        cls,
        handle: AsyncHandle,
        *,
        run_mount_root: Path | None = None,
        checkpoint_store_dir: Path | None = None,
    ) -> AsyncHandle:
        payload = (handle).model_dump()
        payload["working_directory"] = cls._container_known_path(
            handle.working_directory,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        ) or ""
        payload["stdout_path"] = cls._container_known_path(
            handle.stdout_path,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        )
        payload["stderr_path"] = cls._container_known_path(
            handle.stderr_path,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        )
        payload["artifact_refs"] = [
            cls._container_known_path(
                ref,
                run_mount_root=run_mount_root,
                checkpoint_store_dir=checkpoint_store_dir,
            )
            or ref
            for ref in handle.artifact_refs
        ]
        return (AsyncHandle).model_validate(payload)

    @staticmethod
    def _open_handle_payloads(value: Any) -> list[Any]:
        if hasattr(value, "handles"):
            return list(getattr(value, "handles") or [])
        if isinstance(value, Mapping):
            return list(value.get("handles", []) or [])
        if isinstance(value, list):
            return list(value)
        return []

    @classmethod
    def _rewrite_working_memory_snapshot_paths(
        cls,
        snapshot_payload: Mapping[str, Any],
        *,
        run_mount_root: Path | None = None,
        checkpoint_store_dir: Path | None = None,
    ) -> dict[str, Any]:
        payload = dict(snapshot_payload)
        selected_refs = payload.get("selected_checkpoint_refs", [])
        if isinstance(selected_refs, list):
            payload["selected_checkpoint_refs"] = [
                cls._rewrite_known_path(
                    str(ref),
                    run_mount_root=run_mount_root,
                    checkpoint_store_dir=checkpoint_store_dir,
                )
                or str(ref)
                for ref in selected_refs
            ]
        return payload

    @classmethod
    def _rewrite_recovery_payload_paths(
        cls,
        payload: Any,
        *,
        run_mount_root: Path | None = None,
        checkpoint_store_dir: Path | None = None,
    ) -> Any:
        if not isinstance(payload, Mapping):
            return payload
        result = dict(payload)
        for key in ("selected_checkpoint_ref", "source_checkpoint_ref"):
            if key not in result:
                continue
            result[key] = cls._rewrite_known_path(
                result.get(key),
                run_mount_root=run_mount_root,
                checkpoint_store_dir=checkpoint_store_dir,
            ) or result.get(key)
        return result

    @classmethod
    def _rewrite_branch_resume_snapshot_paths(
        cls,
        snapshot_payload: Mapping[str, Any],
        *,
        run_mount_root: Path | None = None,
        checkpoint_store_dir: Path | None = None,
    ) -> dict[str, Any]:
        payload = dict(snapshot_payload)
        payload["artifacts"] = dict(payload.get("artifacts") or {})
        payload["side_effect_receipts"] = [
            cls._copy_side_effect_receipt_payload(receipt_payload)
            for receipt_payload in list(payload.get("side_effect_receipts", []) or [])
        ]
        payload["shell_state_snapshot"] = cls._rewrite_shell_state_snapshot_paths(
            payload.get("shell_state_snapshot"),
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        )
        return payload

    @classmethod
    def _rewrite_shell_state_snapshot_paths(
        cls,
        snapshot_payload: Mapping[str, Any] | ShellStateSnapshot | None,
        *,
        run_mount_root: Path | None = None,
        checkpoint_store_dir: Path | None = None,
    ) -> dict[str, Any]:
        if snapshot_payload is None:
            return {}
        if isinstance(snapshot_payload, ShellStateSnapshot):
            normalized = snapshot_payload
        else:
            try:
                normalized = (ShellStateSnapshot).model_validate(snapshot_payload)
            except Exception:
                return dict(snapshot_payload)
        rewritten_handles = [
            cls._rewrite_async_handle_paths(
                handle,
                run_mount_root=run_mount_root,
                checkpoint_store_dir=checkpoint_store_dir,
            )
            for handle in normalized.open_handles.handles
        ]
        rewritten = normalized.model_copy(
            update={
                "open_handles": OpenHandleTableSnapshot(handles=rewritten_handles),
            }
        )
        return (rewritten).model_dump()

    @classmethod
    def _containerize_shell_state_snapshot_paths(
        cls,
        snapshot_payload: Mapping[str, Any] | ShellStateSnapshot | None,
        *,
        run_mount_root: Path | None = None,
        checkpoint_store_dir: Path | None = None,
    ) -> dict[str, Any]:
        if snapshot_payload is None:
            return {}
        if isinstance(snapshot_payload, ShellStateSnapshot):
            normalized = snapshot_payload
        else:
            normalized = (ShellStateSnapshot).model_validate(snapshot_payload)
        container_handles = [
            cls._containerize_async_handle_paths(
                handle,
                run_mount_root=run_mount_root,
                checkpoint_store_dir=checkpoint_store_dir,
            )
            for handle in normalized.open_handles.handles
        ]
        containerized = normalized.model_copy(
            update={
                "open_handles": OpenHandleTableSnapshot(handles=container_handles),
            }
        )
        return (containerized).model_dump()

    @classmethod
    def _containerize_branch_resume_snapshot_paths(
        cls,
        snapshot_payload: Mapping[str, Any],
        *,
        run_mount_root: Path | None = None,
        checkpoint_store_dir: Path | None = None,
    ) -> dict[str, Any]:
        payload = dict(snapshot_payload)
        payload["shell_state_snapshot"] = cls._containerize_shell_state_snapshot_paths(
            payload.get("shell_state_snapshot"),
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        )
        return payload

    @classmethod
    def _containerize_working_memory_snapshot_paths(
        cls,
        snapshot_payload: Mapping[str, Any],
        *,
        run_mount_root: Path | None = None,
        checkpoint_store_dir: Path | None = None,
    ) -> dict[str, Any]:
        payload = dict(snapshot_payload)
        selected_refs = payload.get("selected_checkpoint_refs", [])
        if isinstance(selected_refs, list):
            payload["selected_checkpoint_refs"] = [
                cls._container_known_path(
                    str(ref),
                    run_mount_root=run_mount_root,
                    checkpoint_store_dir=checkpoint_store_dir,
                )
                or str(ref)
                for ref in selected_refs
            ]
        return payload

    @classmethod
    def _containerize_checkpoint_envelope_paths(
        cls,
        envelope: CheckpointEnvelope,
        *,
        run_mount_root: Path | None = None,
        checkpoint_store_dir: Path | None = None,
    ) -> CheckpointEnvelope:
        payload = (envelope).model_dump()
        payload["run_root"] = cls._container_known_path(
            envelope.run_root,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        ) or ""
        payload["source_checkpoint_ref"] = cls._container_known_path(
            envelope.source_checkpoint_ref,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        )
        payload["selected_checkpoint_ref"] = cls._container_known_path(
            envelope.selected_checkpoint_ref,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        )
        payload["runtime_state_snapshot"]["latest_checkpoint_ref"] = cls._container_known_path(
            envelope.runtime_state_snapshot.latest_checkpoint_ref,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        )
        payload["attempt_snapshot"]["run_root"] = cls._container_known_path(
            envelope.attempt_snapshot.run_root,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        ) or envelope.attempt_snapshot.run_root
        payload["attempt_snapshot"]["resumed_from_checkpoint_ref"] = cls._container_known_path(
            envelope.attempt_snapshot.resumed_from_checkpoint_ref,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        ) or envelope.attempt_snapshot.resumed_from_checkpoint_ref
        payload["runtime_state_snapshot"]["branch_resume_snapshots"] = {
            str(key): cls._containerize_branch_resume_snapshot_paths(
                dict(value),
                run_mount_root=run_mount_root,
                checkpoint_store_dir=checkpoint_store_dir,
            )
            for key, value in dict(payload["runtime_state_snapshot"].get("branch_resume_snapshots", {})).items()
        }
        payload["shell_state_snapshot"] = cls._containerize_shell_state_snapshot_paths(
            payload.get("shell_state_snapshot"),
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        )
        payload["working_state"] = cls._containerize_working_memory_snapshot_paths(
            payload.get("working_state", {}),
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        )
        return (CheckpointEnvelope).model_validate(payload)
