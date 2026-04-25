from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from .artifacts import ArtifactMode, ArtifactPolicy
from .container_runtime import DockerRuntimeExecutor
from .exceptions import RuntimeLoadError
from .providers import ModelProvider, provider_payload, provider_payload_file_paths, rewrite_provider_payload_file_paths
from .run_store import RunStore
from .runtime_api import (
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
from .runtime_sdk import KERNEL_BUNDLE_DIR
from .schemas import (
    AttemptManifest,
    BenchmarkTask,
    CapabilityExchange,
    ExecutionUnitRequestEnvelope,
    ResumeRequest,
    RunManifest,
    RunResult,
    RuntimeBatchRequest,
    RuntimeBatchResponse,
    RuntimeResumeRequest,
    RuntimeSolveRequest,
    RuntimeSolveResponse,
)
from .utils import ensure_directory, stable_hash
from .versioning import RUNTIME_CONTRACT_VERSION


class _HostPostLaunchValidationError(Exception):
    def __init__(self, failure_kind: str, message: str) -> None:
        super().__init__(message)
        self.failure_kind = str(failure_kind)


class RuntimeHost:
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
    def _normalize_backend(value: str | None, *, fallback: str | None = None) -> str:
        backend = str(value or "").strip().lower()
        if not backend:
            backend = str(fallback or "").strip().lower() or "local"
        if backend not in {"local", "docker"}:
            raise RuntimeLoadError(f"unsupported runtime backend {backend!r}")
        return backend

    def _selected_solve_backend(self, request: RuntimeSolveRequest) -> str:
        return self._normalize_backend(request.runtime_backend, fallback=self.runtime_backend)

    def _selected_resume_backend(
        self,
        runtime_request: RuntimeResumeRequest,
        manifest: RunManifest,
    ) -> str:
        return self._normalize_backend(
            runtime_request.runtime_backend,
            fallback=manifest.runtime_backend or self.runtime_backend,
        )

    def _selected_batch_backend(self, request: RuntimeBatchRequest) -> str:
        explicit_backends: list[str] = []
        request_backend = str(request.runtime_backend or "").strip()
        if request_backend:
            explicit_backends.append(self._normalize_backend(request_backend, fallback=self.runtime_backend))
        explicit_backends.extend(
            self._normalize_backend(invocation.runtime_backend, fallback=self.runtime_backend)
            for invocation in request.invocations
            if str(invocation.runtime_backend or "").strip()
        )
        selected_backend = explicit_backends[0] if explicit_backends else self.runtime_backend
        if any(backend != selected_backend for backend in explicit_backends[1:]):
            raise RuntimeLoadError("mixed runtime backends are not supported within one batch request")
        return selected_backend

    def _normalized_batch_request(
        self,
        request: RuntimeBatchRequest,
    ) -> tuple[str, RuntimeBatchRequest]:
        selected_backend = self._selected_batch_backend(request)
        return selected_backend, request.model_copy(
            update={
                "runtime_backend": selected_backend,
                "invocations": [
                    invocation.model_copy(update={"runtime_backend": selected_backend})
                    for invocation in request.invocations
                ],
            }
        )

    def _docker_executor(self) -> DockerRuntimeExecutor:
        if self.container_executor is None:
            self.container_executor = DockerRuntimeExecutor(
                self.workspace / ".runtime_host",
                artifact_mode=self.artifact_policy.mode,
                sandbox_root=self.artifact_policy.sandbox_root,
            )
        return self.container_executor

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
    ) -> RuntimeBatchResponse:
        request = runtime_batch_request_for_tasks(
            request_id=f"run.{stable_hash(runtime_dir, len(task_runs), self.runtime_backend)[:12]}",
            runtime_backend=self.runtime_backend,
            task_runs=task_runs,
            budget_overrides=dict(budget_overrides or {}),
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
        for evaluation_unit_id, (manifest, attempt, invocations) in grouped_runs.items():
            try:
                ordered_runs = self._validated_group_run_results(manifest, invocations, response)
            except _HostPostLaunchValidationError as exc:
                self._finalize_execution_unit(manifest, attempt, failure_kind=exc.failure_kind)
                validation_errors.append(str(exc))
                continue
            self._finalize_execution_unit(manifest, attempt, run_results=ordered_runs)

        if validation_errors:
            raise RuntimeLoadError("; ".join(validation_errors))
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
        runtime_request, original_request, manifest, attempt = self._resolve_runtime_resume_request(request)
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

    @staticmethod
    def _preflight_runtime_guarantees(runtime_dir: str | Path, capability_exchange: CapabilityExchange) -> None:
        policy = capability_exchange.runtime_isolation_policy
        if policy is None:
            return
        effective = {str(guarantee) for guarantee in capability_exchange.effective_guarantees}
        missing = [guarantee for guarantee in policy.required_guarantees if guarantee not in effective]
        if missing:
            raise RuntimeLoadError(
                f"runtime backend cannot satisfy required isolation guarantees for {runtime_dir}: {', '.join(sorted(missing))}"
            )

    @staticmethod
    def _service_network_allowed(policy: str | None) -> bool:
        normalized = str(policy or "").strip().lower()
        return normalized not in {"", "none", "restricted", "provider-only"}

    @staticmethod
    def _filesystem_write_allowed(policy: str | None) -> bool:
        normalized = str(policy or "").strip().lower()
        return "read-only" not in normalized and normalized not in {"readonly", "read_only", "none"}

    @staticmethod
    def _compiled_execution_plan_for_request(runtime_dir: str | Path, request: RuntimeSolveRequest):
        try:
            if request.mode == "benchmark":
                if request.task is None:
                    raise RuntimeLoadError("benchmark solve requests require a benchmark task payload")
                return compile_execution_plan_from_task(
                    (BenchmarkTask).model_validate((request.task).model_dump()),
                    request_id=request.request_id,
                    seed=request.seed,
                    runtime_hash="",
                    runtime_dir=str(runtime_dir),
                    trace_context=request.trace_context,
                    budget_overrides=request.budget_overrides,
                )
            if request.solve_request is None:
                raise RuntimeLoadError("user_request solve requests require a solve_request payload")
            _, execution_plan = compile_execution_plan_from_solve_request(
                request.solve_request,
                seed=request.seed,
                runtime_hash="",
                runtime_dir=str(runtime_dir),
                trace_context=request.trace_context,
            )
            return execution_plan
        except RuntimeLoadError:
            raise
        except Exception as exc:
            raise RuntimeLoadError(f"failed to compile execution plan for {runtime_dir}: {exc}") from exc

    @classmethod
    def _preflight_execution_plan_contract(
        cls,
        runtime_dir: str | Path,
        capability_exchange: CapabilityExchange,
        execution_plan,
    ):
        requirements = execution_plan_requirements(execution_plan)
        isolation_policy = capability_exchange.runtime_isolation_policy
        effective_guarantees = {
            str(guarantee or "").strip().lower()
            for guarantee in capability_exchange.effective_guarantees
            if str(guarantee or "").strip()
        }
        if requirements.requires_network_access:
            network_policy = str(getattr(isolation_policy, "network_policy", "") or "")
            transport_keys = list(requirements.required_network_transports) or ["generic"]
            for transport in transport_keys:
                transport_node_ids = requirements.network_transport_nodes.get(transport, requirements.network_nodes)
                node_list = ", ".join(sorted(transport_node_ids))
                if "network_disablement" in effective_guarantees:
                    raise RuntimeLoadError(
                        "compiled execution plan for "
                        f"{runtime_dir} requires network/service transport {transport!r} at nodes [{node_list}], "
                        "but the selected runtime backend guarantees network disablement"
                    )
                if isolation_policy is not None and not cls._service_network_allowed(network_policy):
                    raise RuntimeLoadError(
                        "compiled execution plan for "
                        f"{runtime_dir} requires network/service transport {transport!r} at nodes [{node_list}], "
                        f"but runtime contract network policy {network_policy!r} forbids that surface"
                    )
        if requirements.requires_filesystem_write:
            node_list = ", ".join(sorted(requirements.filesystem_write_nodes))
            filesystem_policy = str(getattr(isolation_policy, "filesystem_policy", "") or "")
            if isolation_policy is not None and not cls._filesystem_write_allowed(filesystem_policy):
                raise RuntimeLoadError(
                    "compiled execution plan for "
                    f"{runtime_dir} requires writable filesystem access at nodes [{node_list}], "
                    f"but runtime contract filesystem policy {filesystem_policy!r} is read-only"
                )
        return requirements

    def _preflight_solve_contract(
        self,
        runtime_dir: str | Path,
        capability_exchange: CapabilityExchange,
        request: RuntimeSolveRequest,
        *,
        provider: ModelProvider,
        runtime_profile: object | None,
    ) -> None:
        execution_plan = self._compiled_execution_plan_for_request(runtime_dir, request)
        requirements = self._preflight_execution_plan_contract(runtime_dir, capability_exchange, execution_plan)
        if not self._provider_matches_runtime_profile(provider, runtime_profile):
            return
        if not requirements.requires_default_provider:
            return
        missing = [
            name
            for name in capability_exchange.required_env_names
            if str(name).strip() and not self._runtime_requirement_available(provider, str(name))
        ]
        missing_any_of = []
        for group in capability_exchange.required_env_any_of:
            candidates = [str(name).strip() for name in group if str(name).strip()]
            if candidates and not any(self._runtime_requirement_available(provider, name) for name in candidates):
                missing_any_of.append(candidates)
        if not missing and not missing_any_of:
            return
        parts = [", ".join(sorted(missing))] if missing else []
        parts.extend(f"one of {', '.join(sorted(group))}" for group in missing_any_of)
        raise RuntimeLoadError(
            f"missing required runtime environment variables for {runtime_dir}: {'; '.join(parts)}"
        )

    def _preflight_batch_contracts(
        self,
        runtime_dir: str | Path,
        capability_exchange: CapabilityExchange,
        request: RuntimeBatchRequest,
        *,
        provider: ModelProvider,
        runtime_profile: object | None,
    ) -> None:
        for invocation in request.invocations:
            preflight_request = RuntimeSolveRequest(
                request_id=invocation.request_id,
                evaluation_unit_id=invocation.evaluation_unit_id or invocation.request_id,
                runtime_backend=request.runtime_backend,
                seed=invocation.seed,
                mode="benchmark",
                task=(BenchmarkTask).model_validate((invocation.task).model_dump()),
                budget_overrides=dict(request.budget_overrides),
                trace_context=invocation.trace_context,
            )
            self._preflight_solve_contract(
                runtime_dir,
                capability_exchange,
                preflight_request,
                provider=provider,
                runtime_profile=runtime_profile,
            )

    def _resolve_runtime_resume_request(
        self,
        request: ResumeRequest,
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
        effective_trace_context = runtime_trace_context(
            request.trace_context or plan.trace_context,
            request_id=solve_request.request_id,
            runtime_hash=target.run_manifest.runtime_hash or getattr(plan.trace_context, "runtime_hash", None),
            runtime_dir=getattr(plan.trace_context, "runtime_dir", None),
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

    @staticmethod
    def _validate_solve_response_contract(
        request: RuntimeSolveRequest | RuntimeResumeRequest,
        response: RuntimeSolveResponse,
        capability_exchange: CapabilityExchange,
        *,
        action: str,
    ) -> None:
        if response.capability_exchange != capability_exchange:
            raise _HostPostLaunchValidationError(
                "capability_drift",
                f"runtime capability exchange changed between inspect and {action}",
            )
        expected_request_id = str(request.request_id or "").strip()
        response_request_id = str(response.request_id or "").strip()
        solve_result_request_id = str(response.solve_result.request_id or "").strip()
        if expected_request_id and response_request_id != expected_request_id:
            raise _HostPostLaunchValidationError(
                "protocol_mismatch",
                f"runtime {action} response request_id mismatch: expected {expected_request_id!r}, found {response_request_id!r}",
            )
        if expected_request_id and solve_result_request_id != expected_request_id:
            raise _HostPostLaunchValidationError(
                "protocol_mismatch",
                f"runtime {action} solve_result request_id mismatch: expected {expected_request_id!r}, found {solve_result_request_id!r}",
            )

    @staticmethod
    def _validate_batch_response_contract(
        request: RuntimeBatchRequest,
        response: RuntimeBatchResponse,
        capability_exchange: CapabilityExchange,
    ) -> None:
        if response.capability_exchange != capability_exchange:
            raise _HostPostLaunchValidationError(
                "capability_drift",
                "runtime capability exchange changed between inspect and execution",
            )
        expected_request_id = str(request.request_id or "").strip()
        response_request_id = str(response.request_id or "").strip()
        if expected_request_id and response_request_id != expected_request_id:
            raise _HostPostLaunchValidationError(
                "protocol_mismatch",
                f"runtime batch response request_id mismatch: expected {expected_request_id!r}, found {response_request_id!r}",
            )

    @staticmethod
    def _validate_batch_response_run_ids(
        grouped_runs: Mapping[str, tuple[RunManifest, AttemptManifest, list[Any]]],
        response: RuntimeBatchResponse,
    ) -> None:
        expected_run_ids = {
            manifest.run_id
            for manifest, _, _ in grouped_runs.values()
        }
        unexpected_run_ids = sorted(
            {
                str(run.run_id or "").strip() or "<empty>"
                for run in response.run_results
                if str(run.run_id or "").strip() not in expected_run_ids
            }
        )
        if unexpected_run_ids:
            raise _HostPostLaunchValidationError(
                "protocol_mismatch",
                f"runtime batch response returned unexpected run_id values: {', '.join(unexpected_run_ids)}",
            )

    def _validated_group_run_results(
        self,
        manifest: RunManifest,
        invocations: Sequence[Any],
        response: RuntimeBatchResponse,
    ) -> list[RunResult]:
        expected_request_ids = self._group_run_request_order(invocations)
        matching_runs = [run for run in response.run_results if run.run_id == manifest.run_id]
        if not matching_runs:
            raise _HostPostLaunchValidationError(
                "missing_batch_result",
                f"runtime batch response omitted run results for {manifest.run_id}",
            )
        runs_by_request_id: dict[str, RunResult] = {}
        unexpected_request_ids: list[str] = []
        for run in matching_runs:
            run_request_id = str(run.request_id or "").strip()
            if run_request_id not in expected_request_ids:
                unexpected_request_ids.append(run_request_id or "<empty>")
                continue
            if run_request_id in runs_by_request_id:
                raise _HostPostLaunchValidationError(
                    "protocol_mismatch",
                    f"runtime batch response duplicated request_id {run_request_id!r} for {manifest.run_id}",
                )
            runs_by_request_id[run_request_id] = run
        if unexpected_request_ids:
            raise _HostPostLaunchValidationError(
                "protocol_mismatch",
                f"runtime batch response returned unexpected request_id values for {manifest.run_id}: {', '.join(unexpected_request_ids)}",
            )
        missing_request_ids = [request_id for request_id in expected_request_ids if request_id not in runs_by_request_id]
        if missing_request_ids:
            raise _HostPostLaunchValidationError(
                "missing_batch_result",
                f"runtime batch response omitted request_id values for {manifest.run_id}: {', '.join(missing_request_ids)}",
            )
        return [runs_by_request_id[request_id] for request_id in expected_request_ids]

    def _run_local_inspect(self, runtime_dir: Path, request, *, runtime_backend: str) -> CapabilityExchange:
        run_dir = ensure_directory(self.workspace / f"inspect_{stable_hash(runtime_dir, (request).model_dump())[:12]}")
        input_json = run_dir / "inspect_request.json"
        output_json = run_dir / "inspect_response.json"
        input_json.write_text(json.dumps((request).model_dump(), indent=2, sort_keys=True), encoding="utf-8")
        command = self._runtime_command(runtime_dir, "inspect", input_json=input_json, output_json=output_json)
        completed = subprocess.run(
            command,
            env=self._runtime_env(runtime_dir, runtime_backend),
            cwd=str(run_dir.resolve()),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeLoadError(completed.stderr.strip() or completed.stdout.strip() or "runtime inspect failed")
        capability = (CapabilityExchange).model_validate(json.loads(output_json.read_text(encoding="utf-8")))
        self._cleanup_run_dir(run_dir, failed=False)
        return capability

    def _run_local_batch(
        self,
        runtime_dir: Path,
        request,
        *,
        provider: ModelProvider,
        runtime_profile: object | None,
        runtime_backend: str,
    ) -> RuntimeBatchResponse:
        run_dir = ensure_directory(self.workspace / f"batch_{stable_hash(runtime_dir, (request).model_dump())[:12]}")
        input_json = run_dir / "batch_request.json"
        output_json = run_dir / "batch_response.json"
        provider_json = run_dir / "provider.json"
        profile_json = run_dir / "runtime_profile.json"
        workspace_dir = ensure_directory(run_dir / "workspace")
        input_json.write_text(json.dumps((request).model_dump(), indent=2, sort_keys=True), encoding="utf-8")
        provider_json.write_text(json.dumps(self._local_provider_payload(provider), indent=2, sort_keys=True), encoding="utf-8")
        command = self._runtime_command(
            runtime_dir,
            "run-batch",
            input_json=input_json,
            output_json=output_json,
            provider_json=provider_json,
            workspace=workspace_dir,
            profile_json=profile_json if runtime_profile is not None else None,
        )
        if runtime_profile is not None:
            profile_json.write_text(json.dumps((runtime_profile).model_dump(), indent=2, sort_keys=True), encoding="utf-8")
        completed = subprocess.run(
            command,
            env=self._runtime_env(runtime_dir, runtime_backend),
            cwd=str(run_dir.resolve()),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeLoadError(completed.stderr.strip() or completed.stdout.strip() or "runtime batch failed")
        response = (RuntimeBatchResponse).model_validate(json.loads(output_json.read_text(encoding="utf-8")))
        failed = any(run.hard_invalid for run in response.run_results)
        self._cleanup_run_dir(run_dir, failed=failed)
        return response

    def _run_local_solve(
        self,
        runtime_dir: Path,
        request: RuntimeSolveRequest,
        *,
        provider: ModelProvider,
        runtime_profile: object | None,
        runtime_backend: str,
    ) -> RuntimeSolveResponse:
        run_dir = ensure_directory(self.workspace / f"solve_{stable_hash(runtime_dir, (request).model_dump())[:12]}")
        input_json = run_dir / "solve_request.json"
        output_json = run_dir / "solve_response.json"
        provider_json = run_dir / "provider.json"
        profile_json = run_dir / "runtime_profile.json"
        workspace_dir = ensure_directory(run_dir / "workspace")
        input_json.write_text(json.dumps((request).model_dump(), indent=2, sort_keys=True), encoding="utf-8")
        provider_json.write_text(json.dumps(self._local_provider_payload(provider), indent=2, sort_keys=True), encoding="utf-8")
        command = self._runtime_command(
            runtime_dir,
            "solve",
            input_json=input_json,
            output_json=output_json,
            provider_json=provider_json,
            workspace=workspace_dir,
            profile_json=profile_json if runtime_profile is not None else None,
        )
        if runtime_profile is not None:
            profile_json.write_text(json.dumps((runtime_profile).model_dump(), indent=2, sort_keys=True), encoding="utf-8")
        completed = subprocess.run(
            command,
            env=self._runtime_env(runtime_dir, runtime_backend),
            cwd=str(run_dir.resolve()),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeLoadError(completed.stderr.strip() or completed.stdout.strip() or "runtime solve failed")
        response = (RuntimeSolveResponse).model_validate(json.loads(output_json.read_text(encoding="utf-8")))
        failed = response.solve_result.status in {"failed", "controlled_failure"} or bool(response.solve_result.faults.get("hard_invalid"))
        self._cleanup_run_dir(run_dir, failed=failed)
        return response

    def _run_local_resume(
        self,
        runtime_dir: Path,
        request: RuntimeResumeRequest,
        *,
        provider: ModelProvider,
        runtime_profile: object | None,
        runtime_backend: str,
    ) -> RuntimeSolveResponse:
        run_dir = ensure_directory(self.workspace / f"resume_{stable_hash(runtime_dir, (request).model_dump())[:12]}")
        input_json = run_dir / "resume_request.json"
        output_json = run_dir / "resume_response.json"
        provider_json = run_dir / "provider.json"
        profile_json = run_dir / "runtime_profile.json"
        workspace_dir = ensure_directory(run_dir / "workspace")
        input_json.write_text(json.dumps((request).model_dump(), indent=2, sort_keys=True), encoding="utf-8")
        provider_json.write_text(json.dumps(self._local_provider_payload(provider), indent=2, sort_keys=True), encoding="utf-8")
        command = self._runtime_command(
            runtime_dir,
            "resume",
            input_json=input_json,
            output_json=output_json,
            provider_json=provider_json,
            workspace=workspace_dir,
            profile_json=profile_json if runtime_profile is not None else None,
        )
        if runtime_profile is not None:
            profile_json.write_text(json.dumps((runtime_profile).model_dump(), indent=2, sort_keys=True), encoding="utf-8")
        completed = subprocess.run(
            command,
            env=self._runtime_env(runtime_dir, runtime_backend),
            cwd=str(run_dir.resolve()),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeLoadError(completed.stderr.strip() or completed.stdout.strip() or "runtime resume failed")
        response = (RuntimeSolveResponse).model_validate(json.loads(output_json.read_text(encoding="utf-8")))
        failed = response.solve_result.status in {"failed", "controlled_failure"} or bool(response.solve_result.faults.get("hard_invalid"))
        self._cleanup_run_dir(run_dir, failed=failed)
        return response

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
    def _provider_matches_runtime_profile(provider: ModelProvider, runtime_profile: object | None) -> bool:
        runtime_provider = getattr(runtime_profile, "runtime_provider", None)
        target = str(getattr(runtime_provider, "name", "") or "").strip().lower()
        if not target:
            return False
        visited: set[int] = set()

        def visit(instance: object) -> bool:
            ident = id(instance)
            if ident in visited:
                return False
            visited.add(ident)
            if str(getattr(instance, "provider_name", "") or "").strip().lower() == target:
                return True
            wrapped = getattr(instance, "wrapped", None)
            if wrapped is not None and visit(wrapped):
                return True
            providers = getattr(instance, "providers", None)
            if isinstance(providers, list):
                for child in providers:
                    if visit(child):
                        return True
            return False

        return visit(provider)

    @classmethod
    def _request_requires_default_provider(cls, runtime_dir: str | Path, request: RuntimeSolveRequest) -> bool:
        execution_plan = cls._compiled_execution_plan_for_request(runtime_dir, request)
        return execution_plan_requirements(execution_plan).requires_default_provider

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

    @classmethod
    def _runtime_requirement_available(cls, provider: ModelProvider, env_name: str) -> bool:
        name = str(env_name or "").strip()
        if not name:
            return False
        return bool(os.environ.get(name)) or cls._provider_supplies_requirement(provider, name)

    @staticmethod
    def _provider_supplies_requirement(provider: ModelProvider, env_name: str) -> bool:
        target = str(env_name or "").strip()
        if not target:
            return False
        visited: set[int] = set()

        def visit(instance: object) -> bool:
            ident = id(instance)
            if ident in visited:
                return False
            visited.add(ident)
            resolved_env_values = {
                str(getattr(instance, "api_key_env", "") or "").strip(): getattr(instance, "api_key", None),
                str(getattr(instance, "api_key_file_env", "") or "").strip(): getattr(instance, "api_key_file", None),
                str(getattr(instance, "base_url_env", "") or "").strip(): getattr(instance, "base_url", None),
                str(getattr(instance, "pricing_env", "") or "").strip(): getattr(instance, "pricing_map", None),
            }
            if target in resolved_env_values and resolved_env_values[target]:
                return True
            wrapped = getattr(instance, "wrapped", None)
            if wrapped is not None and visit(wrapped):
                return True
            providers = getattr(instance, "providers", None)
            if isinstance(providers, list):
                for child in providers:
                    if visit(child):
                        return True
            return False

        return visit(provider)

    @staticmethod
    def _local_provider_payload(provider: ModelProvider) -> dict[str, Any]:
        payload = provider_payload(provider)
        path_map = {
            path_text: str(Path(path_text).resolve())
            for path_text in provider_payload_file_paths(payload)
        }
        if not path_map:
            return payload
        return rewrite_provider_payload_file_paths(payload, path_map)

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

    def _runtime_command(
        self,
        runtime_dir: Path,
        command: str,
        *,
        input_json: Path,
        output_json: Path,
        provider_json: Path | None = None,
        profile_json: Path | None = None,
        workspace: Path | None = None,
    ) -> list[str]:
        argv = [
            sys.executable,
            "-m",
            "agintor_runtime.runtime_entry",
            command,
            "--runtime-dir",
            str(runtime_dir.resolve()),
            "--input-json",
            str(input_json.resolve()),
            "--output-json",
            str(output_json.resolve()),
        ]
        if provider_json is not None:
            argv.extend(["--provider-json", str(provider_json.resolve())])
        if profile_json is not None:
            argv.extend(["--profile-json", str(profile_json.resolve())])
        if workspace is not None:
            argv.extend(["--workspace", str(workspace.resolve())])
            argv.extend(["--artifact-mode", self.artifact_policy.mode.value])
        return argv

    def _runtime_env(self, runtime_dir: Path, runtime_backend: str) -> dict[str, str]:
        env = dict(os.environ)
        runtime_sdk = str((runtime_dir / KERNEL_BUNDLE_DIR).resolve())
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = runtime_sdk if not existing else runtime_sdk + os.pathsep + existing
        env["AGINTOR_RUNTIME_BACKEND"] = self._normalize_backend(
            runtime_backend,
            fallback=self.runtime_backend,
        )
        if self.sandbox_root is not None:
            env["AGINTOR_SANDBOX_CACHE_ROOT"] = str(self.sandbox_root)
        return env
