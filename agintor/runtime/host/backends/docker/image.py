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

def _default_repo_root() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "pyproject.toml").is_file() and (parent / "agintor").is_dir():
            return parent
    raise RuntimeError(f"could not resolve Agintor repo root from {current}")


class DockerImageMixin:
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
