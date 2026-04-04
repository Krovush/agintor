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
from .providers import ModelProvider, provider_payload
from .pydantic_compat import model_dump, model_validate
from .runtime_api import inspect_request_for_runtime, runtime_batch_request_for_tasks
from .runtime_loader import RUNTIME_ABI_VERSION
from .runtime_sdk import KERNEL_BUNDLE_DIR, KERNEL_VERSION, STORAGE_SCHEMA_VERSION
from .schemas import CapabilityExchange, RuntimeBatchResponse
from .utils import ensure_directory, stable_hash


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
        self.runtime_backend = str(runtime_backend or "local").strip().lower()
        self.artifact_policy = ArtifactPolicy.resolve(
            artifact_mode=artifact_mode,
            sandbox_root=Path(sandbox_root) if sandbox_root is not None else None,
        )
        self.sandbox_root = self.artifact_policy.sandbox_root
        self.container_executor = (
            DockerRuntimeExecutor(
                self.workspace / ".runtime_host",
                artifact_mode=self.artifact_policy.mode,
                sandbox_root=self.artifact_policy.sandbox_root,
            )
            if self.runtime_backend == "docker"
            else None
        )

    def inspect(self, runtime_dir: str | Path) -> CapabilityExchange:
        request = inspect_request_for_runtime(
            request_id=f"inspect.{stable_hash(runtime_dir, self.runtime_backend)[:12]}",
            requested_backend=self.runtime_backend,
            runtime_abi=RUNTIME_ABI_VERSION,
            kernel_version=KERNEL_VERSION,
            storage_schema_version=STORAGE_SCHEMA_VERSION,
        )
        if self.runtime_backend == "docker" and self.container_executor is not None:
            return self.container_executor.inspect(runtime_dir, request)
        return self._run_local_inspect(Path(runtime_dir), request)

    def run_batch(
        self,
        runtime_dir: str | Path,
        task_runs: list[tuple[object, int]],
        *,
        provider: ModelProvider,
        runtime_profile: object | None = None,
        budget_overrides: Mapping[str, Any] | None = None,
    ) -> RuntimeBatchResponse:
        capability_exchange = self.inspect(runtime_dir)
        request = runtime_batch_request_for_tasks(
            request_id=f"run.{stable_hash(runtime_dir, len(task_runs), self.runtime_backend)[:12]}",
            runtime_backend=self.runtime_backend,
            task_runs=task_runs,
            budget_overrides=dict(budget_overrides or {}),
        )
        if self.runtime_backend == "docker" and self.container_executor is not None:
            response = self.container_executor.run_batch_protocol(
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
            )
        if response.capability_exchange != capability_exchange:
            raise RuntimeLoadError("runtime capability exchange changed between inspect and execution")
        failed = any(run.hard_invalid for run in response.run_results)
        if not self._should_retain_run_dir(failed=failed):
            for run in response.run_results:
                run.trace_path = None
                run.checkpoint_ref = None
        return response

    def _run_local_inspect(self, runtime_dir: Path, request) -> CapabilityExchange:
        run_dir = ensure_directory(self.workspace / f"inspect_{stable_hash(runtime_dir, model_dump(request))[:12]}")
        input_json = run_dir / "inspect_request.json"
        output_json = run_dir / "inspect_response.json"
        input_json.write_text(json.dumps(model_dump(request), indent=2, sort_keys=True), encoding="utf-8")
        command = self._runtime_command(runtime_dir, "inspect", input_json=input_json, output_json=output_json)
        completed = subprocess.run(
            command,
            env=self._runtime_env(runtime_dir),
            cwd=str(run_dir.resolve()),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeLoadError(completed.stderr.strip() or completed.stdout.strip() or "runtime inspect failed")
        capability = model_validate(CapabilityExchange, json.loads(output_json.read_text(encoding="utf-8")))
        self._cleanup_run_dir(run_dir, failed=False)
        return capability

    def _run_local_batch(self, runtime_dir: Path, request, *, provider: ModelProvider, runtime_profile: object | None) -> RuntimeBatchResponse:
        run_dir = ensure_directory(self.workspace / f"batch_{stable_hash(runtime_dir, model_dump(request))[:12]}")
        input_json = run_dir / "batch_request.json"
        output_json = run_dir / "batch_response.json"
        provider_json = run_dir / "provider.json"
        profile_json = run_dir / "runtime_profile.json"
        workspace_dir = ensure_directory(run_dir / "workspace")
        input_json.write_text(json.dumps(model_dump(request), indent=2, sort_keys=True), encoding="utf-8")
        provider_json.write_text(json.dumps(provider_payload(provider), indent=2, sort_keys=True), encoding="utf-8")
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
            profile_json.write_text(json.dumps(model_dump(runtime_profile), indent=2, sort_keys=True), encoding="utf-8")
        completed = subprocess.run(
            command,
            env=self._runtime_env(runtime_dir),
            cwd=str(run_dir.resolve()),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeLoadError(completed.stderr.strip() or completed.stdout.strip() or "runtime batch failed")
        response = model_validate(RuntimeBatchResponse, json.loads(output_json.read_text(encoding="utf-8")))
        failed = any(run.hard_invalid for run in response.run_results)
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

    def _runtime_env(self, runtime_dir: Path) -> dict[str, str]:
        env = dict(os.environ)
        runtime_sdk = str((runtime_dir / KERNEL_BUNDLE_DIR).resolve())
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = runtime_sdk if not existing else runtime_sdk + os.pathsep + existing
        if self.sandbox_root is not None:
            env["AGINTOR_SANDBOX_CACHE_ROOT"] = str(self.sandbox_root)
        return env
