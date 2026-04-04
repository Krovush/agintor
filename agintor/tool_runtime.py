from __future__ import annotations

import ast
import contextlib
import io
import json
import math
import shutil
import statistics
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from .artifacts import ArtifactPolicy
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
SAFE_TOOL_IMPORTS = {"collections", "decimal", "fractions", "functools", "itertools", "json", "math", "statistics"}
FORBIDDEN_TOOL_IMPORT_PREFIXES = ("builtins", "http", "os", "pathlib", "requests", "shutil", "socket", "subprocess", "tempfile", "urllib")
FORBIDDEN_TOOL_CALLS = {"__import__", "input", "open"}


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


class SandboxManager:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
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
        self.validate_tool_module(ast.parse(source))

    def validate_expression(self, expression: str) -> None:
        tree = ast.parse(expression, mode="eval")
        for node in ast.walk(tree):
            if type(node) not in SAFE_AST_NODES:
                raise SafetyViolation(f"forbidden AST node in expression: {type(node).__name__}")

    def validate_tool_module(self, tree: ast.Module) -> None:
        for node in tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                continue
            if not isinstance(node, (ast.FunctionDef, ast.Import, ast.ImportFrom)):
                raise SafetyViolation("generated tool source contains top-level executable statements")
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self._validate_import_name(alias.name)
            elif isinstance(node, ast.ImportFrom):
                self._validate_import_name(node.module or "")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_TOOL_CALLS:
                raise SafetyViolation(f"forbidden builtin call in generated source: {node.func.id}")

    def _validate_import_name(self, module_name: str) -> None:
        normalized = module_name.strip()
        if not normalized:
            return
        if any(normalized == prefix or normalized.startswith(f"{prefix}.") for prefix in FORBIDDEN_TOOL_IMPORT_PREFIXES):
            raise SafetyViolation(f"forbidden import in generated source: {normalized}")
        root = normalized.split(".", 1)[0]
        if root not in SAFE_TOOL_IMPORTS:
            raise SafetyViolation(f"unsupported import in generated source: {normalized}")


def _tool_filename(spec: ToolSpec) -> str:
    digest = stable_hash(spec.source_digest, spec.runtime, spec.signature)[:16]
    return f"generated_{digest}.py"


def _async_artifact_stem(tool_name: str, handle_id: str) -> str:
    slug = tool_name.replace("/", "_").strip("_") or "tool"
    return f"{slug[:12]}_{handle_id[:8]}"


def _validation_temp_base() -> Path:
    return ensure_directory(ArtifactPolicy.resolve().tool_validation_root)


def _materialize_generated_tool(spec: ToolSpec, source: str, sandbox_manager: SandboxManager) -> tuple[ToolSpec, Path]:
    staged_spec = model_copy(spec, update={"source_digest": stable_hash(source)})
    staged_dir = sandbox_manager.ensure_environment(staged_spec)
    staged_file = staged_dir / _tool_filename(staged_spec)
    staged_file.write_text(source, encoding="utf-8")
    finalized_spec = model_copy(staged_spec, update={"source_digest": file_digest(staged_file)})
    finalized_dir = sandbox_manager.ensure_environment(finalized_spec)
    finalized_file = finalized_dir / _tool_filename(finalized_spec)
    if not finalized_file.exists():
        finalized_file.write_text(source, encoding="utf-8")
    return finalized_spec, finalized_file.resolve()


def _run_validation_trial(tool_file: Path, args: Mapping[str, Any], timeout_s: float) -> Any:
    payload = json.dumps(dict(args), sort_keys=True)
    program = textwrap.dedent(
        f"""
        import json
        namespace = {{}}
        exec(open({repr(str(tool_file))}, 'r', encoding='utf-8').read(), namespace, namespace)
        output = namespace['run'](**json.loads({payload!r}))
        print(json.dumps(output))
        """
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", program],
            cwd=str(tool_file.parent),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValidationError(f"generated tool validation timed out after {timeout_s}s") from exc
    stderr = completed.stderr.strip()
    if completed.returncode != 0:
        detail = stderr or f"process exited with code {completed.returncode}"
        raise ValidationError(f"generated tool failed validation run: {detail}")
    stdout = completed.stdout.strip()
    try:
        return json.loads(stdout or "null")
    except Exception as exc:
        raise ValidationError(f"generated tool produced non-JSON output during validation: {stdout}") from exc


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


@dataclass
class _AsyncProcessRecord:
    process: subprocess.Popen[Any]
    state: dict[str, Any]


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, sandbox_manager: SandboxManager, persist_artifacts: bool = False) -> None:
        self.registry = registry
        self.sandbox_manager = sandbox_manager
        self.persist_artifacts = persist_artifacts
        self._async_processes: dict[str, _AsyncProcessRecord] = {}
        self._async_launch_counter = 0

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
        tool_file = sandbox_dir / _tool_filename(tool.spec)
        if not tool_file.exists():
            raise FileNotFoundError(f"tool source missing for {tool.spec.name}")
        namespace: Dict[str, Any] = {}
        exec(tool_file.read_text(encoding="utf-8"), namespace, namespace)
        if "run" not in namespace:
            raise ValidationError("generated tool source missing run()")
        return namespace["run"](**dict(args))

    def _artifact_paths(self, workspace: Path, artifact_stem: str) -> tuple[Path | None, Path | None, Path | None]:
        if not self.persist_artifacts:
            return None, None, None
        ensure_directory(workspace)
        return (
            workspace / f"{artifact_stem}.stdout",
            workspace / f"{artifact_stem}.stderr",
            workspace / f"{artifact_stem}.result.json",
        )

    def _write_async_artifacts(
        self,
        stdout_path: Path | None,
        stderr_path: Path | None,
        result_path: Path | None,
        *,
        stdout: str,
        stderr: str,
        output: Any,
    ) -> list[str]:
        refs: list[str] = []
        if stdout_path is not None:
            stdout_path.write_text(stdout, encoding="utf-8")
            refs.append(str(stdout_path))
        if stderr_path is not None:
            stderr_path.write_text(stderr, encoding="utf-8")
            refs.append(str(stderr_path))
        if result_path is not None and output is not None:
            result_path.write_text(json.dumps(output), encoding="utf-8")
            refs.append(str(result_path))
        return refs

    def launch_async(self, tool_name: str, args: Mapping[str, Any], workspace: Path, task_id: str) -> AsyncHandle:
        tool = self.registry.get(tool_name)
        tool.historical_runs += 1
        tool.distinct_tasks.add(task_id)
        sandbox_dir = self.sandbox_manager.ensure_environment(tool.spec)
        tool_file = sandbox_dir / _tool_filename(tool.spec)
        handle_workspace = Path(workspace)
        working_directory = handle_workspace if self.persist_artifacts else tool_file.parent
        if self.persist_artifacts:
            ensure_directory(handle_workspace)
        self._async_launch_counter += 1
        handle_id = stable_hash(tool_name, args, self._async_launch_counter)[:16]
        artifact_stem = _async_artifact_stem(tool_name, handle_id)
        stdout_path, stderr_path, result_path = self._artifact_paths(handle_workspace, artifact_stem)
        process_pid: int | None = None
        if not tool_file.exists():
            if tool.executor is not None:
                raise ValidationError(
                    "executor-backed async tools must be materialized into source before background execution"
                )
            raise FileNotFoundError(f"tool source missing for {tool.spec.name}")
        payload = json.dumps(dict(args), sort_keys=True)
        program = textwrap.dedent(
            f"""
            import json
            namespace = {{}}
            exec(open({repr(str(tool_file))}, 'r', encoding='utf-8').read(), namespace, namespace)
            output = namespace['run'](**json.loads({payload!r}))
            print(json.dumps(output))
            """
        )
        process = subprocess.Popen(
            [sys.executable, "-c", program],
            cwd=str(tool_file.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        process_pid = process.pid
        self._async_processes[handle_id] = _AsyncProcessRecord(
            process=process,
            state={"stdout": "", "stderr": "", "output": None, "artifact_refs": []},
        )
        handle = AsyncHandle(
            handle_id=handle_id,
            tool_name=tool_name,
            sandbox_hash=tool.sandbox_hash or self.sandbox_manager.sandbox_hash(tool.spec),
            working_directory=str(working_directory),
            launch_time=now_ts(),
            timeout=tool.spec.timeout_s,
            stdout_path=str(stdout_path) if stdout_path is not None else None,
            stderr_path=str(stderr_path) if stderr_path is not None else None,
            state="running",
            artifact_refs=[path for path in [str(result_path) if result_path is not None else None] if path],
            process_pid=process_pid,
        )
        return handle

    def wait_async(self, handle: AsyncHandle, poll_interval_s: float = 0.01) -> ToolExecutionResult:
        start = time.perf_counter()
        record = self._async_processes.get(handle.handle_id)
        if record is None:
            return ToolExecutionResult(
                tool_name=handle.tool_name,
                output=None,
                stderr="async process handle missing",
                latency_s=time.perf_counter() - start,
                success=False,
                async_handle_id=handle.handle_id,
            )
        process = record.process
        return_code = 0
        try:
            try:
                stdout, stderr = process.communicate(timeout=handle.timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
                return ToolExecutionResult(
                    tool_name=handle.tool_name,
                    output=None,
                    stdout=stdout or "",
                    stderr=(stderr or f"async tool timed out after {handle.timeout}s").strip(),
                    latency_s=time.perf_counter() - start,
                    success=False,
                    async_handle_id=handle.handle_id,
                )
        finally:
            self._async_processes.pop(handle.handle_id, None)
        record.state["stdout"] = stdout or ""
        record.state["stderr"] = stderr or ""
        return_code = process.returncode
        stdout = str(record.state.get("stdout", ""))
        stderr = str(record.state.get("stderr", ""))
        output = record.state.get("output")
        success = False
        try:
            output = json.loads(stdout or "null")
            success = return_code == 0 and not bool(stderr.strip())
            if return_code != 0 and not stderr.strip():
                stderr = f"process exited with code {return_code}"
                success = False
        except Exception as exc:
            output = None
            stderr = f"{stderr}\n{exc}".strip()
            success = False
        record.state["output"] = output
        record.state["artifact_refs"] = self._write_async_artifacts(
            Path(handle.stdout_path) if handle.stdout_path else None,
            Path(handle.stderr_path) if handle.stderr_path else None,
            Path(handle.artifact_refs[0]) if handle.artifact_refs else None,
            stdout=stdout,
            stderr=stderr,
            output=output,
        )
        if return_code not in (None, 0):
            message = f"process exited with code {return_code}"
            if message not in stderr:
                stderr = f"{stderr}\n{message}".strip()
            success = False
        if success:
            self.registry.get(handle.tool_name).historical_passes += 1
        return ToolExecutionResult(
            tool_name=handle.tool_name,
            output=output,
            stdout=stdout,
            stderr=stderr,
            latency_s=time.perf_counter() - start,
            success=success,
            async_handle_id=handle.handle_id,
        )

    def await_handle(self, handle_id: str, handle_table: Any) -> dict[str, Any]:
        handle = handle_table.get(handle_id)
        result = self.wait_async(handle)
        handle_table.update_state(handle_id, "completed" if result.success else "failed")
        return {
            "handle_id": handle_id,
            "state": "completed" if result.success else "failed",
            "latency_s": result.latency_s,
            "output": result.output,
            "stderr": result.stderr,
        }



def validate_expression_tool(expression: str, tests: list[dict[str, Any]], safety_guard: SafetyGuard) -> tuple[str, Callable[..., Any]]:
    safety_guard.validate_expression(expression)
    tree = ast.parse(expression, mode="eval")
    bound_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.comprehension):
            targets = [node.target]
            while targets:
                target = targets.pop()
                if isinstance(target, ast.Name):
                    bound_names.add(target.id)
                elif isinstance(target, (ast.Tuple, ast.List)):
                    targets.extend(list(target.elts))
    free_names = sorted(
        {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id not in bound_names
        }
    )
    required_names = [name for name in free_names if name not in SAFE_GLOBALS]
    shadowable_safe_names = [name for name in free_names if name in SAFE_GLOBALS]
    params = required_names + [f"{name}={name}" for name in shadowable_safe_names]
    run_signature = ", ".join(params)
    source = "import math\n\n\n"
    if run_signature:
        source += f"def run({run_signature}):\n"
    else:
        source += "def run():\n"
    source += f"    return {expression}\n"
    namespace: Dict[str, Any] = {}
    exec(source, namespace, namespace)
    fn = namespace["run"]
    for test in tests:
        expected = test.get("expected")
        result = fn(**test.get("input", {}))
        if result != expected:
            raise ValidationError(f"generated expression tool failed test {test}")
    return source, fn


def _extract_run_function(tree: ast.Module) -> ast.FunctionDef | ast.AsyncFunctionDef:
    run_functions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run"
    ]
    if len(run_functions) != 1:
        raise ValidationError("generated tool source must define exactly one top-level run()")
    return run_functions[0]


def _lint_tool_module(tree: ast.Module) -> None:
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    raise ValidationError("generated tool source may not use wildcard imports")
    _extract_run_function(tree)


def _signature_arg_names(signature: str) -> list[str]:
    stripped = signature.strip()
    if "(" not in stripped or ")" not in stripped:
        raise ValidationError(f"invalid tool signature: {signature}")
    start = stripped.find("(")
    end = stripped.find(")", start)
    payload = stripped[start + 1 : end].strip()
    if not payload:
        return []
    names: list[str] = []
    for raw_part in payload.split(","):
        part = raw_part.strip()
        if not part:
            continue
        if ":" in part:
            part = part.split(":", 1)[0].strip()
        if "=" in part:
            part = part.split("=", 1)[0].strip()
        names.append(part)
    return names


def _runtime_arg_names(run_function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    if run_function.args.posonlyargs or run_function.args.vararg or run_function.args.kwarg:
        raise ValidationError("generated tool run() must use an explicit positional interface")
    return [arg.arg for arg in run_function.args.args]


def _check_signature(spec: ToolSpec, tree: ast.Module) -> None:
    expected_args = _signature_arg_names(spec.signature)
    actual_args = _runtime_arg_names(_extract_run_function(tree))
    if actual_args != expected_args:
        raise ValidationError(f"generated tool signature mismatch: expected {expected_args}, got {actual_args}")


def _check_import_resolution(tool_file: Path, timeout_s: float) -> None:
    program = "import runpy, sys; runpy.run_path(sys.argv[1], run_name='__tool__')"
    completed = subprocess.run(
        [sys.executable, "-c", program, str(tool_file)],
        cwd=str(tool_file.parent),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ValidationError(f"generated tool import resolution failed: {detail}")


def validate_tool_candidate(
    spec: ToolSpec,
    source: str,
    safety_guard: SafetyGuard,
    sandbox_manager: SandboxManager | None = None,
) -> dict[str, Any]:
    safety_guard.validate_permissions(spec.permissions)
    safety_guard.validate_generated_source(source)
    tree = ast.parse(source)
    _lint_tool_module(tree)
    _check_signature(spec, tree)
    compile(source, f"<{spec.name}>", "exec")
    temp_root: Path | None = None
    if sandbox_manager is None:
        # Avoid TemporaryDirectory here: on Windows, nested sandbox mkdir calls can fail
        # beneath that root. Creating a normal directory keeps validation writable.
        temp_root = ensure_directory(_validation_temp_base() / f"agtv_{stable_hash(spec.name, source, time.time_ns())[:12]}")
        sandbox_manager = SandboxManager(temp_root)
    try:
        finalized_spec, tool_file = _materialize_generated_tool(spec, source, sandbox_manager)
        compile(tool_file.read_text(encoding="utf-8"), str(tool_file), "exec")
        _check_import_resolution(tool_file, finalized_spec.timeout_s)
        timeout_payload = dict(finalized_spec.tests[0].get("input", {})) if finalized_spec.tests else {}
        _run_validation_trial(tool_file, timeout_payload, finalized_spec.timeout_s)
        deterministic = True
        for test in finalized_spec.tests:
            payload = dict(test.get("input", {}))
            expected = test.get("expected")
            first = _run_validation_trial(tool_file, payload, finalized_spec.timeout_s)
            second = _run_validation_trial(tool_file, payload, finalized_spec.timeout_s)
            if first != expected:
                raise ValidationError(f"generated tool failed smoke test {test}")
            if first != second:
                deterministic = False
        return {
            "checked_tests": len(finalized_spec.tests),
            "deterministic": deterministic,
            "has_permissions": bool(finalized_spec.permissions),
            "valid": True,
            "lint_ok": True,
            "signature_ok": True,
            "import_resolution_ok": True,
            "permission_boundary_ok": True,
            "timeout_ok": True,
        }
    finally:
        if temp_root is not None:
            shutil.rmtree(temp_root, ignore_errors=True)
