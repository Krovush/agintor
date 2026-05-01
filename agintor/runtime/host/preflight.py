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

class PreflightMixin:
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
