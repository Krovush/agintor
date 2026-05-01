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

@dataclass
class RegisteredTool:
    spec: ToolSpec
    executor: Callable[..., Any] | None = None
    historical_passes: int = 0
    historical_runs: int = 0
    distinct_tasks: set[str] = field(default_factory=set)
    sandbox_hash: str | None = None
    safety_validated: bool = False

    @property
    def category_key(self) -> str:
        return "/".join(self.spec.category_path)

    @property
    def pass_rate(self) -> float:
        if self.historical_runs <= 0:
            return 0.0
        return self.historical_passes / self.historical_runs

    @property
    def cache_hit(self) -> float:
        return 1.0 if self.sandbox_hash else 0.0


@dataclass
class _AsyncProcessRecord:
    process: subprocess.Popen[Any]
    state: dict[str, Any]
