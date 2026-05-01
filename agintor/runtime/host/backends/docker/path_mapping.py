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

class DockerPathMappingMixin:
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

    @staticmethod
    def _join_replacement_path(target: str, relative: str) -> str:
        target_text = str(target or "").rstrip("/\\")
        relative_text = str(relative or "").replace("\\", "/").strip("/")
        if not relative_text:
            return target_text
        if target_text.startswith("/") and not (len(target_text) >= 2 and target_text[1] == ":"):
            return f"{target_text}/{relative_text}"
        return str((Path(target_text) / Path(*relative_text.split("/"))).resolve())

    @staticmethod
    def _rewrite_path_string(path_text: str, replacements: Mapping[str, str]) -> str:
        rewritten = replacements.get(path_text, path_text)
        if rewritten != path_text:
            return rewritten
        for source, target in replacements.items():
            source_text = str(source or "").rstrip("/\\")
            if not source_text:
                continue
            for separator in ("/", "\\"):
                prefix = f"{source_text}{separator}"
                if path_text.startswith(prefix):
                    relative = path_text[len(prefix):]
                    return DockerPathMappingMixin._join_replacement_path(str(target), relative)
        return path_text

    @classmethod
    def _rewrite_path_value(cls, value: Any, replacements: Mapping[str, str]) -> Any:
        if isinstance(value, str):
            return cls._rewrite_path_string(value, replacements)
        if isinstance(value, list):
            return [cls._rewrite_path_value(item, replacements) for item in value]
        return value

    @classmethod
    def _rewrite_structured_path_payload(cls, payload: Any, replacements: Mapping[str, str]) -> Any:
        if isinstance(payload, Mapping):
            rewritten: dict[str, Any] = {}
            for key, value in payload.items():
                key_text = str(key)
                if key_text in cls.PATH_PAYLOAD_KEYS or key_text in cls.PATH_LIST_PAYLOAD_KEYS:
                    rewritten[key_text] = cls._rewrite_path_value(value, replacements)
                else:
                    rewritten[key_text] = cls._rewrite_structured_path_payload(value, replacements)
            return rewritten
        if isinstance(payload, list):
            return [cls._rewrite_structured_path_payload(item, replacements) for item in payload]
        return payload

    @classmethod
    def _rewrite_trace_context_paths(cls, trace_context: Any, replacements: Mapping[str, str]) -> Any:
        if trace_context is None or not hasattr(trace_context, "model_dump"):
            return trace_context
        payload = trace_context.model_dump()
        payload["runtime_dir"] = cls._rewrite_path_value(payload.get("runtime_dir"), replacements)
        return type(trace_context).model_validate(payload)

    @classmethod
    def _rewrite_inline_trace_ref(cls, trace_ref: str | None, replacements: Mapping[str, str]) -> str | None:
        if not trace_ref or not str(trace_ref).startswith(RunResult._inline_trace_prefix()):
            return trace_ref
        trace_rows = RunResult.decode_trace_ref(str(trace_ref))
        if not trace_rows:
            return trace_ref
        rewritten = cls._rewrite_structured_path_payload(trace_rows, replacements)
        return RunResult.encode_trace_ref(rewritten)

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
    def _container_known_path(
        cls,
        path_text: str | None,
        *,
        run_mount_root: Path | None = None,
        checkpoint_store_dir: Path | None = None,
    ) -> str | None:
        rewritten = cls._container_run_path(path_text, run_mount_root)
        if rewritten != path_text:
            return rewritten
        return cls._container_checkpoint_path(rewritten, checkpoint_store_dir)
