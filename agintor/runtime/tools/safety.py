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
