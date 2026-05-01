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

class DockerRunRewriteMixin:
    @classmethod
    def _copy_side_effect_receipt_payload(
        cls,
        receipt_payload: Mapping[str, Any] | SideEffectReceipt,
    ) -> dict[str, Any]:
        return (
            (receipt_payload).model_dump()
            if isinstance(receipt_payload, SideEffectReceipt)
            else dict(receipt_payload)
        )

    @classmethod
    def _copy_side_effect_ledger_payload(
        cls,
        ledger_payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        rewritten: dict[str, Any] = {}
        for key, value in dict(ledger_payload or {}).items():
            if isinstance(value, list):
                rewritten[str(key)] = [
                    cls._copy_side_effect_receipt_payload(item)
                    if isinstance(item, (Mapping, SideEffectReceipt))
                    else item
                    for item in value
                ]
            else:
                rewritten[str(key)] = value
        return rewritten

    @classmethod
    def _rewrite_run_manifest_paths(
        cls,
        manifest: RunManifest,
        *,
        run_mount_root: Path | None = None,
        checkpoint_store_dir: Path | None = None,
    ) -> RunManifest:
        payload = (manifest).model_dump()
        payload["run_root"] = cls._rewrite_known_path(
            manifest.run_root,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        ) or ""
        payload["latest_checkpoint_ref"] = cls._rewrite_known_path(
            manifest.latest_checkpoint_ref,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        )
        return (RunManifest).model_validate(payload)

    @classmethod
    def _rewrite_attempt_manifest_paths(
        cls,
        manifest: AttemptManifest,
        *,
        run_mount_root: Path | None = None,
        checkpoint_store_dir: Path | None = None,
    ) -> AttemptManifest:
        payload = (manifest).model_dump()
        payload["run_root"] = cls._rewrite_known_path(
            manifest.run_root,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        ) or ""
        payload["workspace_root"] = cls._rewrite_known_path(
            manifest.workspace_root,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        ) or manifest.workspace_root
        payload["latest_checkpoint_ref"] = cls._rewrite_known_path(
            manifest.latest_checkpoint_ref,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        )
        payload["resumed_from_checkpoint_ref"] = cls._rewrite_known_path(
            manifest.resumed_from_checkpoint_ref,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        )
        return (AttemptManifest).model_validate(payload)

    @classmethod
    def _rewrite_json_file_payload(
        cls,
        path: Path,
        *,
        run_mount_root: Path | None = None,
        checkpoint_store_dir: Path | None = None,
    ) -> Any | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"invalid JSON durable path payload at {path}") from exc
        if path.parent.name == "working_memory":
            rewritten = cls._rewrite_working_memory_snapshot_paths(
                payload if isinstance(payload, Mapping) else {},
                run_mount_root=run_mount_root,
                checkpoint_store_dir=checkpoint_store_dir,
            )
        elif path.parent.name == "recovery" or path.parent.name == "fingerprints":
            rewritten = cls._rewrite_recovery_payload_paths(
                payload,
                run_mount_root=run_mount_root,
                checkpoint_store_dir=checkpoint_store_dir,
            )
        else:
            return None
        if rewritten == payload:
            return None
        return rewritten

    @classmethod
    def _rewrite_durable_run_paths(
        cls,
        run_root: str | Path | None,
        *,
        runtime_path: Path | None = None,
        run_mount_root: Path | None = None,
        checkpoint_store_dir: Path | None = None,
        request_file_reverse_map: Mapping[str, str] | None = None,
    ) -> None:
        text = str(run_root or "").strip()
        if not text:
            return
        root = Path(text).resolve()
        candidate_paths = [root / "run_manifest.json"]
        candidate_paths.extend(sorted((root / "attempts").glob("*/attempt_manifest.json")))
        candidate_paths.extend(sorted((root / "checkpoints").glob("*.json")))
        prepared_writes: list[tuple[Path, Any]] = []
        rewritten_any = False
        for path in candidate_paths:
            if not path.exists() or not path.is_file():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if path.name == "run_manifest.json":
                    rewritten = (cls._rewrite_run_manifest_paths(
                            (RunManifest).model_validate(payload),
                            run_mount_root=run_mount_root,
                            checkpoint_store_dir=checkpoint_store_dir,
                        )).model_dump()
                elif path.name == "attempt_manifest.json":
                    rewritten = (cls._rewrite_attempt_manifest_paths(
                            (AttemptManifest).model_validate(payload),
                            run_mount_root=run_mount_root,
                            checkpoint_store_dir=checkpoint_store_dir,
                        )).model_dump()
                elif path.name == "LATEST.json":
                    rewritten = (cls._rewrite_checkpoint_reference_paths(
                            (CheckpointReference).model_validate(payload),
                            run_mount_root=run_mount_root,
                            checkpoint_store_dir=checkpoint_store_dir,
                        )).model_dump()
                elif path.name == "index.json":
                    if not isinstance(payload, list):
                        continue
                    rewritten = [
                        (cls._rewrite_checkpoint_reference_paths(
                                (CheckpointReference).model_validate(row),
                                run_mount_root=run_mount_root,
                                checkpoint_store_dir=checkpoint_store_dir,
                            )).model_dump()
                        for row in payload
                    ]
                else:
                    rewritten = (cls._rewrite_checkpoint_envelope_paths(
                            (CheckpointEnvelope).model_validate_persisted(payload),
                            run_mount_root=run_mount_root,
                            checkpoint_store_dir=checkpoint_store_dir,
                        )).model_dump()
            except Exception:
                raise RuntimeError(f"failed to rewrite durable run path payload at {path}") from None
            if rewritten == payload:
                continue
            prepared_writes.append((path, rewritten))
        payload_paths: list[Path] = []
        payload_paths.extend(sorted((root / "state" / "working_memory").glob("*.json")))
        payload_paths.extend(sorted((root / "state" / "recovery").rglob("*.json")))
        for path in payload_paths:
            if not path.exists() or not path.is_file():
                continue
            try:
                rewritten = cls._rewrite_json_file_payload(
                    path,
                    run_mount_root=run_mount_root,
                    checkpoint_store_dir=checkpoint_store_dir,
                )
            except Exception:
                raise RuntimeError(f"failed to rewrite durable run path payload at {path}") from None
            if rewritten is not None:
                prepared_writes.append((path, rewritten))
        for path, rewritten in prepared_writes:
            path.write_text(json.dumps(rewritten, indent=2, sort_keys=True), encoding="utf-8")
            rewritten_any = True
        if rewritten_any and (root / "state").exists():
            try:
                state_store.rebuild_from_canonical(root)
            except Exception:
                try:
                    state_store.mark_index_dirty(root, reason="docker_path_rewrite_reindex_failed")
                except Exception:
                    pass
