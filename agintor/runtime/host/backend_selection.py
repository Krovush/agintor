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

class BackendSelectionMixin:
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
                run_store_workspace=self.workspace,
            )
        return self.container_executor
