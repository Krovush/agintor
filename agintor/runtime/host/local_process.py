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

class LocalProcessMixin:
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
