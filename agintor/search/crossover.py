from __future__ import annotations

import ast
import shutil
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from ..core.exceptions import ValidationError
from ..utils import ensure_directory, stable_hash


POLICY_FILE_BY_INTERFACE = {
    "top": "topology_policy.py",
    "mem": "memory_policy.py",
    "tool": "tool_policy.py",
    "ctl": "control_policy.py",
}


class MethodExtractor(ast.NodeTransformer):
    def __init__(self, replacements: Mapping[str, ast.FunctionDef | ast.AsyncFunctionDef]) -> None:
        self.replacements = replacements

    def visit_ClassDef(self, node: ast.ClassDef):
        new_body = []
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name in self.replacements:
                new_body.append(self.replacements[item.name])
            else:
                new_body.append(item)
        node.body = new_body
        return node



def _extract_methods(path: Path, method_names: Iterable[str]) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    methods: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    wanted = set(method_names)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name in wanted:
                    methods[item.name] = item
    return methods



def crossover_runtime(base_runtime_dir: Path, donor_runtime_dirs: Sequence[Path], interface_methods: Mapping[str, Sequence[str]], workspace: Path) -> Path:
    child_dir = ensure_directory(workspace / f"xover_{stable_hash(base_runtime_dir, donor_runtime_dirs, interface_methods)[:10]}")
    if child_dir.exists():
        shutil.rmtree(child_dir)
    shutil.copytree(base_runtime_dir, child_dir)
    for interface, methods in interface_methods.items():
        file_name = POLICY_FILE_BY_INTERFACE[interface]
        donors = [donor / file_name for donor in donor_runtime_dirs if (donor / file_name).exists()]
        if not donors:
            continue
        method_to_donor = {}
        for idx, method_name in enumerate(methods):
            donor_path = donors[idx % len(donors)]
            extracted = _extract_methods(donor_path, [method_name])
            if method_name not in extracted:
                raise ValidationError(f"donor {donor_path} missing method {method_name}")
            if method_name in method_to_donor:
                raise ValidationError(f"overlapping method edit {method_name}")
            method_to_donor[method_name] = extracted[method_name]
        target_path = child_dir / file_name
        tree = ast.parse(target_path.read_text(encoding="utf-8"))
        new_tree = MethodExtractor(method_to_donor).visit(tree)
        ast.fix_missing_locations(new_tree)
        target_path.write_text(ast.unparse(new_tree) + "\n", encoding="utf-8")
    return child_dir
