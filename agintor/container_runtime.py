from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import state_store
from .artifacts import ArtifactMode, ArtifactPolicy
from .providers import (
    ModelProvider,
    provider_environment_names_for_instance,
    provider_payload,
    provider_payload_file_paths,
    rewrite_provider_payload_file_paths,
)
from .runtime_loader import resolve_docker_launch_policy
from .runtime_profile import RuntimeProfile
from .runtime_sdk import KERNEL_BUNDLE_DIR
from .schemas import (
    AsyncHandle,
    AttemptManifest,
    BenchmarkTask,
    CapabilityExchange,
    CheckpointEnvelope,
    CheckpointReference,
    InspectRequest,
    RequestFileRef,
    ResumeRequest,
    RunManifest,
    RunResult,
    RuntimeBatchRequest,
    RuntimeBatchResponse,
    RuntimeResumeRequest,
    RuntimeSolveRequest,
    RuntimeSolveResponse,
    RuntimeTaskInvocation,
)
from .runtime_api import _compile_request_file_ref, normalize_benchmark_request_id
from .utils import ensure_directory, file_digest, stable_hash


class DockerRuntimeExecutor:
    RUNS_MOUNT_ROOT = "/mnt/runs"
    REQUEST_FILES_MOUNT_ROOT = "/mnt/request-files"

    def __init__(
        self,
        workspace: Path,
        repo_root: Path | None = None,
        image_name_prefix: str = "agintor-runtime",
        base_image: str = "python:3.12-slim",
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
        self._cached_source_digest = self._compute_source_digest()
        self.image_tag = f"{self.image_name_prefix}:{self._cached_source_digest[:12]}"

    def _compute_source_digest(self) -> str:
        relevant = [self.repo_root / "pyproject.toml"]
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
                "COPY pyproject.toml /opt/agintor/",
                "RUN printf '# Agintor\\n' > /opt/agintor/README.md",
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
        dockerfile_path = dockerfile_dir / f"Dockerfile.{self._cached_source_digest[:12]}"
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

    def _should_retain_run_dir(self, *, failed: bool) -> bool:
        if failed and self.artifact_policy.keep_failures:
            return True
        if not failed and self.artifact_policy.keep_successes:
            return True
        return False

    def _cleanup_run_dir(self, run_dir: Path, *, failed: bool) -> None:
        if self._should_retain_run_dir(failed=failed):
            return
        shutil.rmtree(run_dir, ignore_errors=True)

    @classmethod
    def _docker_run_argv(
        cls,
        *,
        image_tag: str,
        entrypoint_argv: Sequence[str],
        mounts: Sequence[str],
        env_vars: Mapping[str, str],
        network_none: bool,
    ) -> list[str]:
        argv = ["docker", "run", "--rm", "--init"]
        if network_none:
            argv.extend(["--network", "none"])
        for env_name in sorted(env_vars):
            argv.extend(["-e", f"{env_name}={env_vars[env_name]}"])
        for mount in mounts:
            argv.extend(["-v", mount])
        argv.append(image_tag)
        argv.extend(entrypoint_argv)
        return argv

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
        request_id_rows = [
            {"task_id": task.task_id, "seed": int(seed)}
            for task, seed in task_runs
        ]
        response = self.run_batch_protocol(
            runtime_dir,
            RuntimeBatchRequest(
                request_id=f"docker.{stable_hash(runtime_dir, request_id_rows)[:12]}",
                runtime_backend="docker",
                invocations=[
                    RuntimeTaskInvocation(
                        request_id=normalize_benchmark_request_id(task.task_id, int(seed)),
                        seed=int(seed),
                        task=task,
                    )
                    for task, seed in task_runs
                ],
            ),
            provider=provider,
            runtime_profile=runtime_profile,
        )
        return response.run_results

    def inspect(self, runtime_dir: str | Path, request: InspectRequest) -> CapabilityExchange:
        runtime_path = Path(runtime_dir).resolve()
        launch_policy = resolve_docker_launch_policy(runtime_path)
        self.ensure_image()
        run_dir = ensure_directory(self.workspace / f"inspect_{stable_hash(runtime_path, (request).model_dump(), self.base_image)[:12]}")
        input_json = run_dir / "inspect_request.json"
        output_json = run_dir / "inspect_response.json"
        workspace_dir = ensure_directory(run_dir / "workspace")
        input_json.write_text(json.dumps((request).model_dump(), indent=2, sort_keys=True), encoding="utf-8")
        command = self._docker_run_argv(
            image_tag=self.image_tag,
            entrypoint_argv=[
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
            ],
            mounts=[
                f"{runtime_path}:/mnt/runtime:ro",
                f"{input_json.resolve()}:/mnt/input.json:ro",
                f"{output_json.parent.resolve()}:/mnt/output",
                f"{workspace_dir.resolve()}:/mnt/workspace",
            ],
            env_vars={
                "PYTHONPATH": f"/mnt/runtime/{KERNEL_BUNDLE_DIR}",
                "AGINTOR_RUNTIME_BACKEND": "docker",
            },
            network_none=launch_policy.network_none,
        )
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
        capability = (CapabilityExchange).model_validate(json.loads(output_json.read_text(encoding="utf-8")))
        self._cleanup_run_dir(run_dir, failed=False)
        return capability

    def run_batch_protocol(
        self,
        runtime_dir: str | Path,
        request: RuntimeBatchRequest,
        *,
        provider: ModelProvider,
        runtime_profile: RuntimeProfile | None = None,
    ) -> RuntimeBatchResponse:
        runtime_path = Path(runtime_dir).resolve()
        launch_policy = resolve_docker_launch_policy(runtime_path)
        self.ensure_image()
        profile_payload = (runtime_profile).model_dump() if runtime_profile is not None else None
        provider_config = provider_payload(provider)
        run_dir = ensure_directory(
            self.workspace
            / stable_hash(runtime_path, (request).model_dump(), provider_config, profile_payload, self.base_image)[:12]
        )
        task_runs_json = run_dir / "task_runs.json"
        profile_json = run_dir / "profile.json"
        provider_json = run_dir / "provider.json"
        output_json = run_dir / "run_result.json"
        workspace_dir = ensure_directory(run_dir / "workspace")
        container_request, run_mount_root = self._containerize_batch_request(request)
        container_request, request_file_mounts, request_file_reverse_map = self._containerize_batch_request_file_refs(
            container_request,
            run_mount_root=run_mount_root,
        )
        task_runs_json.write_text(json.dumps((container_request).model_dump(), indent=2, sort_keys=True), encoding="utf-8")
        if profile_payload is not None:
            profile_json.write_text(json.dumps(profile_payload, indent=2, sort_keys=True), encoding="utf-8")
        mounts = [
            f"{runtime_path}:/mnt/runtime:ro",
            f"{task_runs_json.resolve()}:/mnt/task_runs.json:ro",
            f"{workspace_dir.resolve()}:/mnt/workspace",
            f"{output_json.parent.resolve()}:/mnt/output",
        ]
        if run_mount_root is not None:
            mounts.append(f"{run_mount_root.resolve()}:{self.RUNS_MOUNT_ROOT}")
        mounts.extend(request_file_mounts)
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
        if str(request.runtime_backend or "").strip().lower() != "docker":
            raise ValueError(f"docker executor received non-docker batch request backend {request.runtime_backend!r}")
        env_vars = {
            "PYTHONPATH": f"/mnt/runtime/{KERNEL_BUNDLE_DIR}",
            "AGINTOR_RUNTIME_BACKEND": "docker",
        }
        for env_name in provider_environment_names_for_instance(provider):
            env_value = os.environ.get(env_name)
            if env_value:
                env_vars[env_name] = env_value
        entrypoint_argv = [
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
        if profile_payload is not None:
            entrypoint_argv.extend(["--profile-json", "/mnt/profile.json"])
        command = self._docker_run_argv(
            image_tag=self.image_tag,
            entrypoint_argv=entrypoint_argv,
            mounts=mounts,
            env_vars=env_vars,
            network_none=launch_policy.network_none,
        )
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
        response = (RuntimeBatchResponse).model_validate(json.loads(output_json.read_text(encoding="utf-8")))
        self._rewrite_response_paths(
            response,
            workspace_dir,
            runtime_path=runtime_path,
            run_mount_root=run_mount_root,
            request_file_reverse_map=request_file_reverse_map,
        )
        for run_root in sorted({str(run.run_root or "").strip() for run in response.run_results if str(run.run_root or "").strip()}):
            self._rewrite_durable_run_paths(
                run_root,
                runtime_path=runtime_path,
                run_mount_root=run_mount_root,
                request_file_reverse_map=request_file_reverse_map,
            )
        failed = any(run.hard_invalid for run in response.run_results)
        self._cleanup_run_dir(run_dir, failed=failed)
        return response

    def solve_protocol(
        self,
        runtime_dir: str | Path,
        request: RuntimeSolveRequest,
        *,
        provider: ModelProvider,
        runtime_profile: RuntimeProfile | None = None,
    ) -> RuntimeSolveResponse:
        runtime_path = Path(runtime_dir).resolve()
        launch_policy = resolve_docker_launch_policy(runtime_path)
        self.ensure_image()
        profile_payload = (runtime_profile).model_dump() if runtime_profile is not None else None
        provider_config = provider_payload(provider)
        run_dir = ensure_directory(
            self.workspace
            / stable_hash("solve", runtime_path, (request).model_dump(), provider_config, profile_payload, self.base_image)[:12]
        )
        request_json = run_dir / "solve_request.json"
        profile_json = run_dir / "profile.json"
        provider_json = run_dir / "provider.json"
        output_json = run_dir / "solve_result.json"
        workspace_dir = ensure_directory(run_dir / "workspace")
        container_request, run_mount_root = self._containerize_solve_request(request)
        container_request, request_file_mounts, request_file_reverse_map = self._containerize_solve_request_file_refs(
            container_request,
            run_mount_root=run_mount_root,
        )
        request_json.write_text(json.dumps((container_request).model_dump(), indent=2, sort_keys=True), encoding="utf-8")
        if profile_payload is not None:
            profile_json.write_text(json.dumps(profile_payload, indent=2, sort_keys=True), encoding="utf-8")
        mounts = [
            f"{runtime_path}:/mnt/runtime:ro",
            f"{request_json.resolve()}:/mnt/solve_request.json:ro",
            f"{workspace_dir.resolve()}:/mnt/workspace",
            f"{output_json.parent.resolve()}:/mnt/output",
        ]
        if run_mount_root is not None:
            mounts.append(f"{run_mount_root.resolve()}:{self.RUNS_MOUNT_ROOT}")
        mounts.extend(request_file_mounts)
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
        if str(request.runtime_backend or "").strip().lower() != "docker":
            raise ValueError(f"docker executor received non-docker solve request backend {request.runtime_backend!r}")
        env_vars = {
            "PYTHONPATH": f"/mnt/runtime/{KERNEL_BUNDLE_DIR}",
            "AGINTOR_RUNTIME_BACKEND": "docker",
        }
        for env_name in provider_environment_names_for_instance(provider):
            env_value = os.environ.get(env_name)
            if env_value:
                env_vars[env_name] = env_value
        entrypoint_argv = [
            "python",
            "-m",
            "agintor_runtime.runtime_entry",
            "solve",
            "--runtime-dir",
            "/mnt/runtime",
            "--input-json",
            "/mnt/solve_request.json",
            "--provider-json",
            "/mnt/provider.json",
            "--output-json",
            "/mnt/output/solve_result.json",
            "--workspace",
            "/mnt/workspace",
            "--artifact-mode",
            self.artifact_policy.mode.value,
        ]
        if profile_payload is not None:
            entrypoint_argv.extend(["--profile-json", "/mnt/profile.json"])
        command = self._docker_run_argv(
            image_tag=self.image_tag,
            entrypoint_argv=entrypoint_argv,
            mounts=mounts,
            env_vars=env_vars,
            network_none=launch_policy.network_none,
        )
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "docker solve failed")
        response = (RuntimeSolveResponse).model_validate(json.loads(output_json.read_text(encoding="utf-8")))
        self._rewrite_solve_response_paths(
            response,
            workspace_dir,
            runtime_path=runtime_path,
            run_mount_root=run_mount_root,
            request_file_reverse_map=request_file_reverse_map,
        )
        self._rewrite_durable_run_paths(
            response.solve_result.run_root or request.run_root,
            runtime_path=runtime_path,
            run_mount_root=run_mount_root,
            request_file_reverse_map=request_file_reverse_map,
        )
        failed = response.solve_result.status in {"failed", "controlled_failure"} or bool(response.solve_result.faults.get("hard_invalid"))
        self._cleanup_run_dir(run_dir, failed=failed)
        return response

    def resume_protocol(
        self,
        runtime_dir: str | Path,
        request: RuntimeResumeRequest,
        *,
        provider: ModelProvider,
        runtime_profile: RuntimeProfile | None = None,
    ) -> RuntimeSolveResponse:
        runtime_path = Path(runtime_dir).resolve()
        launch_policy = resolve_docker_launch_policy(runtime_path)
        self.ensure_image()
        profile_payload = (runtime_profile).model_dump() if runtime_profile is not None else None
        provider_config = provider_payload(provider)
        run_dir = ensure_directory(
            self.workspace
            / stable_hash("resume", runtime_path, (request).model_dump(), provider_config, profile_payload, self.base_image)[:12]
        )
        request_json = run_dir / "resume_request.json"
        profile_json = run_dir / "profile.json"
        provider_json = run_dir / "provider.json"
        output_json = run_dir / "resume_result.json"
        workspace_dir = ensure_directory(run_dir / "workspace")
        resume_request_file_refs = self._checkpoint_request_file_refs(request.checkpoint_ref)
        container_request, checkpoint_store_dir, run_mount_root = self._container_resume_request(request)
        _, request_file_mounts, _, request_file_reverse_map = self._containerize_request_file_refs(
            resume_request_file_refs,
            run_mount_root=run_mount_root,
        )
        request_json.write_text(json.dumps((container_request).model_dump(), indent=2, sort_keys=True), encoding="utf-8")
        if profile_payload is not None:
            profile_json.write_text(json.dumps(profile_payload, indent=2, sort_keys=True), encoding="utf-8")
        mounts = [
            f"{runtime_path}:/mnt/runtime:ro",
            f"{request_json.resolve()}:/mnt/resume_request.json:ro",
            f"{workspace_dir.resolve()}:/mnt/workspace",
            f"{output_json.parent.resolve()}:/mnt/output",
        ]
        if checkpoint_store_dir is not None:
            mounts.append(f"{checkpoint_store_dir.resolve()}:/mnt/checkpoints:ro")
        if run_mount_root is not None:
            mounts.append(f"{run_mount_root.resolve()}:{self.RUNS_MOUNT_ROOT}")
        mounts.extend(request_file_mounts)
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
        if str(request.runtime_backend or "").strip().lower() != "docker":
            raise ValueError(f"docker executor received non-docker resume request backend {request.runtime_backend!r}")
        env_vars = {
            "PYTHONPATH": f"/mnt/runtime/{KERNEL_BUNDLE_DIR}",
            "AGINTOR_RUNTIME_BACKEND": "docker",
        }
        for env_name in provider_environment_names_for_instance(provider):
            env_value = os.environ.get(env_name)
            if env_value:
                env_vars[env_name] = env_value
        entrypoint_argv = [
            "python",
            "-m",
            "agintor_runtime.runtime_entry",
            "resume",
            "--runtime-dir",
            "/mnt/runtime",
            "--input-json",
            "/mnt/resume_request.json",
            "--provider-json",
            "/mnt/provider.json",
            "--output-json",
            "/mnt/output/resume_result.json",
            "--workspace",
            "/mnt/workspace",
            "--artifact-mode",
            self.artifact_policy.mode.value,
        ]
        if profile_payload is not None:
            entrypoint_argv.extend(["--profile-json", "/mnt/profile.json"])
        command = self._docker_run_argv(
            image_tag=self.image_tag,
            entrypoint_argv=entrypoint_argv,
            mounts=mounts,
            env_vars=env_vars,
            network_none=launch_policy.network_none,
        )
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "docker resume failed")
        response = (RuntimeSolveResponse).model_validate(json.loads(output_json.read_text(encoding="utf-8")))
        self._rewrite_solve_response_paths(
            response,
            workspace_dir,
            checkpoint_store_dir,
            runtime_path=runtime_path,
            run_mount_root=run_mount_root,
            request_file_reverse_map=request_file_reverse_map,
        )
        self._rewrite_durable_run_paths(
            response.solve_result.run_root or request.run_root,
            runtime_path=runtime_path,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
            request_file_reverse_map=request_file_reverse_map,
        )
        failed = response.solve_result.status in {"failed", "controlled_failure"} or bool(response.solve_result.faults.get("hard_invalid"))
        self._cleanup_run_dir(run_dir, failed=failed)
        return response

    @staticmethod
    def _common_run_mount_root(run_roots: list[str]) -> Path | None:
        resolved = [
            Path(path_text).resolve()
            for path_text in run_roots
            if str(path_text or "").strip()
        ]
        if not resolved:
            return None
        parent_strings = [str(path.parent.resolve()) for path in resolved]
        return Path(os.path.commonpath(parent_strings))

    @classmethod
    def _container_run_path(cls, path_text: str | None, run_mount_root: Path | None) -> str | None:
        if not path_text or run_mount_root is None:
            return path_text
        path = Path(path_text).resolve()
        try:
            relative = path.relative_to(run_mount_root)
        except ValueError:
            return path_text
        if str(relative) == ".":
            return cls.RUNS_MOUNT_ROOT
        return f"{cls.RUNS_MOUNT_ROOT}/{relative.as_posix()}"

    @classmethod
    def _containerize_solve_request(
        cls,
        request: RuntimeSolveRequest,
    ) -> tuple[RuntimeSolveRequest, Path | None]:
        run_mount_root = cls._common_run_mount_root([request.run_root])
        return (
            request.model_copy(update={"run_root": cls._container_run_path(request.run_root, run_mount_root) or ""}),
            run_mount_root,
        )

    @classmethod
    def _containerize_batch_request(
        cls,
        request: RuntimeBatchRequest,
    ) -> tuple[RuntimeBatchRequest, Path | None]:
        run_mount_root = cls._common_run_mount_root([invocation.run_root for invocation in request.invocations])
        invocations = [
            invocation.model_copy(
                update={"run_root": cls._container_run_path(invocation.run_root, run_mount_root) or ""}
            )
            for invocation in request.invocations
        ]
        return request.model_copy(update={"invocations": invocations}), run_mount_root

    @classmethod
    def _container_resume_request(
        cls,
        request: RuntimeResumeRequest,
    ) -> tuple[RuntimeResumeRequest, Path | None, Path | None]:
        run_mount_root = cls._common_run_mount_root([request.run_root])
        checkpoint_store_dir: Path | None = None
        checkpoint_ref = request.checkpoint_ref
        checkpoint_store = str(request.checkpoint_store_dir or "").strip()
        if str(checkpoint_ref or "").strip():
            rewritten = (
                cls._container_run_path(checkpoint_ref, run_mount_root)
                if run_mount_root is not None
                else None
            )
            if rewritten is not None and rewritten != checkpoint_ref:
                checkpoint_ref = rewritten
                checkpoint_store_dir = run_mount_root
                checkpoint_store = cls.RUNS_MOUNT_ROOT
            else:
                checkpoint_path = Path(request.checkpoint_ref).resolve()
                checkpoint_store_dir = (
                    Path(checkpoint_store).resolve()
                    if checkpoint_store
                    else checkpoint_path.parent
                )
                try:
                    relative_ref = checkpoint_path.relative_to(checkpoint_store_dir)
                except ValueError:
                    checkpoint_store_dir = checkpoint_path.parent
                    relative_ref = Path(checkpoint_path.name)
                checkpoint_ref = f"/mnt/checkpoints/{relative_ref.as_posix()}"
                checkpoint_store = "/mnt/checkpoints"
        return (
            request.model_copy(
                update={
                    "checkpoint_ref": checkpoint_ref,
                    "checkpoint_store_dir": checkpoint_store,
                    "run_root": cls._container_run_path(request.run_root, run_mount_root) or "",
                }
            ),
            checkpoint_store_dir,
            run_mount_root,
        )

    @staticmethod
    def _rewrite_exact_string_payload(payload: Any, replacements: Mapping[str, str]) -> Any:
        if isinstance(payload, Mapping):
            return {
                str(key): DockerRuntimeExecutor._rewrite_exact_string_payload(value, replacements)
                for key, value in payload.items()
            }
        if isinstance(payload, list):
            return [DockerRuntimeExecutor._rewrite_exact_string_payload(item, replacements) for item in payload]
        if isinstance(payload, str):
            rewritten = replacements.get(payload, payload)
            if rewritten != payload:
                return rewritten
            for source, target in replacements.items():
                prefix = f"{source}/"
                if source and payload.startswith(prefix):
                    relative = payload[len(prefix):]
                    return str((Path(target) / Path(*relative.split("/"))).resolve())
            for source, target in replacements.items():
                if source and source in rewritten:
                    rewritten = rewritten.replace(source, target)
            return rewritten
        return payload

    @classmethod
    def _mounted_path_replacements(
        cls,
        *,
        runtime_path: Path | None = None,
        run_mount_root: Path | None = None,
        checkpoint_store_dir: Path | None = None,
        request_file_reverse_map: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        replacements = {
            str(source): str(target)
            for source, target in dict(request_file_reverse_map or {}).items()
            if str(source).strip() and str(target).strip()
        }
        if run_mount_root is not None:
            replacements[cls.RUNS_MOUNT_ROOT] = str(run_mount_root.resolve())
        if checkpoint_store_dir is not None:
            replacements["/mnt/checkpoints"] = str(checkpoint_store_dir.resolve())
        if runtime_path is not None:
            replacements["/mnt/runtime"] = str(runtime_path.resolve())
        return replacements

    @classmethod
    def _container_request_file_mount_path(cls, host_path: Path) -> str:
        return f"{cls.REQUEST_FILES_MOUNT_ROOT}/{stable_hash(str(host_path.resolve()))[:12]}/{host_path.name}"

    @classmethod
    def _containerize_request_file_refs(
        cls,
        request_file_refs: Sequence[RequestFileRef],
        *,
        run_mount_root: Path | None,
    ) -> tuple[list[RequestFileRef], list[str], dict[str, str], dict[str, str]]:
        updated_refs: list[RequestFileRef] = []
        mounts: list[str] = []
        forward_map: dict[str, str] = {}
        reverse_map: dict[str, str] = {}
        mounted_host_paths: set[str] = set()
        for file_ref in request_file_refs:
            if file_ref.path_root != "host_absolute" or not str(file_ref.host_path or "").strip():
                updated_refs.append((file_ref).model_copy(deep=True))
                continue
            host_path = Path(file_ref.host_path).resolve()
            container_path = cls._container_run_path(str(host_path), run_mount_root)
            if container_path == str(host_path):
                container_path = cls._container_request_file_mount_path(host_path)
                if str(host_path) not in mounted_host_paths:
                    mounts.append(f"{host_path}:{container_path}:rw")
                    mounted_host_paths.add(str(host_path))
            updated_ref = (file_ref).model_copy(update={"runtime_path": container_path}, deep=True)
            updated_refs.append(updated_ref)
            forward_map[file_ref.source_path] = container_path
            forward_map[str(host_path)] = container_path
            reverse_map[container_path] = str(host_path)
        return updated_refs, mounts, forward_map, reverse_map

    @classmethod
    def _containerize_solve_request_file_refs(
        cls,
        request: RuntimeSolveRequest,
        *,
        run_mount_root: Path | None,
    ) -> tuple[RuntimeSolveRequest, list[str], dict[str, str]]:
        if request.mode != "user_request" or request.solve_request is None:
            return request, [], {}
        request_file_refs = [
            (RequestFileRef).model_validate((file_ref).model_dump())
            for file_ref in request.solve_request.request_file_refs
        ]
        if not request_file_refs:
            return request, [], {}
        updated_refs, mounts, forward_map, reverse_map = cls._containerize_request_file_refs(
            request_file_refs,
            run_mount_root=run_mount_root,
        )
        payload = (request.solve_request).model_dump()
        payload["request_file_refs"] = [(file_ref).model_dump() for file_ref in updated_refs]
        payload["file_paths"] = [file_ref.runtime_path for file_ref in updated_refs]
        payload["context_items"] = cls._rewrite_exact_string_payload(payload.get("context_items", []), forward_map)
        return (
            request.model_copy(update={"solve_request": (type(request.solve_request)).model_validate(payload)}),
            mounts,
            reverse_map,
        )

    @classmethod
    def _containerize_task_file_refs(
        cls,
        task: BenchmarkTask,
        *,
        run_mount_root: Path | None,
    ) -> tuple[BenchmarkTask, list[str], dict[str, str]]:
        absolute_paths = [str(path).strip() for path in task.file_paths if Path(str(path)).is_absolute()]
        if not absolute_paths:
            return task, [], {}
        request_file_refs = [_compile_request_file_ref(path) for path in absolute_paths]
        updated_refs, mounts, forward_map, reverse_map = cls._containerize_request_file_refs(
            request_file_refs,
            run_mount_root=run_mount_root,
        )
        payload = cls._rewrite_exact_string_payload((task).model_dump(), forward_map)
        payload["file_paths"] = [forward_map.get(str(path), str(path)) for path in task.file_paths]
        payload.setdefault("metadata", {})
        payload["metadata"]["request_file_refs"] = [(file_ref).model_dump() for file_ref in updated_refs]
        return (BenchmarkTask).model_validate(payload), mounts, reverse_map

    @classmethod
    def _containerize_batch_request_file_refs(
        cls,
        request: RuntimeBatchRequest,
        *,
        run_mount_root: Path | None,
    ) -> tuple[RuntimeBatchRequest, list[str], dict[str, str]]:
        updated_invocations: list[RuntimeTaskInvocation] = []
        mounts: list[str] = []
        reverse_map: dict[str, str] = {}
        seen_mounts: set[str] = set()
        for invocation in request.invocations:
            rewritten_task, task_mounts, task_reverse_map = cls._containerize_task_file_refs(
                invocation.task,
                run_mount_root=run_mount_root,
            )
            updated_invocations.append(invocation.model_copy(update={"task": rewritten_task}))
            for mount in task_mounts:
                if mount not in seen_mounts:
                    mounts.append(mount)
                    seen_mounts.add(mount)
            reverse_map.update(task_reverse_map)
        return request.model_copy(update={"invocations": updated_invocations}), mounts, reverse_map

    @classmethod
    def _checkpoint_request_file_refs(
        cls,
        checkpoint_ref: str | Path | None,
    ) -> list[RequestFileRef]:
        text = str(checkpoint_ref or "").strip()
        if not text:
            return []
        checkpoint_path = Path(text).expanduser().resolve()
        if not checkpoint_path.exists():
            return []
        payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        envelope = (CheckpointEnvelope).model_validate(payload)
        request_file_refs_payload = envelope.plan_snapshot.get("file_ref_specs", [])
        if isinstance(request_file_refs_payload, list) and request_file_refs_payload:
            return [
                (RequestFileRef).model_validate(row)
                for row in request_file_refs_payload
                if isinstance(row, Mapping)
            ]
        task_metadata = envelope.task_payload.get("metadata", {}) if isinstance(envelope.task_payload, Mapping) else {}
        metadata_refs = task_metadata.get("request_file_refs", []) if isinstance(task_metadata, Mapping) else []
        if isinstance(metadata_refs, list) and metadata_refs:
            return [
                (RequestFileRef).model_validate(row)
                for row in metadata_refs
                if isinstance(row, Mapping)
            ]
        task_file_paths = envelope.task_payload.get("file_paths", []) if isinstance(envelope.task_payload, Mapping) else []
        absolute_paths = [str(path).strip() for path in task_file_paths if Path(str(path)).is_absolute()]
        return [_compile_request_file_ref(path) for path in absolute_paths]

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

    @staticmethod
    def _host_mounted_path(path_text: str | None, mount_root: str, host_root: Path | None) -> str | None:
        if not path_text or host_root is None:
            return path_text
        if path_text == mount_root:
            return str(host_root.resolve())
        if path_text.startswith(mount_root + "/"):
            relative = path_text[len(mount_root) + 1 :]
            return str((host_root / relative).resolve())
        return path_text

    @classmethod
    def _rewrite_known_path(
        cls,
        path_text: str | None,
        *,
        run_mount_root: Path | None = None,
        checkpoint_store_dir: Path | None = None,
    ) -> str | None:
        rewritten = cls._host_mounted_path(path_text, cls.RUNS_MOUNT_ROOT, run_mount_root)
        return cls._host_mounted_path(rewritten, "/mnt/checkpoints", checkpoint_store_dir)

    @classmethod
    def _rewrite_async_handle_paths(
        cls,
        handle: AsyncHandle,
        *,
        run_mount_root: Path | None = None,
        checkpoint_store_dir: Path | None = None,
    ) -> AsyncHandle:
        payload = (handle).model_dump()
        payload["working_directory"] = cls._rewrite_known_path(
            handle.working_directory,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        ) or ""
        payload["stdout_path"] = cls._rewrite_known_path(
            handle.stdout_path,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        )
        payload["stderr_path"] = cls._rewrite_known_path(
            handle.stderr_path,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        )
        payload["artifact_refs"] = [
            cls._rewrite_known_path(
                ref,
                run_mount_root=run_mount_root,
                checkpoint_store_dir=checkpoint_store_dir,
            )
            or ref
            for ref in handle.artifact_refs
        ]
        return (AsyncHandle).model_validate(payload)

    @staticmethod
    def _open_handle_payloads(value: Any) -> list[Any]:
        if hasattr(value, "handles"):
            return list(getattr(value, "handles") or [])
        if isinstance(value, Mapping):
            return list(value.get("handles", []) or [])
        if isinstance(value, list):
            return list(value)
        return []

    @classmethod
    def _rewrite_working_memory_snapshot_paths(
        cls,
        snapshot_payload: Mapping[str, Any],
        *,
        run_mount_root: Path | None = None,
        checkpoint_store_dir: Path | None = None,
    ) -> dict[str, Any]:
        payload = dict(snapshot_payload)
        selected_refs = payload.get("selected_checkpoint_refs", [])
        if isinstance(selected_refs, list):
            payload["selected_checkpoint_refs"] = [
                cls._rewrite_known_path(
                    str(ref),
                    run_mount_root=run_mount_root,
                    checkpoint_store_dir=checkpoint_store_dir,
                )
                or str(ref)
                for ref in selected_refs
            ]
        return payload

    @classmethod
    def _rewrite_recovery_payload_paths(
        cls,
        payload: Any,
        *,
        run_mount_root: Path | None = None,
        checkpoint_store_dir: Path | None = None,
        path_replacements: Mapping[str, str] | None = None,
    ) -> Any:
        rewritten = cls._rewrite_exact_string_payload(payload, dict(path_replacements or {}))
        if not isinstance(rewritten, Mapping):
            return rewritten
        result = dict(rewritten)
        for key in ("selected_checkpoint_ref", "source_checkpoint_ref"):
            if key not in result:
                continue
            result[key] = cls._rewrite_known_path(
                result.get(key),
                run_mount_root=run_mount_root,
                checkpoint_store_dir=checkpoint_store_dir,
            ) or result.get(key)
        return result

    @classmethod
    def _rewrite_branch_resume_snapshot_paths(
        cls,
        snapshot_payload: Mapping[str, Any],
        *,
        run_mount_root: Path | None = None,
        checkpoint_store_dir: Path | None = None,
    ) -> dict[str, Any]:
        payload = dict(snapshot_payload)
        payload["shell_state_snapshot"] = dict(payload.get("shell_state_snapshot") or {})
        payload["shell_state_snapshot"]["open_handles"] = {
            "handles": [
                (cls._rewrite_async_handle_paths(
                        (AsyncHandle).model_validate(handle_payload),
                        run_mount_root=run_mount_root,
                        checkpoint_store_dir=checkpoint_store_dir,
                    )).model_dump()
                for handle_payload in cls._open_handle_payloads(payload["shell_state_snapshot"].get("open_handles", []))
            ]
        }
        return payload

    @classmethod
    def _rewrite_checkpoint_reference_paths(
        cls,
        reference: CheckpointReference,
        *,
        run_mount_root: Path | None = None,
        checkpoint_store_dir: Path | None = None,
    ) -> CheckpointReference:
        payload = (reference).model_dump()
        payload["ref"] = cls._rewrite_known_path(
            reference.ref,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        ) or ""
        payload["run_root"] = cls._rewrite_known_path(
            reference.run_root,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        ) or ""
        return (CheckpointReference).model_validate(payload)

    @classmethod
    def _rewrite_run_manifest_paths(
        cls,
        manifest: RunManifest,
        *,
        run_mount_root: Path | None = None,
        checkpoint_store_dir: Path | None = None,
    ) -> RunManifest:
        payload = (manifest).model_dump()
        payload["run_root"] = cls._rewrite_known_path(
            manifest.run_root,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        ) or ""
        payload["latest_checkpoint_ref"] = cls._rewrite_known_path(
            manifest.latest_checkpoint_ref,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        )
        return (RunManifest).model_validate(payload)

    @classmethod
    def _rewrite_attempt_manifest_paths(
        cls,
        manifest: AttemptManifest,
        *,
        run_mount_root: Path | None = None,
        checkpoint_store_dir: Path | None = None,
    ) -> AttemptManifest:
        payload = (manifest).model_dump()
        payload["run_root"] = cls._rewrite_known_path(
            manifest.run_root,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        ) or ""
        payload["workspace_root"] = cls._rewrite_known_path(
            manifest.workspace_root,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        ) or manifest.workspace_root
        payload["latest_checkpoint_ref"] = cls._rewrite_known_path(
            manifest.latest_checkpoint_ref,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        )
        payload["resumed_from_checkpoint_ref"] = cls._rewrite_known_path(
            manifest.resumed_from_checkpoint_ref,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        )
        return (AttemptManifest).model_validate(payload)

    @classmethod
    def _rewrite_checkpoint_envelope_paths(
        cls,
        envelope: CheckpointEnvelope,
        *,
        runtime_path: Path | None = None,
        run_mount_root: Path | None = None,
        checkpoint_store_dir: Path | None = None,
        request_file_reverse_map: Mapping[str, str] | None = None,
    ) -> CheckpointEnvelope:
        payload = (envelope).model_dump()
        payload["run_root"] = cls._rewrite_known_path(
            envelope.run_root,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        ) or ""
        payload["source_checkpoint_ref"] = cls._rewrite_known_path(
            envelope.source_checkpoint_ref,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        )
        payload["runtime_state_snapshot"]["latest_checkpoint_ref"] = cls._rewrite_known_path(
            envelope.runtime_state_snapshot.latest_checkpoint_ref,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        )
        payload["attempt_snapshot"]["run_root"] = cls._rewrite_known_path(
            envelope.attempt_snapshot.run_root,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        ) or envelope.attempt_snapshot.run_root
        payload["attempt_snapshot"]["resumed_from_checkpoint_ref"] = cls._rewrite_known_path(
            envelope.attempt_snapshot.resumed_from_checkpoint_ref,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        ) or envelope.attempt_snapshot.resumed_from_checkpoint_ref
        payload["shell_state_snapshot"]["open_handles"] = {
            "handles": [
                (cls._rewrite_async_handle_paths(
                        handle,
                        run_mount_root=run_mount_root,
                        checkpoint_store_dir=checkpoint_store_dir,
                    )).model_dump()
                for handle in envelope.shell_state_snapshot.open_handles.handles
            ]
        }
        payload["runtime_state_snapshot"]["branch_resume_snapshots"] = {
            str(key): cls._rewrite_branch_resume_snapshot_paths(
                dict(value),
                run_mount_root=run_mount_root,
                checkpoint_store_dir=checkpoint_store_dir,
            )
            for key, value in dict(payload["runtime_state_snapshot"].get("branch_resume_snapshots", {})).items()
        }
        payload["working_state"] = cls._rewrite_working_memory_snapshot_paths(
            payload.get("working_state", {}),
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        )
        path_replacements = cls._mounted_path_replacements(
            runtime_path=runtime_path,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
            request_file_reverse_map=request_file_reverse_map,
        )
        if path_replacements:
            payload = cls._rewrite_exact_string_payload(payload, path_replacements)
        return (CheckpointEnvelope).model_validate(payload)

    @classmethod
    def _rewrite_json_file_payload(
        cls,
        path: Path,
        *,
        runtime_path: Path | None = None,
        run_mount_root: Path | None = None,
        checkpoint_store_dir: Path | None = None,
        request_file_reverse_map: Mapping[str, str] | None = None,
    ) -> bool:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return False
        path_replacements = cls._mounted_path_replacements(
            runtime_path=runtime_path,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
            request_file_reverse_map=request_file_reverse_map,
        )
        if path.parent.name == "working_memory":
            rewritten = cls._rewrite_working_memory_snapshot_paths(
                payload if isinstance(payload, Mapping) else {},
                run_mount_root=run_mount_root,
                checkpoint_store_dir=checkpoint_store_dir,
            )
            rewritten = cls._rewrite_exact_string_payload(rewritten, path_replacements)
        elif path.parent.name == "recovery" or path.parent.name == "fingerprints":
            rewritten = cls._rewrite_recovery_payload_paths(
                payload,
                run_mount_root=run_mount_root,
                checkpoint_store_dir=checkpoint_store_dir,
                path_replacements=path_replacements,
            )
        else:
            rewritten = cls._rewrite_exact_string_payload(payload, path_replacements)
        if rewritten == payload:
            return False
        path.write_text(json.dumps(rewritten, indent=2, sort_keys=True), encoding="utf-8")
        return True

    @classmethod
    def _rewrite_jsonl_file_payloads(
        cls,
        path: Path,
        *,
        path_replacements: Mapping[str, str] | None = None,
    ) -> bool:
        replacements = dict(path_replacements or {})
        if not replacements:
            return False
        try:
            original_text = path.read_text(encoding="utf-8")
        except Exception:
            return False
        changed = False
        rewritten_lines: list[str] = []
        for line in original_text.splitlines():
            if not line.strip():
                rewritten_lines.append(line)
                continue
            try:
                payload = json.loads(line)
            except Exception:
                rewritten_lines.append(line)
                continue
            rewritten = cls._rewrite_exact_string_payload(payload, replacements)
            if rewritten != payload:
                changed = True
            rewritten_lines.append(json.dumps(rewritten, sort_keys=True))
        if not changed:
            return False
        suffix = "\n" if original_text.endswith("\n") else ""
        path.write_text("\n".join(rewritten_lines) + suffix, encoding="utf-8")
        return True

    @classmethod
    def _rewrite_durable_run_paths(
        cls,
        run_root: str | Path | None,
        *,
        runtime_path: Path | None = None,
        run_mount_root: Path | None = None,
        checkpoint_store_dir: Path | None = None,
        request_file_reverse_map: Mapping[str, str] | None = None,
    ) -> None:
        text = str(run_root or "").strip()
        if not text:
            return
        root = Path(text).resolve()
        path_replacements = cls._mounted_path_replacements(
            runtime_path=runtime_path,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
            request_file_reverse_map=request_file_reverse_map,
        )
        candidate_paths = [root / "run_manifest.json"]
        candidate_paths.extend(sorted((root / "attempts").glob("*/attempt_manifest.json")))
        candidate_paths.extend(sorted((root / "checkpoints").glob("*.json")))
        rewritten_any = False
        for path in candidate_paths:
            if not path.exists() or not path.is_file():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if path.name == "run_manifest.json":
                    rewritten = (cls._rewrite_run_manifest_paths(
                            (RunManifest).model_validate(payload),
                            run_mount_root=run_mount_root,
                            checkpoint_store_dir=checkpoint_store_dir,
                        )).model_dump()
                elif path.name == "attempt_manifest.json":
                    rewritten = (cls._rewrite_attempt_manifest_paths(
                            (AttemptManifest).model_validate(payload),
                            run_mount_root=run_mount_root,
                            checkpoint_store_dir=checkpoint_store_dir,
                        )).model_dump()
                elif path.name == "LATEST.json":
                    rewritten = (cls._rewrite_checkpoint_reference_paths(
                            (CheckpointReference).model_validate(payload),
                            run_mount_root=run_mount_root,
                            checkpoint_store_dir=checkpoint_store_dir,
                        )).model_dump()
                elif path.name == "index.json":
                    if not isinstance(payload, list):
                        continue
                    rewritten = [
                        (cls._rewrite_checkpoint_reference_paths(
                                (CheckpointReference).model_validate(row),
                                run_mount_root=run_mount_root,
                                checkpoint_store_dir=checkpoint_store_dir,
                            )).model_dump()
                        for row in payload
                    ]
                else:
                    rewritten = (cls._rewrite_checkpoint_envelope_paths(
                            (CheckpointEnvelope).model_validate(payload),
                            runtime_path=runtime_path,
                            run_mount_root=run_mount_root,
                            checkpoint_store_dir=checkpoint_store_dir,
                            request_file_reverse_map=request_file_reverse_map,
                        )).model_dump()
                if path_replacements:
                    rewritten = cls._rewrite_exact_string_payload(rewritten, path_replacements)
            except Exception:
                continue
            if rewritten == payload:
                continue
            path.write_text(json.dumps(rewritten, indent=2, sort_keys=True), encoding="utf-8")
            rewritten_any = True
        payload_paths: list[Path] = []
        payload_paths.extend(sorted((root / "request").glob("*.json")))
        payload_paths.extend(sorted((root / "side_effects").glob("*.json")))
        payload_paths.extend(sorted((root / "events").glob("*.json")))
        payload_paths.extend(sorted((root / "traces").glob("*.json")))
        payload_paths.extend(
            path
            for path in sorted((root / "state").rglob("*.json"))
            if path.name != state_store.INDEX_DIRTY_FILE
        )
        for path in payload_paths:
            if not path.exists() or not path.is_file():
                continue
            rewritten_any = (
                cls._rewrite_json_file_payload(
                    path,
                    runtime_path=runtime_path,
                    run_mount_root=run_mount_root,
                    checkpoint_store_dir=checkpoint_store_dir,
                    request_file_reverse_map=request_file_reverse_map,
                )
                or rewritten_any
            )
        for path in sorted((root / "state").rglob("*.jsonl")):
            if not path.exists() or not path.is_file():
                continue
            rewritten_any = (
                cls._rewrite_jsonl_file_payloads(
                    path,
                    path_replacements=path_replacements,
                )
                or rewritten_any
            )
        if rewritten_any and (root / "state").exists():
            try:
                state_store.rebuild_from_canonical(root)
            except Exception:
                try:
                    state_store.mark_index_dirty(root, reason="docker_path_rewrite_reindex_failed")
                except Exception:
                    pass

    def _rewrite_response_paths(
        self,
        response: RuntimeBatchResponse,
        workspace_dir: Path,
        *,
        runtime_path: Path | None = None,
        run_mount_root: Path | None = None,
        checkpoint_store_dir: Path | None = None,
        request_file_reverse_map: Mapping[str, str] | None = None,
    ) -> None:
        path_replacements = self._mounted_path_replacements(
            runtime_path=runtime_path,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
            request_file_reverse_map=request_file_reverse_map,
        )
        for run in response.run_results:
            run.trace_path = self._host_workspace_path(run.trace_path, workspace_dir)
            run.checkpoint_ref = self._host_workspace_path(run.checkpoint_ref, workspace_dir)
            run.trace_path = self._host_mounted_path(run.trace_path, self.RUNS_MOUNT_ROOT, run_mount_root)
            run.checkpoint_ref = self._host_mounted_path(run.checkpoint_ref, self.RUNS_MOUNT_ROOT, run_mount_root)
            run.latest_checkpoint_ref = self._host_mounted_path(
                run.latest_checkpoint_ref,
                self.RUNS_MOUNT_ROOT,
                run_mount_root,
            )
            run.run_root = self._host_mounted_path(run.run_root, self.RUNS_MOUNT_ROOT, run_mount_root) or ""
            run.artifact = self._rewrite_exact_string_payload(
                run.artifact,
                path_replacements,
            )

    def _rewrite_solve_response_paths(
        self,
        response: RuntimeSolveResponse,
        workspace_dir: Path,
        checkpoint_store_dir: Path | None = None,
        *,
        runtime_path: Path | None = None,
        run_mount_root: Path | None = None,
        request_file_reverse_map: Mapping[str, str] | None = None,
    ) -> None:
        path_replacements = self._mounted_path_replacements(
            runtime_path=runtime_path,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
            request_file_reverse_map=request_file_reverse_map,
        )
        response.solve_result.trace_ref = self._host_workspace_path(response.solve_result.trace_ref, workspace_dir)
        response.solve_result.checkpoint_ref = self._host_workspace_path(response.solve_result.checkpoint_ref, workspace_dir)
        response.solve_result.trace_ref = self._host_mounted_path(
            response.solve_result.trace_ref,
            self.RUNS_MOUNT_ROOT,
            run_mount_root,
        )
        response.solve_result.checkpoint_ref = self._host_mounted_path(
            response.solve_result.checkpoint_ref,
            self.RUNS_MOUNT_ROOT,
            run_mount_root,
        )
        response.solve_result.checkpoint_ref = self._host_mounted_path(
            response.solve_result.checkpoint_ref,
            "/mnt/checkpoints",
            checkpoint_store_dir,
        )
        response.solve_result.latest_checkpoint_ref = self._host_mounted_path(
            response.solve_result.latest_checkpoint_ref,
            self.RUNS_MOUNT_ROOT,
            run_mount_root,
        )
        response.solve_result.latest_checkpoint_ref = self._host_mounted_path(
            response.solve_result.latest_checkpoint_ref,
            "/mnt/checkpoints",
            checkpoint_store_dir,
        )
        response.solve_result.run_root = self._host_mounted_path(
            response.solve_result.run_root,
            self.RUNS_MOUNT_ROOT,
            run_mount_root,
        ) or ""
        response.solve_result.artifact = self._rewrite_exact_string_payload(
            response.solve_result.artifact,
            path_replacements,
        )
