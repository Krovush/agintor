from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Dict

from .exceptions import RuntimeLoadError
from .pydantic_compat import model_dump, model_validate
from .schemas import RuntimeManifest
from .utils import ast_node_count, stable_hash


@dataclass
class LoadedRuntime:
    runtime_dir: Path
    manifest: RuntimeManifest
    topology: Any
    memory: Any
    tooling: Any
    control: Any
    code_hash: str
    runtime_hash: str
    mutable_ast_nodes: int
    mutable_loc: int


def _load_module(module_name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeLoadError(f"unable to load module {module_name} from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_runtime(runtime_dir: str | Path) -> LoadedRuntime:
    runtime_path = Path(runtime_dir)
    manifest_path = runtime_path / "runtime_manifest.json"
    if not manifest_path.exists():
        raise RuntimeLoadError(f"missing runtime_manifest.json in {runtime_path}")
    manifest = model_validate(RuntimeManifest, json.loads(manifest_path.read_text(encoding="utf-8")))
    policy_objects: Dict[str, Any] = {}
    code_parts: list[str] = []
    ast_count = 0
    mutable_loc = 0
    for key, module_ref in manifest.policy_modules.items():
        rel_path, class_name = module_ref.split(":", 1)
        module_path = runtime_path / rel_path
        module = _load_module(f"agintor_runtime_{runtime_path.name}_{key}", module_path)
        if not hasattr(module, class_name):
            raise RuntimeLoadError(f"module {module_path} missing class {class_name}")
        policy_objects[key] = getattr(module, class_name)()
        source = module_path.read_text(encoding="utf-8")
        code_parts.append(source)
        ast_count += ast_node_count(source)
        mutable_loc += len(source.splitlines())
    code_hash = stable_hash(*code_parts)
    runtime_hash = stable_hash(model_dump(manifest), code_hash)
    return LoadedRuntime(
        runtime_dir=runtime_path,
        manifest=manifest,
        topology=policy_objects["top"],
        memory=policy_objects["mem"],
        tooling=policy_objects["tool"],
        control=policy_objects["ctl"],
        code_hash=code_hash,
        runtime_hash=runtime_hash,
        mutable_ast_nodes=ast_count,
        mutable_loc=mutable_loc,
    )
