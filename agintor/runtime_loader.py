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
from .runtime_profile import RUNTIME_PROFILE_FILE, RuntimeProfile, load_runtime_profile, profile_to_json
from .utils import ast_node_count, file_digest, stable_hash


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


def _resolve_manifest_path(runtime_path: Path, rel_path: str) -> Path:
    candidate = Path(rel_path)
    if candidate.is_absolute():
        if candidate.is_file():
            return candidate
        raise RuntimeLoadError(f"missing immutable dependency {rel_path}")
    package_root = Path(__file__).resolve().parent
    search_roots = [runtime_path, package_root.parent, package_root]
    checked: set[Path] = set()
    for root in search_roots:
        resolved = (root / candidate).resolve()
        if resolved in checked:
            continue
        checked.add(resolved)
        if resolved.is_file():
            return resolved
    raise RuntimeLoadError(f"missing immutable dependency {rel_path}")


def _effective_profile_payload(
    runtime_path: Path,
    runtime_profile: RuntimeProfile | None,
    profile_path: str | Path | None,
) -> str:
    effective_profile = runtime_profile or load_runtime_profile(runtime_path, profile_path=profile_path)
    return profile_to_json(effective_profile)


def load_runtime(
    runtime_dir: str | Path,
    *,
    runtime_profile: RuntimeProfile | None = None,
    profile_path: str | Path | None = None,
) -> LoadedRuntime:
    runtime_path = Path(runtime_dir)
    manifest_path = runtime_path / "runtime_manifest.json"
    if not manifest_path.exists():
        raise RuntimeLoadError(f"missing runtime_manifest.json in {runtime_path}")
    manifest = model_validate(RuntimeManifest, json.loads(manifest_path.read_text(encoding="utf-8")))
    policy_objects: Dict[str, Any] = {}
    mutable_fingerprints: dict[str, str] = {}
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
        mutable_fingerprints[rel_path] = stable_hash(source)
        ast_count += ast_node_count(source)
        mutable_loc += len(source.splitlines())
    immutable_fingerprints: dict[str, str] = {}
    for rel_path in manifest.immutable_manifest:
        if Path(rel_path).name == RUNTIME_PROFILE_FILE:
            immutable_fingerprints[rel_path] = stable_hash(
                _effective_profile_payload(runtime_path, runtime_profile, profile_path)
            )
            continue
        immutable_fingerprints[rel_path] = file_digest(_resolve_manifest_path(runtime_path, rel_path))
    code_hash = stable_hash(
        {
            "mutable_files": mutable_fingerprints,
            "immutable_files": immutable_fingerprints,
        }
    )
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
