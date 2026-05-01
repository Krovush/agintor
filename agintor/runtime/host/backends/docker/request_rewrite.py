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

from .path_mapping import DockerPathMappingMixin

class DockerRequestRewriteMixin:
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
    def _containerize_resume_trace_context(
        cls,
        trace_context: OpenAITraceContext | None,
        *,
        runtime_path: Path | None,
    ) -> OpenAITraceContext | None:
        if runtime_path is None:
            return trace_context
        payload = trace_context.model_dump() if trace_context is not None else {}
        payload["runtime_dir"] = "/mnt/runtime"
        return OpenAITraceContext.model_validate(payload)

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
        payload["context_items"] = cls._rewrite_structured_path_payload(
            payload.get("context_items", []),
            forward_map,
        )
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
        request_file_refs = [compile_request_file_ref(path) for path in absolute_paths]
        updated_refs, mounts, forward_map, reverse_map = cls._containerize_request_file_refs(
            request_file_refs,
            run_mount_root=run_mount_root,
        )
        payload = (task).model_dump()
        payload["file_paths"] = [forward_map.get(str(path), str(path)) for path in task.file_paths]
        payload["context_items"] = cls._rewrite_structured_path_payload(
            payload.get("context_items", []),
            forward_map,
        )
        payload["operations"] = [
            {
                **dict(operation_payload),
                "args": cls._rewrite_structured_path_payload(
                    dict(operation_payload).get("args", {}),
                    forward_map,
                ),
            }
            for operation_payload in payload.get("operations", [])
            if isinstance(operation_payload, Mapping)
        ]
        payload["metadata"] = dict(payload.get("metadata") or {})
        payload["metadata"]["request_file_refs"] = [(file_ref).model_dump() for file_ref in updated_refs]
        payload["metadata"]["input_binding_overrides"] = cls._containerize_input_binding_overrides(
            payload["metadata"].get("input_binding_overrides", {}),
            forward_map,
        )
        return (BenchmarkTask).model_validate(payload), mounts, reverse_map

    @classmethod
    def _containerize_input_binding_overrides(
        cls,
        input_binding_overrides: Any,
        forward_map: Mapping[str, str],
    ) -> Any:
        if not isinstance(input_binding_overrides, Mapping):
            return input_binding_overrides
        rewritten: dict[str, Any] = {}
        for node_id, bindings in input_binding_overrides.items():
            if not isinstance(bindings, list):
                rewritten[str(node_id)] = bindings
                continue
            rewritten_bindings: list[Any] = []
            for binding in bindings:
                if not isinstance(binding, Mapping):
                    rewritten_bindings.append(binding)
                    continue
                binding_payload = dict(binding)
                if str(binding_payload.get("source_kind") or "") == "request_file":
                    binding_payload["source_ref"] = cls._rewrite_path_value(
                        binding_payload.get("source_ref"),
                        forward_map,
                    )
                rewritten_bindings.append(binding_payload)
            rewritten[str(node_id)] = rewritten_bindings
        return rewritten

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
