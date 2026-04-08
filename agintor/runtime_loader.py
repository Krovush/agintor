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
from .schemas import CapabilityExchange, DeploymentContract, KernelManifest, RuntimeManifest
from .runtime_profile import RUNTIME_PROFILE_FILE, RuntimeProfile, load_runtime_profile, profile_to_json
try:
    from .runtime_sdk import (
        KERNEL_BUNDLE_DIR,
        KERNEL_MANIFEST_FILE,
        KERNEL_PACKAGE_NAME,
        KERNEL_VERSION,
        STORAGE_SCHEMA_VERSION,
    )
except ImportError:
    KERNEL_BUNDLE_DIR = "runtime_sdk"
    KERNEL_MANIFEST_FILE = "kernel_manifest.json"
    KERNEL_PACKAGE_NAME = "agintor_runtime"
    KERNEL_VERSION = "agintor-kernel-v1"
    STORAGE_SCHEMA_VERSION = "agintor-storage-v1"
from .utils import ast_node_count, file_digest, stable_hash

RUNTIME_ABI_VERSION = "agintor-runtime-abi-v3"
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
    kernel_manifest: KernelManifest
    deployment_contract: DeploymentContract
    topology: Any
    memory: Any
    tooling: Any
    control: Any
    capability_exchange: CapabilityExchange
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
    if contract.kernel_version != KERNEL_VERSION:
        raise RuntimeLoadError(
            f"deployment contract kernel mismatch for {runtime_path}: contract={contract.kernel_version} loader={KERNEL_VERSION}"
        )
    if contract.storage_schema_version != STORAGE_SCHEMA_VERSION:
        raise RuntimeLoadError(
            f"deployment contract storage schema mismatch for {runtime_path}: contract={contract.storage_schema_version} loader={STORAGE_SCHEMA_VERSION}"
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


def _clear_runtime_package_cache() -> None:
    removable = [name for name in sys.modules if name == KERNEL_PACKAGE_NAME or name.startswith(f"{KERNEL_PACKAGE_NAME}.")]
    for name in removable:
        sys.modules.pop(name, None)


@contextlib.contextmanager
def _runtime_sdk_import_path(runtime_path: Path):
    bundle_root = (runtime_path / KERNEL_BUNDLE_DIR).resolve()
    previous_sys_path = list(sys.path)
    sys.path.insert(0, str(bundle_root))
    _clear_runtime_package_cache()
    try:
        yield
    finally:
        sys.path[:] = previous_sys_path


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
        if candidate.is_file() and runtime_path.resolve() in candidate.resolve().parents:
            return candidate
        raise RuntimeLoadError(f"missing immutable dependency {rel_path}")
    resolved = (runtime_path / candidate).resolve()
    if runtime_path.resolve() in resolved.parents and resolved.is_file():
        return resolved
    raise RuntimeLoadError(f"missing immutable dependency {rel_path}")


def _resolve_runtime_owned_path(runtime_path: Path, rel_path: str, *, label: str) -> Path:
    candidate = Path(rel_path)
    if candidate.is_absolute():
        raise RuntimeLoadError(f"{label} must be runtime-relative: {rel_path}")
    resolved = (runtime_path / candidate).resolve()
    if runtime_path.resolve() in resolved.parents and resolved.is_file():
        return resolved
    raise RuntimeLoadError(f"missing {label} {rel_path}")


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
        missing_any_of = []
        for group in contract.required_env_any_of:
            candidates = [str(name).strip() for name in group if str(name).strip()]
            if candidates and not any(os.environ.get(name) for name in candidates):
                missing_any_of.append(candidates)
        if missing_any_of:
            rendered = "; ".join(f"one of {', '.join(sorted(group))}" for group in missing_any_of)
            raise RuntimeLoadError(
                f"missing required runtime environment variables for {runtime_path}: {rendered}"
            )


def _load_kernel_manifest(runtime_path: Path) -> KernelManifest:
    manifest_path = runtime_path / KERNEL_BUNDLE_DIR / KERNEL_MANIFEST_FILE
    if not manifest_path.exists():
        raise RuntimeLoadError(f"missing {KERNEL_BUNDLE_DIR}/{KERNEL_MANIFEST_FILE} in {runtime_path}")
    manifest = model_validate(KernelManifest, json.loads(manifest_path.read_text(encoding="utf-8")))
    if manifest.runtime_abi != RUNTIME_ABI_VERSION:
        raise RuntimeLoadError(
            f"kernel ABI mismatch for {runtime_path}: kernel={manifest.runtime_abi} loader={RUNTIME_ABI_VERSION}"
        )
    if manifest.kernel_version != KERNEL_VERSION:
        raise RuntimeLoadError(
            f"kernel version mismatch for {runtime_path}: kernel={manifest.kernel_version} loader={KERNEL_VERSION}"
        )
    if manifest.storage_schema_version != STORAGE_SCHEMA_VERSION:
        raise RuntimeLoadError(
            f"storage schema mismatch for {runtime_path}: kernel={manifest.storage_schema_version} loader={STORAGE_SCHEMA_VERSION}"
        )
    return manifest


def _verified_kernel_bundle_fingerprints(runtime_path: Path, kernel_manifest: KernelManifest) -> dict[str, str]:
    bundle_root = (runtime_path / KERNEL_BUNDLE_DIR).resolve()
    fingerprints: dict[str, str] = {}
    for rel_path, expected_digest in sorted(kernel_manifest.files.items()):
        relative_path = Path(rel_path)
        if relative_path.is_absolute():
            raise RuntimeLoadError(f"invalid kernel bundle path {rel_path!r} in {runtime_path}")
        file_path = (bundle_root / relative_path).resolve()
        if bundle_root != file_path.parent and bundle_root not in file_path.parents:
            raise RuntimeLoadError(f"kernel bundle path escapes runtime bundle: {rel_path!r}")
        if not file_path.is_file():
            raise RuntimeLoadError(f"missing bundled kernel file {rel_path!r} in {runtime_path}")
        actual_digest = file_digest(file_path)
        if actual_digest != expected_digest:
            raise RuntimeLoadError(
                f"bundled kernel digest mismatch for {rel_path!r} in {runtime_path}: "
                f"manifest={expected_digest} actual={actual_digest}"
            )
        fingerprints[f"{KERNEL_BUNDLE_DIR}/{relative_path.as_posix()}"] = actual_digest
    return fingerprints


def runtime_identity_inputs(
    runtime_dir: str | Path,
    *,
    runtime_profile: RuntimeProfile | None = None,
    profile_path: str | Path | None = None,
) -> dict[str, dict[str, str]]:
    runtime_path = Path(runtime_dir)
    manifest = _load_manifest(runtime_path)
    kernel_manifest = _load_kernel_manifest(runtime_path)
    mutable_fingerprints: dict[str, str] = {}
    for module_ref in manifest.policy_modules.values():
        rel_path, _ = module_ref.split(":", 1)
        module_path = _resolve_runtime_owned_path(runtime_path, rel_path, label="policy module")
        source = module_path.read_text(encoding="utf-8")
        mutable_fingerprints[rel_path] = stable_hash(source)
    immutable_fingerprints = _verified_kernel_bundle_fingerprints(runtime_path, kernel_manifest)
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
    kernel_manifest = _load_kernel_manifest(runtime_path)
    _verified_kernel_bundle_fingerprints(runtime_path, kernel_manifest)
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
    with _runtime_sdk_import_path(runtime_path):
        for key, module_ref in manifest.policy_modules.items():
            rel_path, class_name = module_ref.split(":", 1)
            module_path = _resolve_runtime_owned_path(runtime_path, rel_path, label="policy module")
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
    code_hash = stable_hash(
        identity_inputs,
        kernel_manifest.kernel_version,
        kernel_manifest.storage_schema_version,
    )
    runtime_hash = stable_hash(model_dump(manifest), model_dump(kernel_manifest), code_hash)
    capability_exchange = CapabilityExchange(
        runtime_abi=RUNTIME_ABI_VERSION,
        kernel_version=kernel_manifest.kernel_version,
        storage_schema_version=kernel_manifest.storage_schema_version,
        supported_backends=list(deployment_contract.supported_backends),
        tool_runtimes=["python"],
        checkpoint_support=True,
        runtime_asset_capabilities={"traces": True, "checkpoints": True, "runtime_sdk": True},
        side_effect_receipts=False,
        required_env_names=list(deployment_contract.required_env_names),
        required_env_any_of=[list(group) for group in deployment_contract.required_env_any_of],
        capability_flags=list(deployment_contract.capability_flags or kernel_manifest.capability_flags),
    )
    return LoadedRuntime(
        runtime_dir=runtime_path,
        manifest=manifest,
        kernel_manifest=kernel_manifest,
        deployment_contract=deployment_contract,
        topology=policy_objects["top"],
        memory=policy_objects["mem"],
        tooling=policy_objects["tool"],
        control=policy_objects["ctl"],
        capability_exchange=capability_exchange,
        code_hash=code_hash,
        runtime_hash=runtime_hash,
        mutable_ast_nodes=ast_count,
        mutable_loc=mutable_loc,
    )


__all__ = [
    "DEPLOYMENT_CONTRACT_FILE",
    "KERNEL_VERSION",
    "LoadedRuntime",
    "RUNTIME_ABI_VERSION",
    "RUNTIME_EXPORT_BUNDLE_FILE",
    "RUNTIME_PROVENANCE_BUNDLE_FILE",
    "STORAGE_SCHEMA_VERSION",
    "load_runtime",
    "runtime_identity_inputs",
]
