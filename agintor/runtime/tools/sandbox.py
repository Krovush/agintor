from __future__ import annotations

import ast
import contextlib
import io
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from ...storage.artifacts import ArtifactPolicy
from ...core.exceptions import SafetyViolation, ValidationError
from ...contracts import (
    AsyncHandle,
    TaskLocalToolRegistrySnapshot,
    TaskLocalToolSnapshot,
    ToolExecutionResult,
    ToolSpec,
)
from ...utils import ensure_directory, file_digest, now_ts, stable_hash

class SandboxManager:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._manifests: dict[str, Path] = {}

    def sandbox_hash(self, spec: ToolSpec, base_image_digest: str = "python-3.12", compiler_flags: str = "", mount_spec: str = "ro", test_digest: str | None = None) -> str:
        digest = stable_hash(
            spec.source_digest,
            spec.runtime,
            spec.deps,
            spec.permissions,
            base_image_digest,
            compiler_flags,
            mount_spec,
            test_digest or stable_hash(spec.tests),
        )
        return digest

    def ensure_environment(self, spec: ToolSpec) -> Path:
        sandbox_hash = self.sandbox_hash(spec)
        ensure_directory(self.root)
        suffix = 0
        while True:
            dir_name = stable_hash("sandbox_dir", sandbox_hash, suffix)[:16]
            sandbox_dir = ensure_directory(self.root / dir_name)
            manifest = sandbox_dir / "manifest.json"
            if not manifest.exists():
                manifest.write_text(json.dumps({"tool": spec.name, "hash": sandbox_hash}, indent=2), encoding="utf-8")
                break
            try:
                manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
            except Exception:
                manifest_payload = {}
            if manifest_payload.get("hash") == sandbox_hash:
                break
            suffix += 1
        self._manifests[sandbox_hash] = manifest
        return sandbox_dir
