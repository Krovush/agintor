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

class FinalizationMixin:
    def _cleanup_run_dir(self, run_dir: Path, *, failed: bool) -> None:
        if self._should_retain_run_dir(failed=failed):
            return
        shutil.rmtree(run_dir, ignore_errors=True)
        parent = run_dir.parent
        while True:
            try:
                parent.rmdir()
            except OSError:
                break
            if parent == self.workspace:
                break
            parent = parent.parent

    def _should_retain_run_dir(self, *, failed: bool) -> bool:
        if failed and self.artifact_policy.keep_failures:
            return True
        if not failed and self.artifact_policy.keep_successes:
            return True
        return False

    @staticmethod
    def _request_envelope_from_bundle(bundle: Mapping[str, Any] | None) -> Mapping[str, Any]:
        if not isinstance(bundle, Mapping):
            return {}
        request_envelope = bundle.get("request")
        if isinstance(request_envelope, Mapping):
            return request_envelope
        return bundle

    @staticmethod
    def _group_run_request_order(invocations: Sequence[Any]) -> list[str]:
        rows = list(invocations)
        if rows and str(getattr(rows[0], "episode_kind", "") or "") == "transfer_episode":
            rows = sorted(
                rows,
                key=lambda invocation: (
                    int(invocation.episode_step_index or 0),
                    invocation.task.task_id,
                    invocation.request_id,
                ),
            )
        return [invocation.request_id for invocation in rows]

    @staticmethod
    def _is_inline_trace_ref(trace_ref: str | None) -> bool:
        return bool(trace_ref) and str(trace_ref).startswith("inline-json:")

    def _run_result_from_solve_result(self, manifest: RunManifest, solve_result) -> RunResult:
        return RunResult(
            request_id=str(solve_result.request_id or manifest.request_id),
            plan_id="",
            run_id=manifest.run_id,
            run_root=manifest.run_root,
            attempt_id="",
            runtime_hash=str(solve_result.runtime_hash or manifest.runtime_hash or ""),
            runtime_backend=manifest.runtime_backend,
            task_id=str(manifest.task_id or ""),
            seed=int(manifest.seed or 0),
            artifact=solve_result.artifact,
            runtime_evidence_manifest=dict(solve_result.runtime_evidence_manifest or {}),
            verifier_score=1.0 if bool(solve_result.verified) else 0.0,
            cost=float((solve_result.budget or {}).get("cost", 0.0) or 0.0),
            latency=float((solve_result.budget or {}).get("latency", 0.0) or 0.0),
            faults=int((solve_result.faults or {}).get("count", 0) or 0),
            hard_invalid=bool((solve_result.faults or {}).get("hard_invalid")),
            invalid_reason=(solve_result.faults or {}).get("invalid_reason"),
            failure_kind=str((solve_result.faults or {}).get("failure_kind") or (solve_result.faults or {}).get("code") or ""),
            checkpoint_ref=solve_result.latest_checkpoint_ref or solve_result.checkpoint_ref,
            latest_checkpoint_ref=solve_result.latest_checkpoint_ref or solve_result.checkpoint_ref,
            run_lifecycle_state=solve_result.run_lifecycle_state,
            run_resumable=bool(solve_result.run_resumable),
            run_prune_eligible=bool(solve_result.run_prune_eligible),
            mode=str(solve_result.mode or ""),
            lifecycle_state=str(solve_result.run_lifecycle_state or ""),
        )

    def _finalize_execution_unit(
        self,
        manifest: RunManifest,
        attempt: AttemptManifest,
        *,
        response: RuntimeSolveResponse | None = None,
        run_results: Sequence[RunResult] | None = None,
        failure_kind: str | None = None,
    ) -> RunManifest:
        effective_runs = list(run_results or [])
        if not effective_runs and response is not None:
            effective_runs = [self._run_result_from_solve_result(manifest, response.solve_result)]
        reduction = reduce_grouped_run_results(effective_runs) if effective_runs else {
            "lifecycle_state": "failed",
            "latest_checkpoint_ref": None,
            "failure_kind": failure_kind,
            "resumable": False,
            "prune_eligible": True,
        }
        reported_checkpoint_ref = str(reduction.get("latest_checkpoint_ref") or "").strip() or None
        if reported_checkpoint_ref and Path(reported_checkpoint_ref).exists() and not self.run_store.checkpoint_ref_is_resume_eligible(reported_checkpoint_ref):
            reported_checkpoint_ref = None
        durable_checkpoint_ref = self.run_store.latest_usable_checkpoint_ref(manifest.run_root)
        latest_checkpoint_ref = reported_checkpoint_ref or durable_checkpoint_ref
        effective_failure_kind = str(reduction.get("failure_kind") or failure_kind or "").strip() or None
        reduced_lifecycle_state = str(reduction.get("lifecycle_state") or "failed")
        if reduced_lifecycle_state == "completed":
            lifecycle_state = "completed"
        elif reduced_lifecycle_state == "cancelled":
            lifecycle_state = "cancelled"
        else:
            lifecycle_state = "paused" if latest_checkpoint_ref else "failed"
        attempt_state = (
            "completed"
            if lifecycle_state == "completed"
            else "cancelled"
            if lifecycle_state == "cancelled"
            else "paused"
            if latest_checkpoint_ref
            else "failed"
        )
        resolved_runtime_hash = (
            response.solve_result.runtime_hash
            if response is not None
            else next(
                (
                    str(getattr(run, "runtime_hash", "") or "").strip()
                    for run in effective_runs
                    if str(getattr(run, "runtime_hash", "") or "").strip()
                ),
                str(manifest.runtime_hash or ""),
            )
        )
        manifest = manifest.model_copy(update={"runtime_hash": resolved_runtime_hash})
        self.run_store.finish_attempt(
            attempt,
            lifecycle_state=attempt_state,
            latest_checkpoint_ref=latest_checkpoint_ref,
            failure_kind=effective_failure_kind,
        )
        updated = self.run_store.finish_run(
            manifest,
            lifecycle_state=lifecycle_state,
            latest_checkpoint_ref=latest_checkpoint_ref,
            resumable=bool(latest_checkpoint_ref),
            failure_kind=effective_failure_kind,
        )
        if updated.prune_eligible and not self.artifact_policy.keep_failures:
            updated = self.run_store.prune_run(updated)
        final_checkpoint_ref = latest_checkpoint_ref if updated.lifecycle_state != "pruned" else None
        if response is not None:
            response.solve_result.run_id = updated.run_id
            response.solve_result.run_root = updated.run_root
            response.solve_result.attempt_id = attempt.attempt_id
            response.solve_result.latest_checkpoint_ref = final_checkpoint_ref
            response.solve_result.checkpoint_ref = final_checkpoint_ref
            response.solve_result.run_lifecycle_state = updated.lifecycle_state
            response.solve_result.run_resumable = updated.resumable
            response.solve_result.run_prune_eligible = updated.prune_eligible
            if updated.lifecycle_state == "pruned":
                response.solve_result.trace_ref = None
        for run in effective_runs:
            run.run_id = updated.run_id
            run.run_root = updated.run_root
            run.attempt_id = attempt.attempt_id
            run.latest_checkpoint_ref = final_checkpoint_ref
            run.checkpoint_ref = final_checkpoint_ref
            run.run_lifecycle_state = updated.lifecycle_state
            run.run_resumable = updated.resumable
            run.run_prune_eligible = updated.prune_eligible
            run.failure_kind = effective_failure_kind
            if updated.lifecycle_state == "pruned":
                run.trace_path = None
                run.trace = []
        return updated
