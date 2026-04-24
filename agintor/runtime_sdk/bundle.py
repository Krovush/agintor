from __future__ import annotations

import json
import shutil
from pathlib import Path

from ..pydantic_compat import model_dump
from ..schemas import KernelManifest
from ..utils import ensure_directory, file_digest

KERNEL_BUNDLE_DIR = "runtime_sdk"
KERNEL_MANIFEST_FILE = "kernel_manifest.json"
KERNEL_PACKAGE_NAME = "agintor_runtime"
KERNEL_VERSION = "agintor-kernel-v1"
STORAGE_SCHEMA_VERSION = "agintor-storage-v3"
KERNEL_CAPABILITY_FLAGS = [
    "inspect",
    "run_batch",
    "resume",
    "execution_plan_v1",
    "checkpoint_refs",
    "checkpoint_envelopes",
    "provider_usage",
    "trace_refs",
    "side_effect_receipts",
    "runtime_isolation",
]

_KERNEL_SOURCE_FILES = [
    "__init__.py",
    "artifacts.py",
    "exceptions.py",
    "memory_graph.py",
    "openai_trace.py",
    "provider_common.py",
    "provider_minimax.py",
    "provider_openai.py",
    "providers.py",
    "predictors.py",
    "prompts.py",
    "pydantic_compat.py",
    "run_store.py",
    "runner.py",
    "task_runtime/__init__.py",
    "task_runtime/base.py",
    "task_runtime/bounded_io.py",
    "task_runtime/branch_execution.py",
    "task_runtime/branching.py",
    "task_runtime/checkpointing.py",
    "task_runtime/execution_loop.py",
    "task_runtime/frames.py",
    "task_runtime/memory.py",
    "task_runtime/operations.py",
    "task_runtime/plan_helpers.py",
    "task_runtime/side_effects.py",
    "task_runtime/tooling.py",
    "task_runtime/verification.py",
    "runtime_api.py",
    "runtime_loader.py",
    "runtime_profile.py",
    "schemas.py",
    "shell.py",
    "tool_runtime.py",
    "utils.py",
    "verifiers.py",
]
_KERNEL_TEMPLATE_FILES = [
    ("templates/baseline_runtime/runtime_profile.json", "templates/baseline_runtime/runtime_profile.json"),
    ("templates/prompts", "templates/prompts"),
]


def _package_root() -> Path:
    return Path(__file__).resolve().parent.parent


def kernel_manifest_path(runtime_dir: str | Path) -> Path:
    return Path(runtime_dir) / KERNEL_BUNDLE_DIR / KERNEL_MANIFEST_FILE


def _kernel_files_payload(package_root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in sorted(package_root.rglob("*")):
        if path.is_file():
            rel_path = path.relative_to(package_root.parent).as_posix()
            files[rel_path] = file_digest(path)
    return files


def preview_kernel_manifest(*, runtime_abi: str) -> KernelManifest:
    source_root = _package_root()
    bundle_root = source_root / "runtime_sdk"
    files: dict[str, str] = {}
    for rel_path in _KERNEL_SOURCE_FILES:
        source_path = source_root / rel_path
        files[f"{KERNEL_PACKAGE_NAME}/{rel_path}"] = file_digest(source_path)
    runtime_entry = bundle_root / "runtime_entry.py"
    files[f"{KERNEL_PACKAGE_NAME}/runtime_entry.py"] = file_digest(runtime_entry)
    for source_rel, bundle_rel in _KERNEL_TEMPLATE_FILES:
        source_path = source_root / source_rel
        if source_path.is_dir():
            for child in sorted(source_path.rglob("*")):
                if child.is_file():
                    rel_child = child.relative_to(source_path).as_posix()
                    files[f"{KERNEL_PACKAGE_NAME}/{bundle_rel}/{rel_child}"] = file_digest(child)
        else:
            files[f"{KERNEL_PACKAGE_NAME}/{bundle_rel}"] = file_digest(source_path)
    return KernelManifest(
        schema_version="agintor.kernel.manifest.v1",
        runtime_abi=runtime_abi,
        kernel_version=KERNEL_VERSION,
        storage_schema_version=STORAGE_SCHEMA_VERSION,
        package_name=KERNEL_PACKAGE_NAME,
        entry_module=f"{KERNEL_PACKAGE_NAME}.runtime_entry",
        files=files,
        capability_flags=list(KERNEL_CAPABILITY_FLAGS),
    )


def bundle_runtime_kernel(runtime_dir: str | Path, *, runtime_abi: str, force: bool = False) -> KernelManifest:
    runtime_path = Path(runtime_dir)
    bundle_root = runtime_path / KERNEL_BUNDLE_DIR
    package_root = bundle_root / KERNEL_PACKAGE_NAME
    source_root = _package_root()
    runtime_entry_source = source_root / "runtime_sdk" / "runtime_entry.py"
    if force and bundle_root.exists():
        shutil.rmtree(bundle_root)
    ensure_directory(package_root)
    for rel_path in _KERNEL_SOURCE_FILES:
        source_path = source_root / rel_path
        dest_path = package_root / rel_path
        ensure_directory(dest_path.parent)
        shutil.copy2(source_path, dest_path)
    runtime_entry_dest = package_root / "runtime_entry.py"
    ensure_directory(runtime_entry_dest.parent)
    shutil.copy2(runtime_entry_source, runtime_entry_dest)
    for source_rel, bundle_rel in _KERNEL_TEMPLATE_FILES:
        source_path = source_root / source_rel
        dest_path = package_root / bundle_rel
        if source_path.is_dir():
            if dest_path.exists():
                shutil.rmtree(dest_path)
            shutil.copytree(source_path, dest_path)
        else:
            ensure_directory(dest_path.parent)
            shutil.copy2(source_path, dest_path)
    manifest = KernelManifest(
        schema_version="agintor.kernel.manifest.v1",
        runtime_abi=runtime_abi,
        kernel_version=KERNEL_VERSION,
        storage_schema_version=STORAGE_SCHEMA_VERSION,
        package_name=KERNEL_PACKAGE_NAME,
        entry_module=f"{KERNEL_PACKAGE_NAME}.runtime_entry",
        files=_kernel_files_payload(package_root),
        capability_flags=list(KERNEL_CAPABILITY_FLAGS),
    )
    manifest_path = bundle_root / KERNEL_MANIFEST_FILE
    ensure_directory(manifest_path.parent)
    manifest_path.write_text(json.dumps(model_dump(manifest), indent=2, sort_keys=True), encoding="utf-8")
    return manifest
