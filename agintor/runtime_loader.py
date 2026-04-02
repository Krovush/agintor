from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Dict

from .exceptions import RuntimeLoadError
from .pydantic_compat import model_dump, model_validate
from .schemas import DeploymentContract, RuntimeManifest
from .runtime_profile import RUNTIME_PROFILE_FILE, RuntimeProfile, load_runtime_profile, profile_to_json
from .utils import ast_node_count, file_digest, stable_hash

RUNTIME_ABI_VERSION = "agintor-runtime-abi-v2"
DEPLOYMENT_CONTRACT_FILE = "deployment_contract.json"
RUNTIME_EXPORT_BUNDLE_FILE = "runtime_export_bundle.json"
RUNTIME_PROVENANCE_BUNDLE_FILE = "runtime_provenance_bundle.json"


def _validate_runtime_abi(runtime_path: Path, manifest: RuntimeManifest) -> None:
    runtime_abi = str(manifest.metadata.get("runtime_abi", "")).strip() if isinstance(manifest.metadata, dict) else ""
    if runtime_abi and runtime_abi != RUNTIME_ABI_VERSION:
        raise RuntimeLoadError(
            f"runtime ABI mismatch for {runtime_path}: runtime={runtime_abi} loader={RUNTIME_ABI_VERSION}"
        )


@dataclass
class LoadedRuntime:
    runtime_dir: Path
    manifest: RuntimeManifest
    deployment_contract: DeploymentContract
    topology: Any
    memory: Any
    tooling: Any
    control: Any
    code_hash: str
    runtime_hash: str
    mutable_ast_nodes: int
    mutable_loc: int


def _load_manifest(runtime_path: Path) -> RuntimeManifest:
    manifest_path = runtime_path / "runtime_manifest.json"
    if not manifest_path.exists():
        raise RuntimeLoadError(f"missing runtime_manifest.json in {runtime_path}")
    manifest = model_validate(RuntimeManifest, json.loads(manifest_path.read_text(encoding="utf-8")))
    _validate_runtime_abi(runtime_path, manifest)
    return manifest


def _load_deployment_contract(runtime_path: Path) -> DeploymentContract:
    contract_path = runtime_path / DEPLOYMENT_CONTRACT_FILE
    if not contract_path.exists():
        raise RuntimeLoadError(f"missing {DEPLOYMENT_CONTRACT_FILE} in {runtime_path}")
    contract = model_validate(DeploymentContract, json.loads(contract_path.read_text(encoding="utf-8")))
    if contract.runtime_abi != RUNTIME_ABI_VERSION:
        raise RuntimeLoadError(
            f"deployment contract ABI mismatch for {runtime_path}: contract={contract.runtime_abi} loader={RUNTIME_ABI_VERSION}"
        )
    return contract


@contextlib.contextmanager
def _without_bytecode_writes():
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        yield
    finally:
        sys.dont_write_bytecode = previous


def _load_module(module_name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeLoadError(f"unable to load module {module_name} from {path}")
    module = importlib.util.module_from_spec(spec)
    with _without_bytecode_writes():
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
    return profile_to_json(effective_profile, runtime_only=True)


def _parse_python_version(value: str) -> tuple[int, ...]:
    cleaned = value.strip()
    if cleaned.startswith(">="):
        cleaned = cleaned[2:]
    if cleaned.endswith("+"):
        cleaned = cleaned[:-1]
    parts: list[int] = []
    for part in cleaned.split("."):
        if not part:
            continue
        try:
            parts.append(int(part))
        except ValueError:
            break
    return tuple(parts)


def _python_version_ok(spec: str) -> bool:
    current = (sys.version_info.major, sys.version_info.minor)
    text = spec.strip()
    if not text:
        return True
    parsed = _parse_python_version(text)
    if not parsed:
        return True
    if text.startswith(">=") or text.endswith("+"):
        return current >= parsed[:2]
    return current[: len(parsed)] == parsed


def _validate_deployment_contract(
    runtime_path: Path,
    contract: DeploymentContract,
    *,
    runtime_backend: str | None = None,
    require_env_names: bool = False,
) -> None:
    if not _python_version_ok(contract.python_version):
        raise RuntimeLoadError(
            f"python version mismatch for {runtime_path}: required={contract.python_version} current={sys.version_info.major}.{sys.version_info.minor}"
        )
    if runtime_backend is not None:
        backend = str(runtime_backend).strip().lower()
        supported = {item.strip().lower() for item in contract.supported_backends}
        if backend and supported and backend not in supported:
            raise RuntimeLoadError(
                f"runtime backend {backend!r} is not supported by {runtime_path}; supported backends: {sorted(supported)}"
            )
    if require_env_names:
        missing = [name for name in contract.required_env_names if name and not os.environ.get(name)]
        if missing:
            raise RuntimeLoadError(
                f"missing required runtime environment variables for {runtime_path}: {', '.join(sorted(missing))}"
            )


def runtime_identity_inputs(
    runtime_dir: str | Path,
    *,
    runtime_profile: RuntimeProfile | None = None,
    profile_path: str | Path | None = None,
) -> dict[str, dict[str, str]]:
    runtime_path = Path(runtime_dir)
    manifest = _load_manifest(runtime_path)
    mutable_fingerprints: dict[str, str] = {}
    for module_ref in manifest.policy_modules.values():
        rel_path, _ = module_ref.split(":", 1)
        module_path = runtime_path / rel_path
        source = module_path.read_text(encoding="utf-8")
        mutable_fingerprints[rel_path] = stable_hash(source)
    immutable_fingerprints: dict[str, str] = {}
    for rel_path in manifest.immutable_manifest:
        if Path(rel_path).name == RUNTIME_PROFILE_FILE:
            immutable_fingerprints[rel_path] = stable_hash(
                _effective_profile_payload(runtime_path, runtime_profile, profile_path)
            )
            continue
        immutable_fingerprints[rel_path] = file_digest(_resolve_manifest_path(runtime_path, rel_path))
    return {
        "mutable_files": mutable_fingerprints,
        "immutable_files": immutable_fingerprints,
    }


def load_runtime(
    runtime_dir: str | Path,
    *,
    runtime_profile: RuntimeProfile | None = None,
    profile_path: str | Path | None = None,
    runtime_backend: str | None = None,
    require_env_names: bool = False,
) -> LoadedRuntime:
    runtime_path = Path(runtime_dir)
    manifest = _load_manifest(runtime_path)
    deployment_contract = _load_deployment_contract(runtime_path)
    _validate_deployment_contract(
        runtime_path,
        deployment_contract,
        runtime_backend=runtime_backend,
        require_env_names=require_env_names,
    )
    policy_objects: Dict[str, Any] = {}
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
        ast_count += ast_node_count(source)
        mutable_loc += len(source.splitlines())
    identity_inputs = runtime_identity_inputs(
        runtime_path,
        runtime_profile=runtime_profile,
        profile_path=profile_path,
    )
    code_hash = stable_hash(identity_inputs)
    runtime_hash = stable_hash(model_dump(manifest), code_hash)
    return LoadedRuntime(
        runtime_dir=runtime_path,
        manifest=manifest,
        deployment_contract=deployment_contract,
        topology=policy_objects["top"],
        memory=policy_objects["mem"],
        tooling=policy_objects["tool"],
        control=policy_objects["ctl"],
        code_hash=code_hash,
        runtime_hash=runtime_hash,
        mutable_ast_nodes=ast_count,
        mutable_loc=mutable_loc,
    )


__all__ = [
    "DEPLOYMENT_CONTRACT_FILE",
    "LoadedRuntime",
    "RUNTIME_ABI_VERSION",
    "RUNTIME_EXPORT_BUNDLE_FILE",
    "RUNTIME_PROVENANCE_BUNDLE_FILE",
    "load_runtime",
    "runtime_identity_inputs",
]
