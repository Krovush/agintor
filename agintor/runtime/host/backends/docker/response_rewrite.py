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

class DockerResponseRewriteMixin:
    @classmethod
    def _rewrite_checkpoint_reference_paths(
        cls,
        reference: CheckpointReference,
        *,
        run_mount_root: Path | None = None,
        checkpoint_store_dir: Path | None = None,
    ) -> CheckpointReference:
        payload = (reference).model_dump()
        payload["ref"] = cls._rewrite_known_path(
            reference.ref,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        ) or ""
        payload["run_root"] = cls._rewrite_known_path(
            reference.run_root,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        ) or ""
        return (CheckpointReference).model_validate(payload)

    @classmethod
    def _rewrite_checkpoint_envelope_paths(
        cls,
        envelope: CheckpointEnvelope,
        *,
        run_mount_root: Path | None = None,
        checkpoint_store_dir: Path | None = None,
    ) -> CheckpointEnvelope:
        payload = (envelope).model_dump()
        payload["run_root"] = cls._rewrite_known_path(
            envelope.run_root,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        ) or ""
        payload["source_checkpoint_ref"] = cls._rewrite_known_path(
            envelope.source_checkpoint_ref,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        )
        payload["selected_checkpoint_ref"] = cls._rewrite_known_path(
            envelope.selected_checkpoint_ref,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        )
        payload["runtime_state_snapshot"]["latest_checkpoint_ref"] = cls._rewrite_known_path(
            envelope.runtime_state_snapshot.latest_checkpoint_ref,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        )
        payload["attempt_snapshot"]["run_root"] = cls._rewrite_known_path(
            envelope.attempt_snapshot.run_root,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        ) or envelope.attempt_snapshot.run_root
        payload["attempt_snapshot"]["resumed_from_checkpoint_ref"] = cls._rewrite_known_path(
            envelope.attempt_snapshot.resumed_from_checkpoint_ref,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        ) or envelope.attempt_snapshot.resumed_from_checkpoint_ref
        payload["runtime_state_snapshot"]["branch_resume_snapshots"] = {
            str(key): cls._rewrite_branch_resume_snapshot_paths(
                dict(value),
                run_mount_root=run_mount_root,
                checkpoint_store_dir=checkpoint_store_dir,
            )
            for key, value in dict(payload["runtime_state_snapshot"].get("branch_resume_snapshots", {})).items()
        }
        payload["shell_state_snapshot"] = cls._rewrite_shell_state_snapshot_paths(
            payload.get("shell_state_snapshot"),
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        )
        # Runtime artifacts and receipt result payloads are replay state; keep
        # them in the coordinate space recorded by the runtime.
        payload["side_effect_ledger"] = cls._copy_side_effect_ledger_payload(
            payload.get("side_effect_ledger", {}),
        )
        payload["working_state"] = cls._rewrite_working_memory_snapshot_paths(
            payload.get("working_state", {}),
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        )
        return (CheckpointEnvelope).model_validate(payload)

    def _rewrite_response_paths(
        self,
        response: RuntimeBatchResponse,
        workspace_dir: Path,
        *,
        runtime_path: Path | None = None,
        run_mount_root: Path | None = None,
        checkpoint_store_dir: Path | None = None,
        request_file_reverse_map: Mapping[str, str] | None = None,
    ) -> None:
        path_replacements = self._mounted_path_replacements(
            runtime_path=runtime_path,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
            request_file_reverse_map=request_file_reverse_map,
        )
        for run in response.run_results:
            run.trace_path = self._host_workspace_path(run.trace_path, workspace_dir)
            run.checkpoint_ref = self._host_workspace_path(run.checkpoint_ref, workspace_dir)
            run.trace_path = self._host_mounted_path(run.trace_path, self.RUNS_MOUNT_ROOT, run_mount_root)
            run.checkpoint_ref = self._host_mounted_path(run.checkpoint_ref, self.RUNS_MOUNT_ROOT, run_mount_root)
            run.latest_checkpoint_ref = self._host_mounted_path(
                run.latest_checkpoint_ref,
                self.RUNS_MOUNT_ROOT,
                run_mount_root,
            )
            run.run_root = self._host_mounted_path(run.run_root, self.RUNS_MOUNT_ROOT, run_mount_root) or ""
            run.trace_context = self._rewrite_trace_context_paths(
                run.trace_context,
                path_replacements,
            )
            run.trace = self._rewrite_structured_path_payload(
                run.trace,
                path_replacements,
            )
            run.artifact = self._rewrite_structured_path_payload(
                run.artifact,
                path_replacements,
            )

    def _rewrite_solve_response_paths(
        self,
        response: RuntimeSolveResponse,
        workspace_dir: Path,
        checkpoint_store_dir: Path | None = None,
        *,
        runtime_path: Path | None = None,
        run_mount_root: Path | None = None,
        request_file_reverse_map: Mapping[str, str] | None = None,
    ) -> None:
        path_replacements = self._mounted_path_replacements(
            runtime_path=runtime_path,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
            request_file_reverse_map=request_file_reverse_map,
        )
        response.solve_result.trace_ref = self._host_workspace_path(response.solve_result.trace_ref, workspace_dir)
        response.solve_result.checkpoint_ref = self._host_workspace_path(response.solve_result.checkpoint_ref, workspace_dir)
        response.solve_result.trace_ref = self._host_mounted_path(
            response.solve_result.trace_ref,
            self.RUNS_MOUNT_ROOT,
            run_mount_root,
        )
        response.solve_result.trace_ref = self._rewrite_inline_trace_ref(
            response.solve_result.trace_ref,
            path_replacements,
        )
        response.solve_result.checkpoint_ref = self._host_mounted_path(
            response.solve_result.checkpoint_ref,
            self.RUNS_MOUNT_ROOT,
            run_mount_root,
        )
        response.solve_result.checkpoint_ref = self._host_mounted_path(
            response.solve_result.checkpoint_ref,
            "/mnt/checkpoints",
            checkpoint_store_dir,
        )
        response.solve_result.latest_checkpoint_ref = self._host_mounted_path(
            response.solve_result.latest_checkpoint_ref,
            self.RUNS_MOUNT_ROOT,
            run_mount_root,
        )
        response.solve_result.latest_checkpoint_ref = self._host_mounted_path(
            response.solve_result.latest_checkpoint_ref,
            "/mnt/checkpoints",
            checkpoint_store_dir,
        )
        response.solve_result.run_root = self._host_mounted_path(
            response.solve_result.run_root,
            self.RUNS_MOUNT_ROOT,
            run_mount_root,
        ) or ""
        response.solve_result.artifact = self._rewrite_structured_path_payload(
            response.solve_result.artifact,
            path_replacements,
        )
        response.solve_result.post_message_short_term_export = self._rewrite_structured_path_payload(
            response.solve_result.post_message_short_term_export,
            path_replacements,
        )
