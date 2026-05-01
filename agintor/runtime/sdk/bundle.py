from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from . import (
    KERNEL_BUNDLE_DIR,
    KERNEL_CAPABILITY_FLAGS,
    KERNEL_MANIFEST_FILE,
    KERNEL_PACKAGE_NAME,
)
from ...contracts import KernelManifest
from ...utils import ensure_directory, file_digest
from ...core.versioning import RUNTIME_CONTRACT_VERSION

_RUNTIME_ENTRY_SHIM_TEXT = """from __future__ import annotations

from .runtime.sdk.entrypoint import *  # noqa: F401,F403
from .runtime.sdk.entrypoint import main as _main

if __name__ == "__main__":
    raise SystemExit(_main())
"""

_CONTRACTS_INIT_SHIM_TEXT = """from __future__ import annotations

from .tracing import *  # noqa: F401,F403
from .providers import *  # noqa: F401,F403
from .execution import *  # noqa: F401,F403
from .state import *  # noqa: F401,F403
from .sessions import *  # noqa: F401,F403
from .runtime import *  # noqa: F401,F403
from .branches import *  # noqa: F401,F403
from .side_effects import *  # noqa: F401,F403
from .checkpoints import *  # noqa: F401,F403
from .benchmarks import *  # noqa: F401,F403
from .protocol import *  # noqa: F401,F403

_FORWARD_REF_NAMESPACE = dict(globals())
for _model in (RuntimeStateSnapshot, BranchResumeSnapshot, BranchResult, CheckpointEnvelope, SuiteEvaluation):
    if hasattr(_model, "model_rebuild"):
        _model.model_rebuild(_types_namespace=_FORWARD_REF_NAMESPACE)
    else:
        _model.update_forward_refs(**_FORWARD_REF_NAMESPACE)

del _model, _FORWARD_REF_NAMESPACE
"""

_GENERATED_PACKAGE_FILES = {
    "contracts/__init__.py": _CONTRACTS_INIT_SHIM_TEXT,
    "runtime_entry.py": _RUNTIME_ENTRY_SHIM_TEXT,
}

_KERNEL_SOURCE_ROOTS = (
    "providers",
    "runtime/api",
    "runtime/kernel",
    "runtime/tools",
    "storage/state_store",
    "tracing",
)

_KERNEL_SOURCE_FILES = (
    "__init__.py",
    "contracts/benchmarks.py",
    "contracts/branches.py",
    "contracts/checkpoints.py",
    "contracts/execution.py",
    "contracts/providers.py",
    "contracts/protocol.py",
    "contracts/runtime.py",
    "contracts/sessions.py",
    "contracts/side_effects.py",
    "contracts/state.py",
    "contracts/tracing.py",
    "core/__init__.py",
    "core/exceptions.py",
    "core/versioning.py",
    "runtime/__init__.py",
    "runtime/loader.py",
    "runtime/profile.py",
    "runtime/prompts.py",
    "runtime/sdk/__init__.py",
    "runtime/sdk/entrypoint.py",
    "storage/__init__.py",
    "storage/artifacts.py",
    "storage/run_store.py",
    "utils.py",
)

_KERNEL_RESOURCE_FILES = (
    ("templates/baseline_runtime/runtime_profile.json", "templates/baseline_runtime/runtime_profile.json"),
    ("templates/prompts/memory.span_summarize.json", "templates/prompts/memory.span_summarize.json"),
    ("templates/prompts/tool.spec_generate.json", "templates/prompts/tool.spec_generate.json"),
)

_KERNEL_FORBIDDEN_ROOTS = (
    "evaluation",
    "factory",
    "learning",
    "search",
    "runtime/host",
)

_KERNEL_FORBIDDEN_FILES = (
    "cli.py",
    "contracts/__init__.py",
    "contracts/factory.py",
    "contracts/search.py",
    "core/patches.py",
    "runtime/project.py",
    "runtime/sdk/bundle.py",
    "storage/factory_chat_store.py",
    "storage/runtime_session_store.py",
)


def _package_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def kernel_manifest_path(runtime_dir: str | Path) -> Path:
    return Path(runtime_dir) / KERNEL_BUNDLE_DIR / KERNEL_MANIFEST_FILE


def _text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_forbidden_source(rel_path: str) -> bool:
    normalized = rel_path.replace("\\", "/")
    if normalized in _KERNEL_FORBIDDEN_FILES:
        return True
    return any(normalized == root or normalized.startswith(f"{root}/") for root in _KERNEL_FORBIDDEN_ROOTS)


def _require_source_file(source_root: Path, rel_path: str) -> Path:
    normalized = rel_path.replace("\\", "/")
    if _is_forbidden_source(normalized):
        raise ValueError(f"kernel bundle spec cannot include forbidden source {normalized!r}")
    path = source_root / normalized
    if not path.is_file():
        raise FileNotFoundError(f"kernel bundle source file is missing: {normalized}")
    return path


def _iter_kernel_source_files(source_root: Path) -> list[Path]:
    rel_paths: set[str] = set()
    for root_rel in _KERNEL_SOURCE_ROOTS:
        normalized_root = root_rel.replace("\\", "/")
        if _is_forbidden_source(normalized_root):
            raise ValueError(f"kernel bundle spec cannot include forbidden root {normalized_root!r}")
        root_path = source_root / normalized_root
        if not root_path.is_dir():
            raise FileNotFoundError(f"kernel bundle source root is missing: {normalized_root}")
        for path in sorted(root_path.rglob("*.py")):
            if not path.is_file():
                continue
            rel_path = path.relative_to(source_root).as_posix()
            if _is_forbidden_source(rel_path):
                raise ValueError(f"kernel bundle source root {normalized_root!r} contains forbidden source {rel_path!r}")
            rel_paths.add(rel_path)
    rel_paths.update(_KERNEL_SOURCE_FILES)
    rel_paths.difference_update(_GENERATED_PACKAGE_FILES)
    return [_require_source_file(source_root, rel_path) for rel_path in sorted(rel_paths)]


def _iter_kernel_resource_files(source_root: Path) -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = []
    for source_rel, bundle_rel in _KERNEL_RESOURCE_FILES:
        source_path = source_root / source_rel
        if not source_path.is_file():
            raise FileNotFoundError(f"kernel bundle resource is missing: {source_rel}")
        files.append((source_path, bundle_rel))
    return files


def _kernel_files_payload(package_root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in sorted(package_root.rglob("*")):
        if path.is_file():
            rel_path = path.relative_to(package_root.parent).as_posix()
            files[rel_path] = file_digest(path)
    return files


def _kernel_manifest_files(source_root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for source_path in _iter_kernel_source_files(source_root):
        rel_path = source_path.relative_to(source_root).as_posix()
        files[f"{KERNEL_PACKAGE_NAME}/{rel_path}"] = file_digest(source_path)
    for rel_path, text in _GENERATED_PACKAGE_FILES.items():
        files[f"{KERNEL_PACKAGE_NAME}/{rel_path}"] = _text_digest(text)
    for source_path, bundle_rel in _iter_kernel_resource_files(source_root):
        files[f"{KERNEL_PACKAGE_NAME}/{bundle_rel}"] = file_digest(source_path)
    return files


def preview_kernel_manifest() -> KernelManifest:
    source_root = _package_root()
    return KernelManifest(
        runtime_contract_version=RUNTIME_CONTRACT_VERSION,
        package_name=KERNEL_PACKAGE_NAME,
        entry_module=f"{KERNEL_PACKAGE_NAME}.runtime_entry",
        files=_kernel_manifest_files(source_root),
        capability_flags=list(KERNEL_CAPABILITY_FLAGS),
    )


def _copy_kernel_source_files(source_root: Path, package_root: Path) -> None:
    for source_path in _iter_kernel_source_files(source_root):
        rel_path = source_path.relative_to(source_root)
        dest_path = package_root / rel_path
        ensure_directory(dest_path.parent)
        shutil.copy2(source_path, dest_path)


def _write_generated_package_files(package_root: Path) -> None:
    for rel_path, text in _GENERATED_PACKAGE_FILES.items():
        dest_path = package_root / rel_path
        ensure_directory(dest_path.parent)
        dest_path.write_text(text, encoding="utf-8")


def _copy_kernel_resources(source_root: Path, package_root: Path) -> None:
    for source_path, bundle_rel in _iter_kernel_resource_files(source_root):
        dest_path = package_root / bundle_rel
        ensure_directory(dest_path.parent)
        shutil.copy2(source_path, dest_path)


def bundle_runtime_kernel(runtime_dir: str | Path, *, force: bool = False) -> KernelManifest:
    runtime_path = Path(runtime_dir)
    bundle_root = runtime_path / KERNEL_BUNDLE_DIR
    package_root = bundle_root / KERNEL_PACKAGE_NAME
    source_root = _package_root()
    if force and bundle_root.exists():
        shutil.rmtree(bundle_root)
    elif package_root.exists():
        shutil.rmtree(package_root)
    ensure_directory(package_root)
    _copy_kernel_source_files(source_root, package_root)
    _write_generated_package_files(package_root)
    _copy_kernel_resources(source_root, package_root)
    manifest = KernelManifest(
        runtime_contract_version=RUNTIME_CONTRACT_VERSION,
        package_name=KERNEL_PACKAGE_NAME,
        entry_module=f"{KERNEL_PACKAGE_NAME}.runtime_entry",
        files=_kernel_files_payload(package_root),
        capability_flags=list(KERNEL_CAPABILITY_FLAGS),
    )
    manifest_path = bundle_root / KERNEL_MANIFEST_FILE
    ensure_directory(manifest_path.parent)
    manifest_path.write_text(json.dumps((manifest).model_dump(), indent=2, sort_keys=True), encoding="utf-8")
    return manifest
