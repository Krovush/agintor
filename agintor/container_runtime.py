from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .artifacts import ArtifactMode, ArtifactPolicy
from .providers import (
    ModelProvider,
    provider_environment_names_for_instance,
    provider_payload,
    provider_payload_file_paths,
    rewrite_provider_payload_file_paths,
)
from .pydantic_compat import model_dump, model_validate
from .runtime_profile import RuntimeProfile
from .runtime_sdk import KERNEL_BUNDLE_DIR
from .schemas import (
    BenchmarkTask,
    CapabilityExchange,
    InspectRequest,
    RunResult,
    RuntimeBatchRequest,
    RuntimeBatchResponse,
    RuntimeTaskInvocation,
)
from .utils import ensure_directory, file_digest, stable_hash


class DockerRuntimeExecutor:
    def __init__(
        self,
        workspace: Path,
        repo_root: Path | None = None,
        image_name_prefix: str = "agintor-runtime",
        base_image: str = "python:3.11-slim",
        artifact_mode: str | ArtifactMode | None = ArtifactMode.ALWAYS,
        sandbox_root: Path | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.repo_root = repo_root or Path(__file__).resolve().parent.parent
        self.image_name_prefix = image_name_prefix
        self.base_image = base_image
        self.artifact_policy = ArtifactPolicy.resolve(
            artifact_mode=artifact_mode,
            sandbox_root=sandbox_root,
        )
        self.retain_artifacts = self.artifact_policy.keep_successes
        self.image_tag = f"{self.image_name_prefix}:{self._source_digest()[:12]}"

    def _source_digest(self) -> str:
        relevant = [self.repo_root / "pyproject.toml", self.repo_root / "README.md"]
        relevant.extend(sorted((self.repo_root / "agintor").rglob("*.py")))
        relevant.extend(sorted((self.repo_root / "agintor").rglob("*.json")))
        parts = [f"base_image::{self.base_image}"]
        for path in relevant:
            if path.exists():
                parts.append(f"{path.relative_to(self.repo_root)}::{file_digest(path)}")
        return stable_hash(*parts)

    def _dockerfile_text(self) -> str:
        return "\n".join(
            [
                f"FROM {self.base_image}",
                "WORKDIR /opt/agintor",
                "COPY pyproject.toml README.md /opt/agintor/",
                "COPY agintor /opt/agintor/agintor",
                "RUN pip install --no-cache-dir '.[hosted]'",
            ]
        )

    def ensure_image(self) -> None:
        inspect = subprocess.run(
            ["docker", "image", "inspect", self.image_tag],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if inspect.returncode == 0:
            return
        dockerfile_dir = ensure_directory(self.workspace / "docker")
        dockerfile_path = dockerfile_dir / f"Dockerfile.{self._source_digest()[:12]}"
        if not dockerfile_path.exists():
            dockerfile_path.write_text(self._dockerfile_text(), encoding="utf-8")
        completed = subprocess.run(
            ["docker", "build", "-f", str(dockerfile_path), "-t", self.image_tag, str(self.repo_root)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "docker build failed")

    def run_task(
        self,
        runtime_dir: str | Path,
        task: BenchmarkTask,
        seed: int,
        *,
        provider: ModelProvider,
        runtime_profile: RuntimeProfile | None = None,
    ) -> RunResult:
        return self.run_unit(
            runtime_dir,
            [task],
            seed,
            provider=provider,
            runtime_profile=runtime_profile,
        )[0]

    def run_unit(
        self,
        runtime_dir: str | Path,
        tasks: list[BenchmarkTask],
        seed: int,
        *,
        provider: ModelProvider,
        runtime_profile: RuntimeProfile | None = None,
    ) -> list[RunResult]:
        task_runs = [(task, seed) for task in tasks]
        return self.run_batch(
            runtime_dir,
            task_runs,
            provider=provider,
            runtime_profile=runtime_profile,
        )

    def run_batch(
        self,
        runtime_dir: str | Path,
        task_runs: list[tuple[BenchmarkTask, int]],
        *,
        provider: ModelProvider,
        runtime_profile: RuntimeProfile | None = None,
    ) -> list[RunResult]:
        response = self.run_batch_protocol(
            runtime_dir,
            RuntimeBatchRequest(
                request_id=f"docker.{stable_hash(runtime_dir, task_runs)[:12]}",
                runtime_backend="docker",
                invocations=[RuntimeTaskInvocation(seed=int(seed), task=task) for task, seed in task_runs],
            ),
            provider=provider,
            runtime_profile=runtime_profile,
        )
        return response.run_results

    def inspect(self, runtime_dir: str | Path, request: InspectRequest) -> CapabilityExchange:
        self.ensure_image()
        runtime_path = Path(runtime_dir).resolve()
        run_dir = ensure_directory(self.workspace / f"inspect_{stable_hash(runtime_path, model_dump(request), self.base_image)[:12]}")
        input_json = run_dir / "inspect_request.json"
        output_json = run_dir / "inspect_response.json"
        workspace_dir = ensure_directory(run_dir / "workspace")
        input_json.write_text(json.dumps(model_dump(request), indent=2, sort_keys=True), encoding="utf-8")
        command = [
            "docker",
            "run",
            "--rm",
            "-e",
            f"PYTHONPATH=/mnt/runtime/{KERNEL_BUNDLE_DIR}",
            "-v",
            f"{runtime_path}:/mnt/runtime:ro",
            "-v",
            f"{input_json.resolve()}:/mnt/input.json:ro",
            "-v",
            f"{output_json.parent.resolve()}:/mnt/output",
            "-v",
            f"{workspace_dir.resolve()}:/mnt/workspace",
            self.image_tag,
            "python",
            "-m",
            "agintor_runtime.runtime_entry",
            "inspect",
            "--runtime-dir",
            "/mnt/runtime",
            "--input-json",
            "/mnt/input.json",
            "--output-json",
            "/mnt/output/inspect_response.json",
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "docker runtime inspect failed")
        capability = model_validate(CapabilityExchange, json.loads(output_json.read_text(encoding="utf-8")))
        if not self.artifact_policy.keep_successes:
            shutil.rmtree(run_dir, ignore_errors=True)
        return capability

    def run_batch_protocol(
        self,
        runtime_dir: str | Path,
        request: RuntimeBatchRequest,
        *,
        provider: ModelProvider,
        runtime_profile: RuntimeProfile | None = None,
    ) -> RuntimeBatchResponse:
        self.ensure_image()
        runtime_path = Path(runtime_dir).resolve()
        profile_payload = model_dump(runtime_profile) if runtime_profile is not None else None
        provider_config = provider_payload(provider)
        run_dir = ensure_directory(
            self.workspace
            / stable_hash(runtime_path, model_dump(request), provider_config, profile_payload, self.base_image)[:12]
        )
        task_runs_json = run_dir / "task_runs.json"
        profile_json = run_dir / "profile.json"
        provider_json = run_dir / "provider.json"
        output_json = run_dir / "run_result.json"
        workspace_dir = ensure_directory(run_dir / "workspace")
        task_runs_json.write_text(json.dumps(model_dump(request), indent=2, sort_keys=True), encoding="utf-8")
        if profile_payload is not None:
            profile_json.write_text(json.dumps(profile_payload, indent=2, sort_keys=True), encoding="utf-8")
        mounts = [
            f"{runtime_path}:/mnt/runtime:ro",
            f"{task_runs_json.resolve()}:/mnt/task_runs.json:ro",
            f"{workspace_dir.resolve()}:/mnt/workspace",
            f"{output_json.parent.resolve()}:/mnt/output",
        ]
        if profile_payload is not None:
            mounts.append(f"{profile_json.resolve()}:/mnt/profile.json:ro")
        provider_file_map: dict[str, str] = {}
        for index, host_path_text in enumerate(provider_payload_file_paths(provider_config)):
            host_path = Path(host_path_text).resolve()
            container_path = f"/mnt/provider_files/{index}_{host_path.name}"
            mounts.append(f"{host_path}:{container_path}:ro")
            provider_file_map[host_path_text] = container_path
        provider_json.write_text(
            json.dumps(rewrite_provider_payload_file_paths(provider_config, provider_file_map), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        mounts.append(f"{provider_json.resolve()}:/mnt/provider.json:ro")
        command = [
            "docker",
            "run",
            "--rm",
            "-e",
            f"PYTHONPATH=/mnt/runtime/{KERNEL_BUNDLE_DIR}",
        ]
        for env_name in provider_environment_names_for_instance(provider):
            env_value = os.environ.get(env_name)
            if env_value:
                command.extend(["-e", f"{env_name}={env_value}"])
        for mount in mounts:
            command.extend(["-v", mount])
        command.extend(
            [
                self.image_tag,
                "python",
                "-m",
                "agintor_runtime.runtime_entry",
                "run-batch",
                "--runtime-dir",
                "/mnt/runtime",
                "--input-json",
                "/mnt/task_runs.json",
                "--provider-json",
                "/mnt/provider.json",
                "--output-json",
                "/mnt/output/run_result.json",
                "--workspace",
                "/mnt/workspace",
                "--artifact-mode",
                self.artifact_policy.mode.value,
            ]
        )
        if profile_payload is not None:
            command.extend(["--profile-json", "/mnt/profile.json"])
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "docker run failed")
        response = model_validate(RuntimeBatchResponse, json.loads(output_json.read_text(encoding="utf-8")))
        self._rewrite_response_paths(response, workspace_dir)
        if not self.artifact_policy.keep_successes:
            shutil.rmtree(run_dir, ignore_errors=True)
        return response

    @staticmethod
    def _host_workspace_path(path_text: str | None, workspace_dir: Path) -> str | None:
        if not path_text:
            return path_text
        prefix = "/mnt/workspace"
        if path_text == prefix:
            return str(workspace_dir.resolve())
        if path_text.startswith(prefix + "/"):
            relative = path_text[len(prefix) + 1 :]
            return str((workspace_dir / relative).resolve())
        return path_text

    def _rewrite_response_paths(self, response: RuntimeBatchResponse, workspace_dir: Path) -> None:
        for run in response.run_results:
            run.trace_path = self._host_workspace_path(run.trace_path, workspace_dir)
            run.checkpoint_ref = self._host_workspace_path(run.checkpoint_ref, workspace_dir)
