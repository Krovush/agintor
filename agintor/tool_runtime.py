from __future__ import annotations

import ast
import importlib.util
import json
import math
import os
import statistics
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from .exceptions import SafetyViolation, ValidationError
from .pydantic_compat import model_copy
from .schemas import AsyncHandle, ToolExecutionResult, ToolSpec
from .utils import ensure_directory, file_digest, now_ts, stable_hash


SAFE_AST_NODES = {
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Mod,
    ast.Pow,
    ast.Load,
    ast.Name,
    ast.Constant,
    ast.Call,
    ast.List,
    ast.Tuple,
    ast.GeneratorExp,
    ast.comprehension,
    ast.Store,
    ast.Compare,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
}
SAFE_GLOBALS = {"sum": sum, "min": min, "max": max, "abs": abs, "round": round, "math": math}


@dataclass
class RegisteredTool:
    spec: ToolSpec
    executor: Callable[..., Any] | None = None
    historical_passes: int = 0
    historical_runs: int = 0
    distinct_tasks: set[str] = field(default_factory=set)
    sandbox_hash: str | None = None

    @property
    def category_key(self) -> str:
        return "/".join(self.spec.category_path)

    @property
    def pass_rate(self) -> float:
        if self.historical_runs <= 0:
            return 0.0
        return self.historical_passes / self.historical_runs


class SandboxManager:
    def __init__(self, root: Path) -> None:
        self.root = ensure_directory(root)
        self._manifests: dict[str, Path] = {}

    def sandbox_hash(self, spec: ToolSpec, base_image_digest: str = "python-3.11", compiler_flags: str = "", mount_spec: str = "ro", test_digest: str | None = None) -> str:
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
        sandbox_dir = ensure_directory(self.root / sandbox_hash)
        manifest = sandbox_dir / "manifest.json"
        if not manifest.exists():
            manifest.write_text(json.dumps({"tool": spec.name, "hash": sandbox_hash}, indent=2), encoding="utf-8")
        self._manifests[sandbox_hash] = manifest
        return sandbox_dir


class SafetyGuard:
    FORBIDDEN_PERMISSIONS = {"network", "filesystem:write:/", "filesystem:write:/etc", "shell:unsafe"}

    def validate_permissions(self, permissions: Iterable[str]) -> None:
        perms = set(permissions)
        forbidden = self.FORBIDDEN_PERMISSIONS & perms
        if forbidden:
            raise SafetyViolation(f"forbidden permissions requested: {sorted(forbidden)}")

    def validate_generated_source(self, source: str) -> None:
        if "import socket" in source or "requests" in source or "urllib" in source:
            raise SafetyViolation("forbidden network access in generated source")

    def validate_expression(self, expression: str) -> None:
        tree = ast.parse(expression, mode="eval")
        for node in ast.walk(tree):
            if type(node) not in SAFE_AST_NODES:
                raise SafetyViolation(f"forbidden AST node in expression: {type(node).__name__}")


class ToolRegistry:
    def __init__(self, sandbox_manager: SandboxManager, safety_guard: SafetyGuard) -> None:
        self.sandbox_manager = sandbox_manager
        self.safety_guard = safety_guard
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
                build_cmd="python -m py_compile tool.py",
                run_cmd="python tool.py",
                timeout_s=10,
                determinism_class="stable",
            )
            self._tools[name] = RegisteredTool(spec=spec, executor=executor, sandbox_hash=self.sandbox_manager.sandbox_hash(spec))
            category_key = "/".join(category)
            self._category_summaries.setdefault(category_key, description)

        register("math/basic/sum_numbers", ["math", "basic"], "Aggregate numbers by sum", lambda numbers: sum(numbers), "(numbers: list[float]) -> float")
        register("math/basic/product_numbers", ["math", "basic"], "Aggregate numbers by product", lambda numbers: math.prod(numbers), "(numbers: list[float]) -> float")
        register("math/basic/max_number", ["math", "basic"], "Return max number", lambda numbers: max(numbers), "(numbers: list[float]) -> float")
        register("math/basic/min_number", ["math", "basic"], "Return min number", lambda numbers: min(numbers), "(numbers: list[float]) -> float")
        register("math/basic/median_number", ["math", "basic"], "Return median number", lambda numbers: statistics.median(numbers), "(numbers: list[float]) -> float")
        register("data/csv/column_sum", ["data", "csv"], "Sum a numeric column across rows", lambda rows, column: sum(float(row[column]) for row in rows), "(rows: list[dict], column: str) -> float")
        register("data/csv/column_max", ["data", "csv"], "Max a numeric column across rows", lambda rows, column: max(float(row[column]) for row in rows), "(rows: list[dict], column: str) -> float")

    @property
    def tools(self) -> dict[str, RegisteredTool]:
        return self._tools

    @property
    def category_summaries(self) -> dict[str, str]:
        return self._category_summaries

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

    def register_generated_tool(self, spec: ToolSpec, source: str, executor: Callable[..., Any] | None = None) -> RegisteredTool:
        self.safety_guard.validate_permissions(spec.permissions)
        self.safety_guard.validate_generated_source(source)
        sandbox_dir = self.sandbox_manager.ensure_environment(spec)
        tool_file = sandbox_dir / f"{spec.name.replace('/', '_')}.py"
        tool_file.write_text(source, encoding="utf-8")
        spec = model_copy(spec, update={"source_digest": file_digest(tool_file)})
        registered = RegisteredTool(spec=spec, executor=executor, sandbox_hash=self.sandbox_manager.sandbox_hash(spec))
        self._tools[spec.name] = registered
        self._category_summaries.setdefault(registered.category_key, spec.description)
        return registered


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, sandbox_manager: SandboxManager) -> None:
        self.registry = registry
        self.sandbox_manager = sandbox_manager

    def run_tool(self, tool_name: str, args: Mapping[str, Any], task_id: str) -> ToolExecutionResult:
        start = time.perf_counter()
        tool = self.registry.get(tool_name)
        tool.historical_runs += 1
        tool.distinct_tasks.add(task_id)
        try:
            if tool.executor is not None:
                output = tool.executor(**args)
            else:
                output = self._run_python_tool(tool, args)
            tool.historical_passes += 1
            return ToolExecutionResult(tool_name=tool_name, output=output, latency_s=time.perf_counter() - start, success=True)
        except Exception as exc:
            return ToolExecutionResult(tool_name=tool_name, output=None, stderr=str(exc), latency_s=time.perf_counter() - start, success=False)

    def _run_python_tool(self, tool: RegisteredTool, args: Mapping[str, Any]) -> Any:
        sandbox_dir = self.sandbox_manager.ensure_environment(tool.spec)
        tool_file = next(sandbox_dir.glob(f"{tool.spec.name.replace('/', '_')}*.py"), None)
        if tool_file is None:
            raise FileNotFoundError(f"tool source missing for {tool.spec.name}")
        namespace: Dict[str, Any] = {}
        exec(tool_file.read_text(encoding="utf-8"), namespace, namespace)
        if "run" not in namespace:
            raise ValidationError("generated tool source missing run()")
        return namespace["run"](**dict(args))

    def launch_async(self, tool_name: str, args: Mapping[str, Any], workspace: Path) -> AsyncHandle:
        tool = self.registry.get(tool_name)
        sandbox_dir = self.sandbox_manager.ensure_environment(tool.spec)
        tool_file = next(sandbox_dir.glob(f"{tool.spec.name.replace('/', '_')}*.py"), None)
        if tool_file is None:
            raise FileNotFoundError(f"tool source missing for {tool.spec.name}")
        ensure_directory(workspace)
        stdout_path = workspace / f"{tool.spec.name.replace('/', '_')}.stdout"
        stderr_path = workspace / f"{tool.spec.name.replace('/', '_')}.stderr"
        args_path = workspace / f"{tool.spec.name.replace('/', '_')}.args.json"
        args_path.write_text(json.dumps(args), encoding="utf-8")
        program = textwrap.dedent(
            f"""
            import json
            namespace = {{}}
            exec(open({repr(str(tool_file))}, 'r', encoding='utf-8').read(), namespace, namespace)
            args = json.load(open({repr(str(args_path))}, 'r', encoding='utf-8'))
            output = namespace['run'](**args)
            print(json.dumps(output))
            """
        )
        stdout_handle = open(stdout_path, "w", encoding="utf-8")
        stderr_handle = open(stderr_path, "w", encoding="utf-8")
        process = subprocess.Popen([sys.executable, "-c", program], stdout=stdout_handle, stderr=stderr_handle)
        stdout_handle.close()
        stderr_handle.close()
        return AsyncHandle(
            handle_id=stable_hash(tool_name, now_ts())[:16],
            tool_name=tool_name,
            sandbox_hash=tool.sandbox_hash or self.sandbox_manager.sandbox_hash(tool.spec),
            working_directory=str(workspace),
            launch_time=now_ts(),
            timeout=tool.spec.timeout_s,
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
            state="running",
            artifact_refs=[],
            process_pid=process.pid,
        )



def validate_expression_tool(expression: str, tests: list[dict[str, Any]], safety_guard: SafetyGuard) -> tuple[str, Callable[..., Any]]:
    safety_guard.validate_expression(expression)
    source = textwrap.dedent(
        f"""
        import math

        def run(**kwargs):
            return eval({expression!r}, {{'sum': sum, 'min': min, 'max': max, 'abs': abs, 'round': round, 'math': math}}, kwargs)
        """
    )
    namespace: Dict[str, Any] = {}
    exec(source, namespace, namespace)
    fn = namespace["run"]
    for test in tests:
        expected = test.get("expected")
        result = fn(**test.get("input", {}))
        if result != expected:
            raise ValidationError(f"generated expression tool failed test {test}")
    return source, fn
