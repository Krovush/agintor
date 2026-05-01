from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from .....storage import state_store
from .....storage.artifacts import ArtifactMode, ArtifactPolicy
from .....providers import (
    ModelProvider,
    provider_environment_names_for_instance,
    provider_payload,
    provider_payload_file_paths,
    rewrite_provider_payload_file_paths,
)
from ....loader import resolve_docker_launch_policy
from ....profile import RuntimeProfile
from ....sdk import KERNEL_BUNDLE_DIR
from .....storage.run_store import RunStore
from .....contracts import (
    AsyncHandle,
    AttemptManifest,
    BenchmarkTask,
    CapabilityExchange,
    CheckpointEnvelope,
    CheckpointReference,
    InspectRequest,
    OpenAITraceContext,
    OpenHandleTableSnapshot,
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
    ShellStateSnapshot,
    SideEffectReceipt,
)
from ....api import compile_request_file_ref, normalize_benchmark_request_id
from .....utils import ensure_directory, file_digest, stable_hash

from .checkpoint_rewrite import DockerCheckpointRewriteMixin
from .commands import DockerCommandMixin
from .image import (
    DockerImageMixin,
    _default_repo_root,
)
from .path_mapping import DockerPathMappingMixin
from .request_rewrite import DockerRequestRewriteMixin
from .response_rewrite import DockerResponseRewriteMixin
from .run_rewrite import DockerRunRewriteMixin

class DockerRuntimeExecutor(DockerResponseRewriteMixin, DockerRunRewriteMixin, DockerCheckpointRewriteMixin, DockerRequestRewriteMixin, DockerPathMappingMixin, DockerCommandMixin, DockerImageMixin):
    RUNS_MOUNT_ROOT = "/mnt/runs"
    REQUEST_FILES_MOUNT_ROOT = "/mnt/request-files"
    PATH_PAYLOAD_KEYS = frozenset(
        {
            "artifact_path",
            "artifact_ref",
            "checkpoint_ref",
            "failed_path",
            "file_path",
            "input_path",
            "latest_checkpoint_ref",
            "materialization_state_ref",
            "path",
            "payload_ref",
            "run_root",
            "runtime_dir",
            "selected_checkpoint_ref",
            "source_checkpoint_ref",
            "stderr_path",
            "stdout_path",
            "target_path",
            "trace_path",
            "trace_ref",
            "working_directory",
            "workspace_root",
        }
    )
    PATH_LIST_PAYLOAD_KEYS = frozenset(
        {
            "artifact_refs",
            "file_paths",
            "selected_checkpoint_refs",
            "target_file_paths",
        }
    )

    def __init__(
        self,
        workspace: Path,
        repo_root: Path | None = None,
        image_name_prefix: str = "agintor-runtime",
        base_image: str = "python:3.12-slim",
        artifact_mode: str | ArtifactMode | None = ArtifactMode.ALWAYS,
        sandbox_root: Path | None = None,
        run_store_workspace: Path | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.run_store_workspace = Path(run_store_workspace) if run_store_workspace is not None else self.workspace
        self.repo_root = repo_root or _default_repo_root()
        self.image_name_prefix = image_name_prefix
        self.base_image = base_image
        self.artifact_policy = ArtifactPolicy.resolve(
            artifact_mode=artifact_mode,
            sandbox_root=sandbox_root,
        )
        self.retain_artifacts = self.artifact_policy.keep_successes
        self._cached_source_digest = self._compute_source_digest()
        self.image_tag = f"{self.image_name_prefix}:{self._cached_source_digest[:12]}"

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
        request = self._resolve_resume_checkpoint_request(request)
        resume_request_file_refs = self._checkpoint_request_file_refs(request.checkpoint_ref)
        container_request, checkpoint_store_dir, run_mount_root = self._container_resume_request(
            request,
            runtime_path=runtime_path,
        )
        container_request, checkpoint_mount_dir, checkpoint_rewrite_dir = self._materialize_container_resume_checkpoint(
            container_request,
            request,
            run_dir=run_dir,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_store_dir,
        )
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
        if checkpoint_mount_dir is not None:
            mounts.append(f"{checkpoint_mount_dir.resolve()}:/mnt/checkpoints:ro")
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
            checkpoint_rewrite_dir,
            runtime_path=runtime_path,
            run_mount_root=run_mount_root,
            request_file_reverse_map=request_file_reverse_map,
        )
        self._rewrite_durable_run_paths(
            response.solve_result.run_root or request.run_root,
            runtime_path=runtime_path,
            run_mount_root=run_mount_root,
            checkpoint_store_dir=checkpoint_rewrite_dir,
            request_file_reverse_map=request_file_reverse_map,
        )
        failed = response.solve_result.status in {"failed", "controlled_failure"} or bool(response.solve_result.faults.get("hard_invalid"))
        self._cleanup_run_dir(run_dir, failed=failed)
        return response
