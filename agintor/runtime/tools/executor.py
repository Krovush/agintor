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

from .execution import ToolExecutionMixin
from .models import _AsyncProcessRecord
from .registry import ToolRegistry
from .sandbox import SandboxManager

class ToolExecutor(ToolExecutionMixin):
    def __init__(self, registry: ToolRegistry, sandbox_manager: SandboxManager, persist_artifacts: bool = False) -> None:
        self.registry = registry
        self.sandbox_manager = sandbox_manager
        self.persist_artifacts = persist_artifacts
        self._async_processes: dict[str, _AsyncProcessRecord] = {}
        self._async_launch_counter = 0
