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

class _HostPostLaunchValidationError(Exception):
    def __init__(self, failure_kind: str, message: str) -> None:
        super().__init__(message)
        self.failure_kind = str(failure_kind)


class ValidationMixin:
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
