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
from .safety import (
    SAFE_GLOBALS,
    SafetyGuard,
)
from .sandbox import SandboxManager


def _tool_filename(spec: ToolSpec) -> str:
    digest = stable_hash(spec.source_digest, spec.runtime, spec.signature)[:16]
    return f"generated_{digest}.py"


def _validation_temp_base() -> Path:
    return ensure_directory(ArtifactPolicy.resolve().tool_validation_root)


def _resolve_runtime_file_path(path: str | Path, *, workspace_root: Path) -> Path:
    candidate = Path(str(path or "").strip()).expanduser()
    if not str(candidate):
        raise ValidationError("filesystem/read_text_file requires a non-empty path")
    if candidate.is_absolute():
        return candidate.resolve()
    resolved = (workspace_root / candidate).resolve()
    try:
        resolved.relative_to(workspace_root)
    except ValueError as exc:
        raise ValidationError(
            f"filesystem/read_text_file path {str(candidate)!r} escapes runtime workspace {workspace_root}"
        ) from exc
    return resolved


def _materialize_generated_tool(spec: ToolSpec, source: str, sandbox_manager: SandboxManager) -> tuple[ToolSpec, Path]:
    staged_spec = (spec).model_copy(update={"source_digest": stable_hash(source)})
    staged_dir = sandbox_manager.ensure_environment(staged_spec)
    staged_file = staged_dir / _tool_filename(staged_spec)
    staged_file.write_text(source, encoding="utf-8")
    finalized_spec = (staged_spec).model_copy(update={"source_digest": file_digest(staged_file)})
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
