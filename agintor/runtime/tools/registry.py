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

from .models import RegisteredTool
from .safety import SafetyGuard
from .sandbox import SandboxManager
from .validation import (
    _materialize_generated_tool,
    _resolve_runtime_file_path,
    _tool_filename,
    validate_tool_candidate,
)

class ToolRegistry:
    def __init__(self, sandbox_manager: SandboxManager, safety_guard: SafetyGuard, *, workspace_root: Path) -> None:
        self.sandbox_manager = sandbox_manager
        self.safety_guard = safety_guard
        self.workspace_root = Path(workspace_root).resolve()
        self._tools: dict[str, RegisteredTool] = {}
        self._category_summaries: dict[str, str] = {}
        self._register_builtin_tools()

    def _register_builtin_tools(self) -> None:
        def register(name: str, category: list[str], description: str, executor: Callable[..., Any], signature: str) -> None:
            spec = ToolSpec(
                name=name,
                category_path=category,
                signature=signature,
                description=description,
                runtime="python",
                deps=[],
                permissions=[],
                tests=[],
                backgroundable=False,
                state_schema={},
                source_digest=stable_hash(name, category, description, signature),
                build_cmd=f'"{sys.executable}" -m py_compile tool.py',
                run_cmd=f'"{sys.executable}" tool.py',
                timeout_s=10,
                determinism_class="stable",
            )
            self._tools[name] = RegisteredTool(
                spec=spec,
                executor=executor,
                sandbox_hash=self.sandbox_manager.sandbox_hash(spec),
                safety_validated=True,
            )
            category_key = "/".join(category)
            self._category_summaries.setdefault(category_key, description)

        register("math/basic/sum_numbers", ["math", "basic"], "Aggregate numbers by sum", lambda numbers: sum(numbers), "(numbers: list[float]) -> float")
        register("math/basic/product_numbers", ["math", "basic"], "Aggregate numbers by product", lambda numbers: math.prod(numbers), "(numbers: list[float]) -> float")
        register("math/basic/max_number", ["math", "basic"], "Return max number", lambda numbers: max(numbers), "(numbers: list[float]) -> float")
        register("math/basic/min_number", ["math", "basic"], "Return min number", lambda numbers: min(numbers), "(numbers: list[float]) -> float")
        register("math/basic/median_number", ["math", "basic"], "Return median number", lambda numbers: statistics.median(numbers), "(numbers: list[float]) -> float")
        register("data/csv/column_sum", ["data", "csv"], "Sum a numeric column across rows", lambda rows, column: sum(float(row[column]) for row in rows), "(rows: list[dict], column: str) -> float")
        register("data/csv/column_max", ["data", "csv"], "Max a numeric column across rows", lambda rows, column: max(float(row[column]) for row in rows), "(rows: list[dict], column: str) -> float")
        register(
            "filesystem/read_text_file",
            ["filesystem", "read"],
            "Read UTF-8 text content from a runtime-workspace-relative or explicit absolute file path",
            self._read_text_file,
            "(path: str) -> dict",
        )

    def _read_text_file(self, path: str) -> dict[str, Any]:
        resolved = _resolve_runtime_file_path(path, workspace_root=self.workspace_root)
        if not resolved.exists():
            return {
                "path": str(resolved),
                "content": "",
                "exists": False,
            }
        return {
            "path": str(resolved),
            "content": resolved.read_text(encoding="utf-8"),
            "exists": True,
        }

    @property
    def tools(self) -> dict[str, RegisteredTool]:
        return self._tools

    @property
    def category_summaries(self) -> dict[str, dict[str, Any]]:
        summaries: dict[str, dict[str, Any]] = {}
        for category_key, summary in self._category_summaries.items():
            tools = self.tools_in_category(category_key)
            runs = sum(tool.historical_runs for tool in tools)
            passes = sum(tool.historical_passes for tool in tools)
            histpass = passes / runs if runs > 0 else 0.0
            coldstarts = [0.05 if tool.spec.runtime == "python" else 0.20 for tool in tools] or [0.10]
            cache_hits = [tool.cache_hit for tool in tools]
            summaries[category_key] = {
                "summary": summary,
                "descendants": len(tools),
                "historical_pass_rate": histpass,
                "cache_hit": sum(cache_hits) / len(cache_hits) if cache_hits else 0.0,
                "coldstart": statistics.median(coldstarts),
                "permission_risk": max((1.0 if tool.spec.permissions else 0.0 for tool in tools), default=0.0),
            }
        return summaries

    def get(self, name: str) -> RegisteredTool:
        return self._tools[name]

    def categories(self) -> list[str]:
        return sorted(self._category_summaries)

    def tools_in_category(self, category_key: str) -> list[RegisteredTool]:
        return sorted([tool for tool in self._tools.values() if tool.category_key == category_key], key=lambda t: t.spec.name)


    def reset_task_local(self) -> None:
        removable = [name for name, tool in self._tools.items() if tool.spec.category_path[:2] == ["generated", "local"]]
        for name in removable:
            self._tools.pop(name, None)
        removable_categories = [
            category_key
            for category_key in list(self._category_summaries)
            if category_key.startswith("generated/local")
            and not any(tool.category_key == category_key for tool in self._tools.values())
        ]
        for category_key in removable_categories:
            self._category_summaries.pop(category_key, None)

    def snapshot_task_local(self) -> TaskLocalToolRegistrySnapshot:
        tool_snapshots: list[TaskLocalToolSnapshot] = []
        category_summaries: dict[str, str] = {}
        for tool in self._tools.values():
            if tool.spec.category_path[:2] != ["generated", "local"]:
                continue
            sandbox_dir = self.sandbox_manager.ensure_environment(tool.spec)
            tool_file = sandbox_dir / _tool_filename(tool.spec)
            source = tool_file.read_text(encoding="utf-8") if tool_file.exists() else ""
            tool_snapshots.append(
                TaskLocalToolSnapshot(
                    spec=(tool.spec).model_copy(deep=True),
                    source=source,
                    historical_passes=tool.historical_passes,
                    historical_runs=tool.historical_runs,
                    distinct_tasks=sorted(tool.distinct_tasks),
                    sandbox_hash=tool.sandbox_hash,
                    safety_validated=tool.safety_validated,
                )
            )
            category_summaries[tool.category_key] = self._category_summaries.get(tool.category_key, tool.spec.description)
        return TaskLocalToolRegistrySnapshot(
            tools=sorted(tool_snapshots, key=lambda item: item.spec.name),
            category_summaries=dict(sorted(category_summaries.items())),
        )

    def snapshot(self) -> TaskLocalToolRegistrySnapshot:
        return self.snapshot_task_local()

    def restore_task_local(self, snapshot: Mapping[str, Any] | TaskLocalToolRegistrySnapshot) -> None:
        registry_snapshot = (
            snapshot
            if isinstance(snapshot, TaskLocalToolRegistrySnapshot)
            else (TaskLocalToolRegistrySnapshot).model_validate(snapshot)
        )
        self.reset_task_local()
        for tool_snapshot in registry_snapshot.tools:
            registered = self.register_generated_tool(tool_snapshot.spec, tool_snapshot.source)
            registered.historical_passes = tool_snapshot.historical_passes
            registered.historical_runs = tool_snapshot.historical_runs
            registered.distinct_tasks = set(tool_snapshot.distinct_tasks)
            registered.sandbox_hash = tool_snapshot.sandbox_hash
            registered.safety_validated = tool_snapshot.safety_validated
            self._category_summaries[registered.category_key] = registry_snapshot.category_summaries.get(
                registered.category_key,
                registered.spec.description,
            )

    def restore(self, snapshot: Mapping[str, Any] | TaskLocalToolRegistrySnapshot) -> None:
        self.restore_task_local(snapshot)

    @classmethod
    def fork_from_snapshot(
        cls,
        snapshot: Mapping[str, Any] | TaskLocalToolRegistrySnapshot,
        *,
        sandbox_manager: SandboxManager,
        safety_guard: SafetyGuard,
        workspace_root: Path,
    ) -> "ToolRegistry":
        registry = cls(
            sandbox_manager,
            safety_guard,
            workspace_root=workspace_root,
        )
        registry.restore(snapshot)
        return registry

    def register_generated_tool(self, spec: ToolSpec, source: str, executor: Callable[..., Any] | None = None) -> RegisteredTool:
        self.safety_guard.validate_permissions(spec.permissions)
        self.safety_guard.validate_generated_source(source)
        spec, tool_file = _materialize_generated_tool(spec, source, self.sandbox_manager)
        registered = RegisteredTool(
            spec=spec,
            executor=executor,
            sandbox_hash=self.sandbox_manager.sandbox_hash(spec),
            safety_validated=True,
        )
        self._tools[spec.name] = registered
        self._category_summaries.setdefault(registered.category_key, spec.description)
        return registered
