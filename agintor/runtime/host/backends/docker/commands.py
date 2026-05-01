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

class DockerCommandMixin:
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
