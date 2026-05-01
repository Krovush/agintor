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

from ..core.exceptions import RuntimeLoadError
from ..contracts import CapabilityExchange, DeploymentContract, KernelManifest, RuntimeIsolationPolicy, RuntimeManifest
from .profile import RUNTIME_PROFILE_FILE, RuntimeProfile, load_runtime_profile, profile_to_json
try:
    from .sdk import (
        KERNEL_BUNDLE_DIR,
        KERNEL_MANIFEST_FILE,
        KERNEL_PACKAGE_NAME,
    )
except ImportError:
    KERNEL_BUNDLE_DIR = "runtime_sdk"
    KERNEL_MANIFEST_FILE = "kernel_manifest.json"
    KERNEL_PACKAGE_NAME = "agintor_runtime"
from ..utils import ast_node_count, file_digest, stable_hash
from ..core.versioning import RUNTIME_CONTRACT_VERSION

DEPLOYMENT_CONTRACT_FILE = "deployment_contract.json"
RUNTIME_EXPORT_BUNDLE_FILE = "runtime_export_bundle.json"


def _validate_runtime_contract(runtime_path: Path, manifest: RuntimeManifest) -> None:
    contract_version = (
        str(manifest.metadata.get("runtime_contract_version", "")).strip()
        if isinstance(manifest.metadata, dict)
        else ""
    )
    if contract_version and contract_version != RUNTIME_CONTRACT_VERSION:
        raise RuntimeLoadError(
            f"runtime contract mismatch for {runtime_path}: runtime={contract_version} loader={RUNTIME_CONTRACT_VERSION}"
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


@dataclass(frozen=True)
class DockerLaunchPolicy:
    deployment_contract: DeploymentContract
    runtime_isolation_policy: RuntimeIsolationPolicy
    network_none: bool


def _load_manifest(runtime_path: Path) -> RuntimeManifest:
    manifest_path = runtime_path / "runtime_manifest.json"
    if not manifest_path.exists():
        raise RuntimeLoadError(f"missing runtime_manifest.json in {runtime_path}")
    manifest = (RuntimeManifest).model_validate(json.loads(manifest_path.read_text(encoding="utf-8")))
    _validate_runtime_contract(runtime_path, manifest)
    return manifest


def _load_deployment_contract(runtime_path: Path) -> DeploymentContract:
    contract_path = runtime_path / DEPLOYMENT_CONTRACT_FILE
    if not contract_path.exists():
        raise RuntimeLoadError(f"missing {DEPLOYMENT_CONTRACT_FILE} in {runtime_path}")
    try:
        payload = json.loads(contract_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeLoadError(f"unable to read deployment contract {contract_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeLoadError(f"invalid JSON in deployment contract {contract_path}: {exc.msg}") from exc
    try:
        contract = (DeploymentContract).model_validate(payload)
    except Exception as exc:
        raise RuntimeLoadError(f"invalid deployment contract schema in {contract_path}: {exc}") from exc
    if contract.runtime_contract_version != RUNTIME_CONTRACT_VERSION:
        raise RuntimeLoadError(
            f"deployment contract mismatch for {runtime_path}: contract={contract.runtime_contract_version} loader={RUNTIME_CONTRACT_VERSION}"
        )
    return contract


def _resolved_runtime_isolation_policy(runtime_path: Path, contract: DeploymentContract) -> RuntimeIsolationPolicy:
    if contract.runtime_isolation_policy is not None:
        return contract.runtime_isolation_policy
    return RuntimeIsolationPolicy(
        timeout_envelope={},
        workspace_root=".",
        environment_allowlist=list(contract.environment_allowlist),
        network_policy=contract.network_policy,
        filesystem_policy=contract.filesystem_policy,
        required_guarantees=[],
        desired_guarantees=[],
    )


def _docker_requires_network_none(policy: RuntimeIsolationPolicy) -> bool:
    required = {str(item).strip().lower() for item in policy.required_guarantees}
    network_policy = str(policy.network_policy).strip().lower()
    return "network_disablement" in required or network_policy in {"none", "restricted"}


def _effective_guarantees_for_backend(policy: RuntimeIsolationPolicy, backend: str | None) -> list[str]:
    backend_key = str(backend or "").strip().lower()
    if backend_key == "local":
        return [
            "timeout_enforcement",
            "workspace_isolation",
            "environment_filtering",
        ]
    if backend_key == "docker":
        guarantees = [
            "timeout_enforcement",
            "workspace_isolation",
            "environment_filtering",
            "process_cleanup",
        ]
        if _docker_requires_network_none(policy):
            guarantees.append("network_disablement")
        return guarantees
    return []


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
    isolation_policy = _resolved_runtime_isolation_policy(runtime_path, contract)
    backend_claims = {
        "local": {
            "timeout_enforcement",
            "workspace_isolation",
            "environment_filtering",
        },
        "docker": {
            "timeout_enforcement",
            "workspace_isolation",
            "environment_filtering",
            "process_cleanup",
            "network_disablement",
        },
    }
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
        if backend == "local" and _docker_requires_network_none(isolation_policy):
            raise RuntimeLoadError(
                f"runtime backend {backend!r} cannot satisfy network policy {isolation_policy.network_policy!r} for {runtime_path}"
            )
        missing_guarantees = sorted(set(isolation_policy.required_guarantees) - backend_claims.get(backend, set()))
        if missing_guarantees:
            raise RuntimeLoadError(
                f"runtime backend {backend!r} cannot satisfy required isolation guarantees for {runtime_path}: {', '.join(missing_guarantees)}"
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


def resolve_docker_launch_policy(runtime_dir: str | Path) -> DockerLaunchPolicy:
    runtime_path = Path(runtime_dir).resolve()
    contract = _load_deployment_contract(runtime_path)
    policy = _resolved_runtime_isolation_policy(runtime_path, contract)
    _validate_deployment_contract(runtime_path, contract, runtime_backend="docker")
    return DockerLaunchPolicy(
        deployment_contract=contract,
        runtime_isolation_policy=policy,
        network_none=_docker_requires_network_none(policy),
    )


def _load_kernel_manifest(runtime_path: Path) -> KernelManifest:
    manifest_path = runtime_path / KERNEL_BUNDLE_DIR / KERNEL_MANIFEST_FILE
    if not manifest_path.exists():
        raise RuntimeLoadError(f"missing {KERNEL_BUNDLE_DIR}/{KERNEL_MANIFEST_FILE} in {runtime_path}")
    manifest = (KernelManifest).model_validate(json.loads(manifest_path.read_text(encoding="utf-8")))
    if manifest.runtime_contract_version != RUNTIME_CONTRACT_VERSION:
        raise RuntimeLoadError(
            f"kernel contract mismatch for {runtime_path}: kernel={manifest.runtime_contract_version} loader={RUNTIME_CONTRACT_VERSION}"
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
    runtime_isolation_policy = _resolved_runtime_isolation_policy(runtime_path, deployment_contract)
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
    code_hash = stable_hash(identity_inputs, kernel_manifest.runtime_contract_version)
    runtime_hash = stable_hash((manifest).model_dump(), (kernel_manifest).model_dump(), code_hash)
    capability_exchange = CapabilityExchange(
        runtime_contract_version=kernel_manifest.runtime_contract_version,
        supported_backends=list(deployment_contract.supported_backends),
        tool_runtimes=["python"],
        checkpoint_support=True,
        runtime_asset_capabilities={"traces": True, "checkpoints": True, "runtime_sdk": True},
        side_effect_receipts=True,
        resume_support=True,
        runtime_isolation_policy=runtime_isolation_policy,
        supported_guarantees=[
            "timeout_enforcement",
            "workspace_isolation",
            "environment_filtering",
            "process_cleanup",
            "network_disablement",
        ],
        effective_guarantees=_effective_guarantees_for_backend(runtime_isolation_policy, runtime_backend),
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
    "DockerLaunchPolicy",
    "LoadedRuntime",
    "RUNTIME_CONTRACT_VERSION",
    "RUNTIME_EXPORT_BUNDLE_FILE",
    "load_runtime",
    "resolve_docker_launch_policy",
    "runtime_identity_inputs",
]
