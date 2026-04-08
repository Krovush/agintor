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
from .pydantic_compat import model_dump, model_validate
from .runtime_api import inspect_request_for_runtime, runtime_batch_request_for_tasks, solve_request_to_task
from .runtime_loader import RUNTIME_ABI_VERSION
from .runtime_sdk import KERNEL_BUNDLE_DIR, KERNEL_VERSION, STORAGE_SCHEMA_VERSION
from .schemas import BenchmarkTask, CapabilityExchange, RuntimeBatchResponse, RuntimeSolveRequest, RuntimeSolveResponse
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

    def solve(
        self,
        runtime_dir: str | Path,
        request: RuntimeSolveRequest,
        *,
        provider: ModelProvider,
        runtime_profile: object | None = None,
    ) -> RuntimeSolveResponse:
        capability_exchange = self.inspect(runtime_dir)
        self._preflight_solve_contract(
            runtime_dir,
            capability_exchange,
            request,
            provider=provider,
            runtime_profile=runtime_profile,
        )
        if self.runtime_backend == "docker" and self.container_executor is not None:
            response = self.container_executor.solve_protocol(
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
            )
        if response.capability_exchange != capability_exchange:
            raise RuntimeLoadError("runtime capability exchange changed between inspect and solve")
        failed = response.solve_result.status in {"failed", "controlled_failure"} or bool(response.solve_result.faults.get("hard_invalid"))
        self._prune_solve_result_artifacts(response, failed=failed)
        return response

    def _preflight_solve_contract(
        self,
        runtime_dir: str | Path,
        capability_exchange: CapabilityExchange,
        request: RuntimeSolveRequest,
        *,
        provider: ModelProvider,
        runtime_profile: object | None,
    ) -> None:
        if not self._provider_matches_runtime_profile(provider, runtime_profile):
            return
        if not self._request_requires_default_provider(request):
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

    def _run_local_solve(
        self,
        runtime_dir: Path,
        request: RuntimeSolveRequest,
        *,
        provider: ModelProvider,
        runtime_profile: object | None,
    ) -> RuntimeSolveResponse:
        run_dir = ensure_directory(self.workspace / f"solve_{stable_hash(runtime_dir, model_dump(request))[:12]}")
        input_json = run_dir / "solve_request.json"
        output_json = run_dir / "solve_response.json"
        provider_json = run_dir / "provider.json"
        profile_json = run_dir / "runtime_profile.json"
        workspace_dir = ensure_directory(run_dir / "workspace")
        input_json.write_text(json.dumps(model_dump(request), indent=2, sort_keys=True), encoding="utf-8")
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
            raise RuntimeLoadError(completed.stderr.strip() or completed.stdout.strip() or "runtime solve failed")
        response = model_validate(RuntimeSolveResponse, json.loads(output_json.read_text(encoding="utf-8")))
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

    @staticmethod
    def _task_requires_default_provider(task: BenchmarkTask) -> bool:
        return any(
            str(operation.kind or "").strip().lower() == "direct_response"
            or (
                str(operation.kind or "").strip().lower() == "generated_expression"
                and not str(operation.expression or "").strip()
            )
            for operation in task.operations
        )

    @classmethod
    def _request_requires_default_provider(cls, request: RuntimeSolveRequest) -> bool:
        if request.mode == "benchmark":
            return bool(request.task) and cls._task_requires_default_provider(request.task)
        if request.solve_request is None:
            return False
        if cls._task_requires_default_provider(solve_request_to_task(request.solve_request)):
            return True
        return cls._request_may_trigger_default_provider_side_paths(request)

    @staticmethod
    def _request_may_trigger_default_provider_side_paths(request: RuntimeSolveRequest) -> bool:
        solve_request = request.solve_request
        if solve_request is None:
            return False
        return bool(solve_request.context_items)

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

    @staticmethod
    def _recoverability_without_checkpoint(status: str) -> str:
        if status in {"verified", "unverified", "partially_checked", "best_effort"}:
            return "terminal"
        return "none"

    def _prune_solve_result_artifacts(self, response: RuntimeSolveResponse, *, failed: bool) -> None:
        if self._should_retain_run_dir(failed=failed):
            return
        if response.solve_result.trace_ref and not self._is_inline_trace_ref(response.solve_result.trace_ref):
            response.solve_result.trace_ref = None
        response.solve_result.checkpoint_ref = None
        response.solve_result.recoverability = self._recoverability_without_checkpoint(response.solve_result.status)

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
