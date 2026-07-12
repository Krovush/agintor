from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from . import (
    KERNEL_BUNDLE_DIR,
    KERNEL_CAPABILITY_FLAGS,
    KERNEL_MANIFEST_FILE,
    KERNEL_PACKAGE_NAME,
)
from .harness_manifest import (
    HARNESS_BUNDLE_PROFILE,
    HARNESS_KERNEL_CAPABILITY_FLAGS,
    LEGACY_BUNDLE_PROFILE,
    HarnessKernelManifest,
)
from ...core.versioning import RUNTIME_CONTRACT_VERSION
from ...utils import ensure_directory, file_digest


BundleProfile = Literal["harness", "legacy"]

_HARNESS_RUNTIME_ENTRY_TEXT = """from __future__ import annotations

from .runtime.sdk.harness_entrypoint import *  # noqa: F401,F403
from .runtime.sdk.harness_entrypoint import main as _main

if __name__ == "__main__":
    raise SystemExit(_main())
"""

_LEGACY_RUNTIME_ENTRY_TEXT = """from __future__ import annotations

from .runtime.sdk.entrypoint import *  # noqa: F401,F403
from .runtime.sdk.entrypoint import main as _main

if __name__ == "__main__":
    raise SystemExit(_main())
"""

_MINIMAL_INIT_TEXT = "from __future__ import annotations\n"

_HARNESS_SDK_INIT_TEXT = f'''from __future__ import annotations

from ...core.versioning import RUNTIME_CONTRACT_VERSION

KERNEL_BUNDLE_DIR = "runtime_sdk"
KERNEL_MANIFEST_FILE = "kernel_manifest.json"
KERNEL_PACKAGE_NAME = "agintor_runtime"
KERNEL_BUNDLE_PROFILE = "{HARNESS_BUNDLE_PROFILE}"
KERNEL_CAPABILITY_FLAGS = {HARNESS_KERNEL_CAPABILITY_FLAGS!r}

__all__ = [
    "KERNEL_BUNDLE_DIR",
    "KERNEL_BUNDLE_PROFILE",
    "KERNEL_CAPABILITY_FLAGS",
    "KERNEL_MANIFEST_FILE",
    "KERNEL_PACKAGE_NAME",
    "RUNTIME_CONTRACT_VERSION",
]
'''

_MINIMAL_UTILS_TEXT = """from __future__ import annotations

import math


def count_tokens_rough(text: str) -> int:
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))


__all__ = ["count_tokens_rough"]
"""

_LEGACY_CONTRACTS_INIT_TEXT = """from __future__ import annotations

from .tracing import *  # noqa: F401,F403
from .providers import *  # noqa: F401,F403
from .execution import *  # noqa: F401,F403
from .state import *  # noqa: F401,F403
from .sessions import *  # noqa: F401,F403
from .runtime import *  # noqa: F401,F403
from .runtime_spec import *  # noqa: F401,F403
from .branches import *  # noqa: F401,F403
from .side_effects import *  # noqa: F401,F403
from .checkpoints import *  # noqa: F401,F403
from .benchmarks import *  # noqa: F401,F403
from .protocol import *  # noqa: F401,F403
from .verifiers import *  # noqa: F401,F403
from .evidence import *  # noqa: F401,F403
from .epochs import *  # noqa: F401,F403
from .harness import *  # noqa: F401,F403
from .harness_actions import *  # noqa: F401,F403
from .outcomes import *  # noqa: F401,F403
from .run_evidence import *  # noqa: F401,F403
from .runtime_spec import ToolSpec as RuntimeToolSpec  # noqa: F401
from .state import ToolSpec as ToolSpec  # noqa: F401

_FORWARD_REF_NAMESPACE = dict(globals())
for _model in (RuntimeStateSnapshot, BranchResumeSnapshot, BranchResult, CheckpointEnvelope, SuiteEvaluation):
    if hasattr(_model, "model_rebuild"):
        _model.model_rebuild(_types_namespace=_FORWARD_REF_NAMESPACE)
    else:
        _model.update_forward_refs(**_FORWARD_REF_NAMESPACE)

del _model, _FORWARD_REF_NAMESPACE
"""


@dataclass(frozen=True, slots=True)
class _BundleSpec:
    name: BundleProfile
    source_roots: tuple[str, ...]
    source_files: tuple[str, ...]
    optional_source_files: tuple[str, ...]
    resource_files: tuple[tuple[str, str], ...]
    generated_files: tuple[tuple[str, str], ...]
    capability_flags: tuple[str, ...]


_HARNESS_SOURCE_FILES = (
    "__init__.py",
    "authority/__init__.py",
    "authority/public_tasks.py",
    "contracts/epochs.py",
    "contracts/harness.py",
    "contracts/outcomes.py",
    "contracts/run_evidence.py",
    "core/__init__.py",
    "core/exceptions.py",
    "core/identity.py",
    "core/redaction.py",
    "core/versioning.py",
    "isolation/__init__.py",
    "isolation/commands.py",
    "isolation/replay.py",
    "isolation/workspaces.py",
    "repositories/__init__.py",
    "repositories/workspaces.py",
    "runtime/api/composite_compiler.py",
    "runtime/evidence.py",
    "runtime/harness_profile.py",
    "runtime/kernel/composite_artifacts.py",
    "runtime/kernel/composite_budget.py",
    "runtime/kernel/composite_provider.py",
    "runtime/kernel/composite_replay_provider.py",
    "runtime/kernel/composite_runtime.py",
    "runtime/kernel/openai_responses_provider.py",
    "runtime/kernel/repair_tools.py",
    "runtime/sdk/harness_entrypoint.py",
    "runtime/sdk/harness_executor.py",
    "runtime/sdk/harness_manifest.py",
    "runtime/sdk/harness_release_loader.py",
)

_HARNESS_OPTIONAL_SOURCE_FILES: tuple[str, ...] = ()

_HARNESS_RESOURCE_FILES = (
    (
        "templates/harness/composite_compiler_metadata.json",
        "templates/harness/composite_compiler_metadata.json",
    ),
    (
        "templates/harness/repo_repair_v1_two_actor_seed.json",
        "templates/harness/repo_repair_v1_two_actor_seed.json",
    ),
)

_HARNESS_GENERATED_FILES = (
    ("contracts/__init__.py", _MINIMAL_INIT_TEXT),
    ("runtime/__init__.py", _MINIMAL_INIT_TEXT),
    ("runtime/api/__init__.py", _MINIMAL_INIT_TEXT),
    ("runtime/kernel/__init__.py", _MINIMAL_INIT_TEXT),
    ("runtime/sdk/__init__.py", _HARNESS_SDK_INIT_TEXT),
    ("runtime_entry.py", _HARNESS_RUNTIME_ENTRY_TEXT),
    ("utils.py", _MINIMAL_UTILS_TEXT),
)

_LEGACY_SOURCE_ROOTS = (
    "authority",
    "isolation",
    "providers",
    "repositories",
    "runtime/api",
    "runtime/kernel",
    "runtime/langgraph",
    "runtime/tools",
    "storage/state_store",
    "tracing",
)

_LEGACY_SOURCE_FILES = (
    "__init__.py",
    "contracts/benchmarks.py",
    "contracts/branches.py",
    "contracts/checkpoints.py",
    "contracts/evidence.py",
    "contracts/epochs.py",
    "contracts/execution.py",
    "contracts/harness.py",
    "contracts/harness_actions.py",
    "contracts/outcomes.py",
    "contracts/providers.py",
    "contracts/protocol.py",
    "contracts/runtime.py",
    "contracts/runtime_spec.py",
    "contracts/run_evidence.py",
    "contracts/sessions.py",
    "contracts/side_effects.py",
    "contracts/state.py",
    "contracts/tracing.py",
    "contracts/verifiers.py",
    "core/__init__.py",
    "core/exceptions.py",
    "core/identity.py",
    "core/versioning.py",
    "runtime/__init__.py",
    "runtime/loader.py",
    "runtime/profile.py",
    "runtime/prompts.py",
    "runtime/sdk/__init__.py",
    "runtime/sdk/entrypoint.py",
    "runtime/sdk/harness_executor.py",
    "runtime/sdk/harness_manifest.py",
    "runtime/sdk/harness_release_loader.py",
    "storage/__init__.py",
    "storage/artifacts.py",
    "storage/run_store.py",
    "utils.py",
)

_LEGACY_RESOURCE_FILES = (
    ("runtime/sdk/defaults/runtime_profile.json", "runtime/sdk/defaults/runtime_profile.json"),
    (
        "templates/harness/composite_compiler_metadata.json",
        "templates/harness/composite_compiler_metadata.json",
    ),
    (
        "templates/harness/repo_repair_v1_two_actor_seed.json",
        "templates/harness/repo_repair_v1_two_actor_seed.json",
    ),
    ("templates/prompts/memory.span_summarize.json", "templates/prompts/memory.span_summarize.json"),
    ("templates/prompts/tool.spec_generate.json", "templates/prompts/tool.spec_generate.json"),
)

_LEGACY_GENERATED_FILES = (
    ("contracts/__init__.py", _LEGACY_CONTRACTS_INIT_TEXT),
    ("runtime_entry.py", _LEGACY_RUNTIME_ENTRY_TEXT),
)

_HARNESS_FORBIDDEN_PREFIXES = (
    "evaluation/",
    "factory/",
    "learning/",
    "oracle/",
    "providers/",
    "search/",
    "storage/",
    "tracing/",
    "runtime/host/",
    "runtime/langgraph/",
    "runtime/tools/",
)
_HARNESS_FORBIDDEN_FILES = frozenset(
    {
        "cli.py",
        "contracts/benchmarks.py",
        "contracts/branches.py",
        "contracts/checkpoints.py",
        "contracts/evidence.py",
        "contracts/execution.py",
        "contracts/harness_actions.py",
        "contracts/providers.py",
        "contracts/protocol.py",
        "contracts/runtime.py",
        "contracts/runtime_spec.py",
        "contracts/search.py",
        "contracts/sessions.py",
        "contracts/side_effects.py",
        "contracts/state.py",
        "contracts/tracing.py",
        "contracts/verifiers.py",
        "core/patches.py",
        "runtime/loader.py",
        "runtime/project.py",
        "runtime/prompts.py",
        "runtime/sdk/bundle.py",
        "runtime/sdk/entrypoint.py",
    }
)

_LEGACY_FORBIDDEN_ROOTS = (
    "evaluation",
    "factory",
    "learning",
    "search",
    "runtime/host",
)
_LEGACY_FORBIDDEN_FILES = frozenset(
    {
        "cli.py",
        "contracts/__init__.py",
        "contracts/factory.py",
        "contracts/search.py",
        "core/patches.py",
        "runtime/project.py",
        "runtime/sdk/bundle.py",
        "storage/factory_chat_store.py",
        "storage/runtime_session_store.py",
    }
)


def _package_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def kernel_manifest_path(runtime_dir: str | Path) -> Path:
    return Path(runtime_dir) / KERNEL_BUNDLE_DIR / KERNEL_MANIFEST_FILE


def _text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_profile(profile: str) -> BundleProfile:
    normalized = str(profile or "").strip().lower()
    if normalized not in {HARNESS_BUNDLE_PROFILE, LEGACY_BUNDLE_PROFILE}:
        raise ValueError(f"unsupported runtime bundle profile {profile!r}")
    return normalized  # type: ignore[return-value]


def _bundle_spec(profile: str) -> _BundleSpec:
    normalized = _normalize_profile(profile)
    if normalized == HARNESS_BUNDLE_PROFILE:
        return _BundleSpec(
            name=HARNESS_BUNDLE_PROFILE,
            source_roots=(),
            source_files=_HARNESS_SOURCE_FILES,
            optional_source_files=_HARNESS_OPTIONAL_SOURCE_FILES,
            resource_files=_HARNESS_RESOURCE_FILES,
            generated_files=_HARNESS_GENERATED_FILES,
            capability_flags=HARNESS_KERNEL_CAPABILITY_FLAGS,
        )
    return _BundleSpec(
        name=LEGACY_BUNDLE_PROFILE,
        source_roots=_LEGACY_SOURCE_ROOTS,
        source_files=_LEGACY_SOURCE_FILES,
        optional_source_files=(),
        resource_files=_LEGACY_RESOURCE_FILES,
        generated_files=_LEGACY_GENERATED_FILES,
        capability_flags=tuple(KERNEL_CAPABILITY_FLAGS),
    )


def _is_forbidden_source(rel_path: str, *, profile: BundleProfile) -> bool:
    normalized = rel_path.replace("\\", "/")
    if profile == HARNESS_BUNDLE_PROFILE:
        return normalized in _HARNESS_FORBIDDEN_FILES or any(
            normalized.startswith(prefix) for prefix in _HARNESS_FORBIDDEN_PREFIXES
        )
    if normalized in _LEGACY_FORBIDDEN_FILES:
        return True
    return any(
        normalized == root or normalized.startswith(f"{root}/")
        for root in _LEGACY_FORBIDDEN_ROOTS
    )


def _require_source_file(source_root: Path, rel_path: str, *, profile: BundleProfile) -> Path:
    normalized = rel_path.replace("\\", "/")
    if _is_forbidden_source(normalized, profile=profile):
        raise ValueError(f"{profile} bundle spec cannot include forbidden source {normalized!r}")
    path = source_root / normalized
    if not path.is_file():
        raise FileNotFoundError(f"kernel bundle source file is missing: {normalized}")
    return path


def _source_rel_paths(source_root: Path, spec: _BundleSpec) -> tuple[str, ...]:
    generated = {path for path, _text in spec.generated_files}
    rel_paths: set[str] = set(spec.source_files)
    for optional in spec.optional_source_files:
        if (source_root / optional).is_file():
            rel_paths.add(optional)
    for root_rel in spec.source_roots:
        root_path = source_root / root_rel
        if _is_forbidden_source(root_rel, profile=spec.name):
            raise ValueError(f"{spec.name} bundle spec cannot include forbidden root {root_rel!r}")
        if not root_path.is_dir():
            raise FileNotFoundError(f"kernel bundle source root is missing: {root_rel}")
        for path in sorted(root_path.rglob("*.py")):
            if path.is_file():
                rel_paths.add(path.relative_to(source_root).as_posix())
    rel_paths.difference_update(generated)
    for rel_path in rel_paths:
        _require_source_file(source_root, rel_path, profile=spec.name)
    return tuple(sorted(rel_paths))


def _resource_paths(source_root: Path, spec: _BundleSpec) -> tuple[tuple[Path, str], ...]:
    files: list[tuple[Path, str]] = []
    for source_rel, bundle_rel in spec.resource_files:
        source_path = source_root / source_rel
        if not source_path.is_file():
            raise FileNotFoundError(f"kernel bundle resource is missing: {source_rel}")
        files.append((source_path, bundle_rel))
    return tuple(files)


def _expected_manifest_files(source_root: Path, spec: _BundleSpec) -> dict[str, str]:
    files: dict[str, str] = {}
    for rel_path in _source_rel_paths(source_root, spec):
        files[f"{KERNEL_PACKAGE_NAME}/{rel_path}"] = file_digest(source_root / rel_path)
    for rel_path, text in spec.generated_files:
        files[f"{KERNEL_PACKAGE_NAME}/{rel_path}"] = _text_digest(text)
    for source_path, bundle_rel in _resource_paths(source_root, spec):
        files[f"{KERNEL_PACKAGE_NAME}/{bundle_rel}"] = file_digest(source_path)
    return dict(sorted(files.items()))


def _manifest_for_spec(source_root: Path, spec: _BundleSpec) -> HarnessKernelManifest:
    return HarnessKernelManifest(
        runtime_contract_version=RUNTIME_CONTRACT_VERSION,
        package_name=KERNEL_PACKAGE_NAME,
        entry_module=f"{KERNEL_PACKAGE_NAME}.runtime_entry",
        files=_expected_manifest_files(source_root, spec),
        capability_flags=spec.capability_flags,
    )


def preview_kernel_manifest(
    *,
    profile: BundleProfile = HARNESS_BUNDLE_PROFILE,
) -> HarnessKernelManifest:
    return _manifest_for_spec(_package_root(), _bundle_spec(profile))


def _copy_source_files(source_root: Path, package_root: Path, spec: _BundleSpec) -> None:
    for rel_path in _source_rel_paths(source_root, spec):
        destination = package_root / rel_path
        ensure_directory(destination.parent)
        shutil.copy2(source_root / rel_path, destination)


def _write_generated_files(package_root: Path, spec: _BundleSpec) -> None:
    for rel_path, text in spec.generated_files:
        destination = package_root / rel_path
        ensure_directory(destination.parent)
        destination.write_text(text, encoding="utf-8", newline="\n")


def _copy_resources(source_root: Path, package_root: Path, spec: _BundleSpec) -> None:
    for source_path, bundle_rel in _resource_paths(source_root, spec):
        destination = package_root / bundle_rel
        ensure_directory(destination.parent)
        shutil.copy2(source_path, destination)


def _actual_package_files(package_root: Path) -> dict[str, str]:
    return {
        path.relative_to(package_root.parent).as_posix(): file_digest(path)
        for path in sorted(package_root.rglob("*"))
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix.casefold() not in {".pyc", ".pyo"}
    }


def bundle_runtime_kernel(
    runtime_dir: str | Path,
    *,
    force: bool = False,
    profile: BundleProfile = HARNESS_BUNDLE_PROFILE,
) -> HarnessKernelManifest:
    spec = _bundle_spec(profile)
    runtime_path = Path(runtime_dir)
    bundle_root = runtime_path / KERNEL_BUNDLE_DIR
    package_root = bundle_root / KERNEL_PACKAGE_NAME
    source_root = _package_root()
    if force and bundle_root.exists():
        shutil.rmtree(bundle_root)
    elif package_root.exists():
        shutil.rmtree(package_root)
    ensure_directory(package_root)
    _copy_source_files(source_root, package_root, spec)
    _write_generated_files(package_root, spec)
    _copy_resources(source_root, package_root, spec)
    manifest = HarnessKernelManifest(
        runtime_contract_version=RUNTIME_CONTRACT_VERSION,
        package_name=KERNEL_PACKAGE_NAME,
        entry_module=f"{KERNEL_PACKAGE_NAME}.runtime_entry",
        files=_actual_package_files(package_root),
        capability_flags=spec.capability_flags,
    )
    manifest_path = bundle_root / KERNEL_MANIFEST_FILE
    ensure_directory(manifest_path.parent)
    manifest_path.write_text(
        json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return validate_kernel_bundle(runtime_path, profile=spec.name)


def _profile_from_manifest(manifest: HarnessKernelManifest) -> BundleProfile:
    flags = tuple(manifest.capability_flags)
    if flags == HARNESS_KERNEL_CAPABILITY_FLAGS:
        return HARNESS_BUNDLE_PROFILE
    if flags == tuple(KERNEL_CAPABILITY_FLAGS):
        return LEGACY_BUNDLE_PROFILE
    raise ValueError("kernel bundle capability flags do not identify a supported profile")


def validate_kernel_bundle(
    runtime_dir: str | Path,
    *,
    profile: BundleProfile | None = None,
) -> HarnessKernelManifest:
    runtime_path = Path(runtime_dir)
    bundle_root = (runtime_path / KERNEL_BUNDLE_DIR).resolve()
    manifest_path = bundle_root / KERNEL_MANIFEST_FILE
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise FileNotFoundError(f"kernel bundle manifest is missing or unsafe: {manifest_path}")
    try:
        raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = HarnessKernelManifest.model_validate(raw_manifest)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("kernel bundle manifest failed validation") from exc
    if manifest.runtime_contract_version != RUNTIME_CONTRACT_VERSION:
        raise ValueError(
            "kernel bundle contract version mismatch: "
            f"bundle={manifest.runtime_contract_version} expected={RUNTIME_CONTRACT_VERSION}"
        )
    if manifest.package_name != KERNEL_PACKAGE_NAME:
        raise ValueError(
            f"kernel bundle package mismatch: bundle={manifest.package_name!r} "
            f"expected={KERNEL_PACKAGE_NAME!r}"
        )
    actual_profile = _profile_from_manifest(manifest)
    if profile is not None and actual_profile != _normalize_profile(profile):
        raise ValueError(
            f"kernel bundle profile mismatch: bundle={actual_profile!r} expected={profile!r}"
        )
    spec = _bundle_spec(actual_profile)
    expected_paths = set(_expected_manifest_files(_package_root(), spec))
    if set(manifest.files) != expected_paths:
        raise ValueError("kernel bundle manifest does not match the exact profile closure")

    package_root = bundle_root / KERNEL_PACKAGE_NAME
    symlinks = [
        path.relative_to(package_root).as_posix()
        for path in package_root.rglob("*")
        if path.is_symlink()
    ]
    if symlinks:
        raise ValueError(
            "kernel bundle contains symbolic links: " + ", ".join(sorted(symlinks))
        )
    actual_paths = set(_actual_package_files(package_root))
    missing_paths = set(manifest.files) - actual_paths
    if missing_paths:
        raise FileNotFoundError(
            "kernel bundle file is missing: " + ", ".join(sorted(missing_paths))
        )
    unexpected_paths = actual_paths - set(manifest.files)
    if unexpected_paths:
        raise ValueError(
            "kernel bundle contains unmanifested or unexpected files: "
            + ", ".join(sorted(unexpected_paths))
        )
    for rel_path, expected_digest in sorted(manifest.files.items()):
        relative = PurePosixPath(rel_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"kernel bundle manifest path is unsafe: {rel_path!r}")
        file_path = (bundle_root / Path(*relative.parts)).resolve()
        if bundle_root != file_path and bundle_root not in file_path.parents:
            raise ValueError(f"kernel bundle manifest path escapes bundle: {rel_path!r}")
        if not file_path.is_file() or file_path.is_symlink():
            raise FileNotFoundError(f"kernel bundle file is missing or unsafe: {rel_path}")
        actual_digest = file_digest(file_path)
        if actual_digest != expected_digest:
            raise ValueError(
                f"kernel bundle digest mismatch for {rel_path!r}: "
                f"manifest={expected_digest} actual={actual_digest}"
            )
    entry_path = bundle_root.joinpath(*manifest.entry_module.split(".")).with_suffix(".py")
    if not entry_path.is_file():
        raise FileNotFoundError(f"kernel bundle entry module is missing: {manifest.entry_module}")
    return manifest


__all__ = [
    "BundleProfile",
    "bundle_runtime_kernel",
    "kernel_manifest_path",
    "preview_kernel_manifest",
    "validate_kernel_bundle",
]
