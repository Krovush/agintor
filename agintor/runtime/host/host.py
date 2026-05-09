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
    runtime_visible_benchmark_task,
)
from ...contracts.verifiers import rescore_private_run_results, rescore_private_solve_response
from ...utils import ensure_directory, stable_hash
from ...core.versioning import RUNTIME_CONTRACT_VERSION

from .backend_selection import BackendSelectionMixin
from .finalization import FinalizationMixin
from .local_process import LocalProcessMixin
from .preflight import PreflightMixin
from .resume_resolution import ResumeResolutionMixin
from .validation import (
    ValidationMixin,
    _HostPostLaunchValidationError,
)

class RuntimeHost(FinalizationMixin, LocalProcessMixin, ValidationMixin, ResumeResolutionMixin, PreflightMixin, BackendSelectionMixin):
    def __init__(
        self,
        workspace: str | Path,
        *,
        runtime_backend: str = "local",
        artifact_mode: str | ArtifactMode | None = None,
        sandbox_root: str | Path | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.runtime_backend = self._normalize_backend(runtime_backend)
        self.artifact_policy = ArtifactPolicy.resolve(
            artifact_mode=artifact_mode,
            sandbox_root=Path(sandbox_root) if sandbox_root is not None else None,
        )
        self.sandbox_root = self.artifact_policy.sandbox_root
        self.run_store = RunStore(self.workspace)
        self.container_executor: DockerRuntimeExecutor | None = None

    @staticmethod
    def _rescore_private_batch_runs(runs: Sequence[RunResult], invocations: Sequence[Any]) -> list[RunResult]:
        authoritative_tasks = [
            getattr(invocation, "authoritative_task", None) or invocation.task
            for invocation in invocations
        ]
        return rescore_private_run_results(runs, authoritative_tasks)

    def inspect(self, runtime_dir: str | Path, requested_backend: str | None = None) -> CapabilityExchange:
        selected_backend = self._normalize_backend(requested_backend, fallback=self.runtime_backend)
        request = inspect_request_for_runtime(
            request_id=f"inspect.{stable_hash(runtime_dir, selected_backend)[:12]}",
            requested_backend=selected_backend,
            runtime_contract_version=RUNTIME_CONTRACT_VERSION,
        )
        if selected_backend == "docker":
            return self._docker_executor().inspect(runtime_dir, request)
        return self._run_local_inspect(Path(runtime_dir), request, runtime_backend=selected_backend)

    def run_batch(
        self,
        runtime_dir: str | Path,
        task_runs: list[tuple[object, int]],
        *,
        provider: ModelProvider,
        runtime_profile: object | None = None,
        budget_overrides: Mapping[str, Any] | None = None,
        trace_context: OpenAITraceContext | None = None,
    ) -> RuntimeBatchResponse:
        request = runtime_batch_request_for_tasks(
            request_id=f"run.{stable_hash(runtime_dir, len(task_runs), self.runtime_backend)[:12]}",
            runtime_backend=self.runtime_backend,
            task_runs=task_runs,
            budget_overrides=dict(budget_overrides or {}),
            trace_context=trace_context,
        )
        selected_backend, request = self._normalized_batch_request(request)
        capability_exchange = self.inspect(runtime_dir, requested_backend=selected_backend)
        self._preflight_batch_contracts(
            runtime_dir,
            capability_exchange,
            request,
            provider=provider,
            runtime_profile=runtime_profile,
        )
        grouped_invocations: dict[str, list[Any]] = {}
        for invocation in request.invocations:
            grouped_invocations.setdefault(batch_evaluation_unit_key(invocation), []).append(invocation)

        grouped_runs: dict[str, tuple[RunManifest, AttemptManifest, list[Any]]] = {}
        for evaluation_unit_id, invocations in grouped_invocations.items():
            first = invocations[0]
            grouped_episode = str(first.episode_kind or "") == "transfer_episode" and len(invocations) > 1
            manifest = self.run_store.create_run(
                request_id=evaluation_unit_id,
                evaluation_unit_id=evaluation_unit_id,
                request_mode="batch",
                runtime_backend=selected_backend,
                trace_context=(first.trace_context).model_dump() if first.trace_context is not None else None,
                task_id=None if grouped_episode else first.task.task_id,
                seed=first.seed,
                runtime_contract_version=capability_exchange.runtime_contract_version,
            )
            attempt = self.run_store.begin_attempt(manifest, launch_kind="run_batch")
            envelope = ExecutionUnitRequestEnvelope(
                request_kind="runtime_task_invocation_group" if grouped_episode else "runtime_task_invocation",
                request_mode="batch",
                request_id=evaluation_unit_id,
                evaluation_unit_id=evaluation_unit_id,
                payload=(first).model_dump(),
                member_invocations=[
                    (type(first)).model_validate((item).model_dump())
                    for item in invocations
                ]
                if grouped_episode
                else [],
            )
            self.run_store.write_request_bundle(
                manifest,
                request_envelope=(envelope).model_dump(),
                runtime_identity={
                    "runtime_contract_version": capability_exchange.runtime_contract_version,
                    "runtime_backend": selected_backend,
                },
            )
            grouped_runs[evaluation_unit_id] = (manifest, attempt, invocations)
            for invocation in invocations:
                invocation.run_id = manifest.run_id
                invocation.run_root = manifest.run_root
                invocation.attempt_id = attempt.attempt_id

        try:
            self._preflight_runtime_guarantees(runtime_dir, capability_exchange)
            if selected_backend == "docker":
                response = self._docker_executor().run_batch_protocol(
                    runtime_dir,
                    request,
                    provider=provider,
                    runtime_profile=runtime_profile,
                )
            else:
                response = self._run_local_batch(
                    Path(runtime_dir),
                    request,
                    provider=provider,
                    runtime_profile=runtime_profile,
                    runtime_backend=selected_backend,
                )
            self._validate_batch_response_contract(request, response, capability_exchange)
            self._validate_batch_response_run_ids(grouped_runs, response)
        except _HostPostLaunchValidationError as exc:
            for manifest, attempt, _ in grouped_runs.values():
                self._finalize_execution_unit(manifest, attempt, failure_kind=exc.failure_kind)
            raise RuntimeLoadError(str(exc)) from exc
        except Exception:
            for manifest, attempt, _ in grouped_runs.values():
                self._finalize_execution_unit(manifest, attempt, failure_kind="host_launch_failure")
            raise

        validation_errors: list[str] = []
        rescored_runs_by_key: dict[tuple[str, str], RunResult] = {}
        for evaluation_unit_id, (manifest, attempt, invocations) in grouped_runs.items():
            try:
                ordered_runs = self._validated_group_run_results(manifest, invocations, response)
            except _HostPostLaunchValidationError as exc:
                self._finalize_execution_unit(manifest, attempt, failure_kind=exc.failure_kind)
                validation_errors.append(str(exc))
                continue
            ordered_runs = self._rescore_private_batch_runs(ordered_runs, invocations)
            self._finalize_execution_unit(manifest, attempt, run_results=ordered_runs)
            for run in ordered_runs:
                rescored_runs_by_key[(str(run.run_id), str(run.request_id))] = run

        if validation_errors:
            raise RuntimeLoadError("; ".join(validation_errors))
        if rescored_runs_by_key:
            response = response.model_copy(
                update={
                    "run_results": [
                        rescored_runs_by_key.get((str(run.run_id), str(run.request_id)), run)
                        for run in response.run_results
                    ]
                }
            )
        return response

    def solve(
        self,
        runtime_dir: str | Path,
        request: RuntimeSolveRequest,
        *,
        provider: ModelProvider,
        runtime_profile: object | None = None,
    ) -> RuntimeSolveResponse:
        selected_backend = self._selected_solve_backend(request)
        authoritative_task = request.authoritative_task or request.task
        if request.task is not None:
            request = request.model_copy(update={"task": runtime_visible_benchmark_task(request.task)}, deep=True)
        capability_exchange = self.inspect(runtime_dir, requested_backend=selected_backend)
        evaluation_unit_id = str(request.evaluation_unit_id or request.request_id).strip() or request.request_id
        preflight_request = request.model_copy(
            update={
                "runtime_backend": selected_backend,
                "evaluation_unit_id": evaluation_unit_id,
            }
        )
        self._preflight_runtime_guarantees(runtime_dir, capability_exchange)
        self._preflight_solve_contract(
            runtime_dir,
            capability_exchange,
            preflight_request,
            provider=provider,
            runtime_profile=runtime_profile,
        )
        manifest = self.run_store.create_run(
            request_id=evaluation_unit_id,
            evaluation_unit_id=evaluation_unit_id,
            request_mode=request.mode,
            runtime_backend=selected_backend,
            trace_context=(request.trace_context).model_dump() if request.trace_context is not None else None,
            task_id=request.task.task_id if request.task is not None else None,
            seed=request.seed,
            runtime_contract_version=capability_exchange.runtime_contract_version,
        )
        attempt = self.run_store.begin_attempt(manifest, launch_kind="solve")
        request = preflight_request.model_copy(
            update={
                "run_id": manifest.run_id,
                "run_root": manifest.run_root,
                "attempt_id": attempt.attempt_id,
            }
        )
        self.run_store.write_request_bundle(
            manifest,
            request_envelope=(ExecutionUnitRequestEnvelope(
                    request_kind="runtime_solve_request",
                    request_mode=request.mode,
                    request_id=request.request_id,
                    evaluation_unit_id=evaluation_unit_id,
                    payload=(request).model_dump(),
                )).model_dump(),
            runtime_identity={
                "runtime_contract_version": capability_exchange.runtime_contract_version,
                "runtime_backend": selected_backend,
            },
        )
        try:
            if selected_backend == "docker":
                response = self._docker_executor().solve_protocol(
                    runtime_dir,
                    request,
                    provider=provider,
                    runtime_profile=runtime_profile,
                )
            else:
                response = self._run_local_solve(
                    Path(runtime_dir),
                    request,
                    provider=provider,
                    runtime_profile=runtime_profile,
                    runtime_backend=selected_backend,
                )
            self._validate_solve_response_contract(request, response, capability_exchange, action="solve")
            if authoritative_task is not None:
                response = rescore_private_solve_response(response, authoritative_task)
        except _HostPostLaunchValidationError as exc:
            self._finalize_execution_unit(manifest, attempt, failure_kind=exc.failure_kind)
            raise RuntimeLoadError(str(exc)) from exc
        except Exception:
            self._finalize_execution_unit(manifest, attempt, failure_kind="host_launch_failure")
            raise
        self._finalize_execution_unit(manifest, attempt, response=response)
        return response

    def resume(
        self,
        runtime_dir: str | Path,
        request: ResumeRequest,
        *,
        provider: ModelProvider,
        runtime_profile: object | None = None,
    ) -> RuntimeSolveResponse:
        runtime_request, original_request, manifest, attempt = self._resolve_runtime_resume_request(
            request,
            runtime_dir=runtime_dir,
        )
        selected_backend = self._selected_resume_backend(runtime_request, manifest)
        runtime_request = runtime_request.model_copy(update={"runtime_backend": selected_backend})
        original_request = original_request.model_copy(update={"runtime_backend": selected_backend})
        try:
            capability_exchange = self.inspect(runtime_dir, requested_backend=selected_backend)
            if not capability_exchange.resume_support:
                raise _HostPostLaunchValidationError(
                    "resume_not_supported",
                    f"runtime {runtime_dir} does not advertise resume support",
                )
            self._preflight_runtime_guarantees(runtime_dir, capability_exchange)
            self._preflight_solve_contract(
                runtime_dir,
                capability_exchange,
                original_request,
                provider=provider,
                runtime_profile=runtime_profile,
            )
            if selected_backend == "docker":
                response = self._docker_executor().resume_protocol(
                    runtime_dir,
                    runtime_request,
                    provider=provider,
                    runtime_profile=runtime_profile,
                )
            else:
                response = self._run_local_resume(
                    Path(runtime_dir),
                    runtime_request,
                    provider=provider,
                    runtime_profile=runtime_profile,
                    runtime_backend=selected_backend,
                )
            self._validate_solve_response_contract(runtime_request, response, capability_exchange, action="resume")
        except _HostPostLaunchValidationError as exc:
            self._finalize_execution_unit(manifest, attempt, failure_kind=exc.failure_kind)
            raise RuntimeLoadError(str(exc)) from exc
        except Exception:
            self._finalize_execution_unit(manifest, attempt, failure_kind="host_launch_failure")
            raise
        self._finalize_execution_unit(manifest, attempt, response=response)
        return response
