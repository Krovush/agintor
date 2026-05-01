from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from ...storage.artifacts import ArtifactMode, ArtifactPolicy
from .backends.docker import DockerRuntimeExecutor
from ...core.exceptions import RuntimeLoadError
from ...providers import ModelProvider, provider_payload, provider_payload_file_paths, rewrite_provider_payload_file_paths
from ...storage.run_store import RunStore
from ..api import (
    batch_evaluation_unit_key,
    compile_execution_plan_from_solve_request,
    compile_execution_plan_from_task,
    execution_plan_requirements,
    inspect_request_for_runtime,
    reduce_grouped_run_results,
    resume_task_and_plan_from_checkpoint,
    solve_request_from_resume_checkpoint,
    runtime_trace_context,
    runtime_batch_request_for_tasks,
)
from ..sdk import KERNEL_BUNDLE_DIR
from ...contracts import (
    AttemptManifest,
    BenchmarkTask,
    CapabilityExchange,
    ExecutionUnitRequestEnvelope,
    OpenAITraceContext,
    ResumeRequest,
    RunManifest,
    RunResult,
    RuntimeBatchRequest,
    RuntimeBatchResponse,
    RuntimeResumeRequest,
    RuntimeSolveRequest,
    RuntimeSolveResponse,
)
from ...utils import ensure_directory, stable_hash
from ...core.versioning import RUNTIME_CONTRACT_VERSION

class ResumeResolutionMixin:
    def _resolve_runtime_resume_request(
        self,
        request: ResumeRequest,
        *,
        runtime_dir: str | Path | None = None,
    ) -> tuple[RuntimeResumeRequest, RuntimeSolveRequest, RunManifest, AttemptManifest]:
        try:
            target = self.run_store.resolve_resume_target(run_ref=request.run_ref, checkpoint_ref=request.checkpoint_ref)
        except FileNotFoundError as exc:
            raise RuntimeLoadError(str(exc)) from exc
        bundle = self.run_store.load_request_bundle(target.run_manifest)
        checkpoint_envelope = self.run_store.load_checkpoint_envelope(target.checkpoint_path)
        request_envelope = self._request_envelope_from_bundle(bundle)
        try:
            solve_request, rebound_envelope, _ = solve_request_from_resume_checkpoint(
                checkpoint_envelope,
                request_id_override=request.request_id or checkpoint_envelope.request_id,
                request_bundle=request_envelope,
                source_checkpoint_ref=str(target.checkpoint_path.resolve()),
            )
        except ValueError as exc:
            raise RuntimeLoadError(str(exc)) from exc
        task, plan = resume_task_and_plan_from_checkpoint(rebound_envelope)
        mode = "benchmark" if plan.origin.origin_kind == "benchmark" else "user_request"
        evaluation_unit_id = str(target.run_manifest.evaluation_unit_id or rebound_envelope.request_id or solve_request.request_id).strip() or solve_request.request_id
        resume_runtime_dir = (
            str(Path(runtime_dir).resolve())
            if runtime_dir is not None
            else getattr(plan.trace_context, "runtime_dir", None)
        )
        effective_trace_context = runtime_trace_context(
            request.trace_context or plan.trace_context,
            request_id=solve_request.request_id,
            runtime_hash=target.run_manifest.runtime_hash or getattr(plan.trace_context, "runtime_hash", None),
            runtime_dir=resume_runtime_dir,
            task_id=task.task_id,
            seed=int(rebound_envelope.seed),
            evaluation_unit_id=evaluation_unit_id,
            episode_kind=getattr(plan.trace_context, "episode_kind", None),
            episode_step_index=getattr(plan.trace_context, "episode_step_index", None),
            objective=solve_request.prompt,
        )
        preflight_request = RuntimeSolveRequest(
            request_id=solve_request.request_id,
            evaluation_unit_id=evaluation_unit_id,
            runtime_backend=target.run_manifest.runtime_backend or self.runtime_backend,
            seed=int(rebound_envelope.seed),
            mode=mode,
            task=task if mode == "benchmark" else None,
            solve_request=solve_request if mode == "user_request" else None,
            budget_overrides=dict(plan.budget_overrides),
            trace_context=effective_trace_context,
        )
        attempt = self.run_store.begin_attempt(
            target.run_manifest,
            launch_kind="resume",
            resumed_from_checkpoint_ref=str(target.checkpoint_path),
        )
        runtime_request = RuntimeResumeRequest(
            request_id=request.request_id or preflight_request.request_id or checkpoint_envelope.request_id,
            evaluation_unit_id=evaluation_unit_id,
            run_ref=target.run_manifest.run_id,
            checkpoint_ref=str(target.checkpoint_path),
            run_id=target.run_manifest.run_id,
            run_root=target.run_manifest.run_root,
            attempt_id=attempt.attempt_id,
            runtime_backend=preflight_request.runtime_backend,
            checkpoint_store_dir=str(target.checkpoint_store_dir),
            trace_context=effective_trace_context,
            reconciliation_policy=request.reconciliation_policy,
        )
        return runtime_request, preflight_request, target.run_manifest, attempt
