from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ...contracts.epochs import (
    DeploymentIdentity,
    PromotionMargins,
    SearchEnvelope,
    StopRule,
    TaskCeilings,
    TrustedToolAuthority,
)
from ...contracts.harness import (
    CompositeRunPlan,
    HarnessProtocol,
    RuntimeDependencyManifest,
)
from ...contracts.run_evidence import assert_no_resolved_credentials
from ...core.identity import evidence_digest
from ..harness_profile import HarnessDeploymentProfile, harness_deployment_profile_digest
from . import KERNEL_BUNDLE_DIR, KERNEL_MANIFEST_FILE, KERNEL_PACKAGE_NAME
from .harness_manifest import HARNESS_KERNEL_CAPABILITY_FLAGS, HarnessKernelManifest


HARNESS_RELEASE_SCHEMA_VERSION = "repo-repair-harness-release-v1"
ACTIVE_RELEASE_FILE = "active_release.json"
RELEASES_DIR = "releases"
PUBLIC_EVIDENCE_DIR = "public_release_evidence"
RUNTIME_DIR = "runtime"
RELEASE_MANIFEST_PATH = f"{PUBLIC_EVIDENCE_DIR}/release_manifest.json"

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CONTROLLED_ROOT_NAMES = frozenset(
    {".factory_chat", ".runtime_sessions", "controlled_development_and_evaluator_evidence"}
)
_FORBIDDEN_PUBLIC_KEYS = frozenset(
    {
        "artifact_value",
        "artifact_values",
        "canary",
        "canaries",
        "evaluation_contract",
        "evaluator",
        "evaluator_authority",
        "gold_patch",
        "hidden_check",
        "hidden_checks",
        "outcome_record",
        "outcome_records",
        "pre_call_context",
        "pre_call_contexts",
        "private_expected",
        "raw_context",
        "raw_contexts",
        "sealed_fixture",
        "sealed_value",
        "sealed_values",
    }
)
_SECRET_PATTERNS = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
_WINDOWS_ABSOLUTE_RE = re.compile(r"(?:^|[\s\"'(=])(?:[A-Za-z]:[\\/])")
_POSIX_ABSOLUTE_RE = re.compile(r"(?:^|[\s\"'(=])/(?!/)")


class HarnessReleaseLoadError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class RuntimeReleaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)


def _digest(value: str, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _DIGEST_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return normalized


def _identifier(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not _IDENTIFIER_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a nonempty portable identifier")
    return normalized


def _relative_path(value: str, field_name: str) -> str:
    normalized = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field_name} must be a safe release-relative path")
    return path.as_posix()


class RuntimeActiveReleasePointer(RuntimeReleaseModel):
    runtime_kind: Literal["harness"] = "harness"
    release_digest: str
    release_path: str
    manifest_digest: str

    @field_validator("release_digest", "manifest_digest")
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _digest(value, info.field_name)

    @field_validator("release_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _relative_path(value, "release_path")


class RuntimeCapabilityEpochProjection(RuntimeReleaseModel):
    runtime_contract_version: str
    epoch_id: str
    epoch_manifest_digest: str
    capability_epoch: Literal["repo-repair-v1"] = "repo-repair-v1"
    promotion_capable: Literal[True] = True
    task_manifest_digest: str
    development_split_digest: str
    deployment: DeploymentIdentity
    per_run_ceilings: TaskCeilings
    search_envelope: SearchEnvelope
    trusted_tools: tuple[TrustedToolAuthority, ...]
    mutation_surface: tuple[str, ...]
    promotion_margins: PromotionMargins
    stop_rule: StopRule

    @field_validator(
        "epoch_manifest_digest",
        "task_manifest_digest",
        "development_split_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _digest(value, info.field_name)

    @field_validator("epoch_id")
    @classmethod
    def validate_epoch_id(cls, value: str) -> str:
        return _identifier(value, "epoch_id")


class RuntimeHarnessProfileProjection(RuntimeReleaseModel):
    runtime_kind: Literal["harness"] = "harness"
    deployment: DeploymentIdentity
    profile_digest: str
    profile: dict[str, Any]

    @field_validator("profile_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _digest(value, "profile_digest")

    @model_validator(mode="after")
    def validate_profile_digest(self) -> "RuntimeHarnessProfileProjection":
        profile = HarnessDeploymentProfile.model_validate(self.profile)
        profile.validate_deployment_identity(self.deployment)
        digest = harness_deployment_profile_digest(profile)
        if self.profile_digest != digest:
            raise ValueError("runtime profile digest mismatch")
        return self


class RuntimePublicEvidenceIndex(RuntimeReleaseModel):
    index_digest: str = ""
    protocol_source_digest: str
    compiled_semantic_digest: str
    dependency_manifest_digest: str
    epoch_manifest_digest: str
    profile_digest: str
    artifacts: dict[str, str]

    @field_validator(
        "protocol_source_digest",
        "compiled_semantic_digest",
        "dependency_manifest_digest",
        "epoch_manifest_digest",
        "profile_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _digest(value, info.field_name)

    @field_validator("artifacts")
    @classmethod
    def validate_artifacts(cls, value: dict[str, str]) -> dict[str, str]:
        normalized = {
            _relative_path(path, "evidence artifact path"): _digest(
                digest,
                "evidence artifact digest",
            )
            for path, digest in value.items()
        }
        return dict(sorted(normalized.items()))

    @model_validator(mode="after")
    def bind_digest(self) -> "RuntimePublicEvidenceIndex":
        payload = self.model_dump(mode="python", exclude={"index_digest"})
        computed = evidence_digest({"kind": "public-release-evidence-index-v1", **payload})
        if self.index_digest and self.index_digest != computed:
            raise ValueError("evidence index digest mismatch")
        if not self.index_digest:
            object.__setattr__(self, "index_digest", computed)
        return self


class RuntimeHarnessReleaseManifest(RuntimeReleaseModel):
    schema_version: Literal[HARNESS_RELEASE_SCHEMA_VERSION] = HARNESS_RELEASE_SCHEMA_VERSION
    runtime_kind: Literal["harness"] = "harness"
    release_digest: str
    manifest_digest: str = ""
    epoch_id: str
    epoch_manifest_digest: str
    deployment: DeploymentIdentity
    protocol_source_digest: str
    compiled_semantic_digest: str
    dependency_manifest_digest: str
    profile_digest: str
    gate0_status: Literal["not_run", "completed"] = "not_run"
    pilot_status: Literal["not_run"] = "not_run"
    file_digests: dict[str, str]

    @field_validator(
        "release_digest",
        "epoch_manifest_digest",
        "protocol_source_digest",
        "compiled_semantic_digest",
        "dependency_manifest_digest",
        "profile_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _digest(value, info.field_name)

    @field_validator("epoch_id")
    @classmethod
    def validate_epoch_id(cls, value: str) -> str:
        return _identifier(value, "epoch_id")

    @field_validator("file_digests")
    @classmethod
    def validate_files(cls, value: dict[str, str]) -> dict[str, str]:
        normalized = {
            _relative_path(path, "release file path"): _digest(
                digest,
                "release file digest",
            )
            for path, digest in value.items()
        }
        if RELEASE_MANIFEST_PATH in normalized:
            raise ValueError("release manifest cannot include its own file digest")
        return dict(sorted(normalized.items()))

    @model_validator(mode="after")
    def bind_manifest(self) -> "RuntimeHarnessReleaseManifest":
        expected_release = evidence_digest(
            {"kind": HARNESS_RELEASE_SCHEMA_VERSION, "files": self.file_digests}
        )
        if self.release_digest != expected_release:
            raise ValueError("release_digest does not match relative file digests")
        payload = self.model_dump(mode="python", exclude={"manifest_digest"})
        computed = evidence_digest({"kind": "harness-release-manifest-v1", **payload})
        if self.manifest_digest and self.manifest_digest != computed:
            raise ValueError("release manifest_digest mismatch")
        if not self.manifest_digest:
            object.__setattr__(self, "manifest_digest", computed)
        return self


class RuntimeReleasedIdentity(RuntimeReleaseModel):
    runtime_kind: Literal["harness"]
    epoch_manifest_digest: str
    deployment: DeploymentIdentity
    protocol_source_digest: str
    compiled_semantic_digest: str
    representative_task_envelope_digest: str
    dependency_manifest_digest: str
    profile_digest: str

    @field_validator(
        "epoch_manifest_digest",
        "protocol_source_digest",
        "compiled_semantic_digest",
        "representative_task_envelope_digest",
        "dependency_manifest_digest",
        "profile_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _digest(value, info.field_name)


@dataclass(frozen=True, slots=True)
class LoadedHarnessRelease:
    project_root: Path
    generation_path: Path
    pointer: RuntimeActiveReleasePointer
    manifest: RuntimeHarnessReleaseManifest
    epoch: RuntimeCapabilityEpochProjection
    profile: RuntimeHarnessProfileProjection
    protocol: HarnessProtocol
    dependencies: RuntimeDependencyManifest
    representative_plan: CompositeRunPlan
    runtime_identity: RuntimeReleasedIdentity
    evidence_index: RuntimePublicEvidenceIndex


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HarnessReleaseLoadError("release_json_invalid", f"invalid JSON: {path.name}") from exc


def _read_json_object(path: Path) -> dict[str, Any]:
    value = _read_json(path)
    if not isinstance(value, Mapping):
        raise HarnessReleaseLoadError("release_json_invalid", f"{path.name} must contain an object")
    return dict(value)


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _generation_file_digests(generation: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in sorted(generation.rglob("*")):
        if path.is_symlink():
            raise HarnessReleaseLoadError(
                "release_symlink_forbidden",
                "immutable release generations may not contain symlinks",
            )
        if path.is_dir() and path.name in _CONTROLLED_ROOT_NAMES:
            raise HarnessReleaseLoadError(
                "controlled_state_in_release",
                "controlled state may not be nested in an immutable release",
            )
        if not path.is_file():
            continue
        relative = path.relative_to(generation).as_posix()
        if relative == RELEASE_MANIFEST_PATH:
            continue
        safe_relative = _relative_path(relative, "release file path")
        files[safe_relative] = _file_digest(path)
    return dict(sorted(files.items()))


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def _looks_like_absolute_path(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return False
    return bool(
        _WINDOWS_ABSOLUTE_RE.search(stripped)
        or "file://" in stripped.casefold()
        or _POSIX_ABSOLUTE_RE.search(stripped)
    )


def _scan_public_value(
    value: Any,
    *,
    forbidden_markers: Sequence[str],
    path: str = "root",
) -> None:
    assert_no_resolved_credentials(value)
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = _normalized_key(str(raw_key))
            if key in _FORBIDDEN_PUBLIC_KEYS:
                raise HarnessReleaseLoadError(
                    "release_authority_leak",
                    f"forbidden public release field at {path}.{raw_key}",
                )
            if key.startswith(("sealed_", "evaluator_", "canary_")):
                raise HarnessReleaseLoadError(
                    "release_authority_leak",
                    f"forbidden authority field at {path}.{raw_key}",
                )
            _scan_public_value(
                item,
                forbidden_markers=forbidden_markers,
                path=f"{path}.{raw_key}",
            )
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            _scan_public_value(
                item,
                forbidden_markers=forbidden_markers,
                path=f"{path}[{index}]",
            )
        return
    if isinstance(value, str):
        if any(marker and marker in value for marker in forbidden_markers):
            raise HarnessReleaseLoadError(
                "release_canary_detected",
                "forbidden marker detected in immutable release",
            )
        if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
            raise HarnessReleaseLoadError(
                "release_credential_detected",
                "resolved credential material detected in immutable release",
            )
        if _looks_like_absolute_path(value):
            raise HarnessReleaseLoadError(
                "release_absolute_path_detected",
                "absolute source path detected in immutable release",
            )


def _scan_generation(generation: Path, *, forbidden_markers: Sequence[str]) -> None:
    for path in sorted(generation.rglob("*")):
        if not path.is_file():
            continue
        raw = path.read_bytes()
        if any(str(marker).encode("utf-8") in raw for marker in forbidden_markers if marker):
            raise HarnessReleaseLoadError(
                "release_canary_detected",
                "forbidden marker detected in immutable release",
            )
        suffix = path.suffix.casefold()
        if suffix == ".json":
            _scan_public_value(_read_json(path), forbidden_markers=forbidden_markers)
        elif suffix == ".jsonl":
            try:
                lines = raw.decode("utf-8").splitlines()
            except UnicodeDecodeError as exc:
                raise HarnessReleaseLoadError(
                    "release_json_invalid",
                    "release JSONL is not UTF-8",
                ) from exc
            for line in lines:
                if line.strip():
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise HarnessReleaseLoadError(
                            "release_json_invalid",
                            "release JSONL contains an invalid record",
                        ) from exc
                    _scan_public_value(value, forbidden_markers=forbidden_markers)
        elif suffix == ".md":
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise HarnessReleaseLoadError(
                    "release_text_invalid",
                    "release Markdown is not UTF-8",
                ) from exc
            _scan_public_value(text, forbidden_markers=forbidden_markers)


def _required_layout() -> frozenset[str]:
    return frozenset(
        {
            RELEASE_MANIFEST_PATH,
            f"{PUBLIC_EVIDENCE_DIR}/capability_epoch_public.json",
            f"{PUBLIC_EVIDENCE_DIR}/protocol/source.json",
            f"{PUBLIC_EVIDENCE_DIR}/protocol/compiled_plan.json",
            f"{PUBLIC_EVIDENCE_DIR}/protocol/consumed_field_liveness_manifest.json",
            f"{PUBLIC_EVIDENCE_DIR}/runtime/dependency_manifest.json",
            f"{PUBLIC_EVIDENCE_DIR}/search/transaction_lineage_public.jsonl",
            f"{PUBLIC_EVIDENCE_DIR}/search/selection_decisions_public.jsonl",
            f"{PUBLIC_EVIDENCE_DIR}/search/capability_authority.json",
            f"{PUBLIC_EVIDENCE_DIR}/gate0_preregistration.json",
            f"{PUBLIC_EVIDENCE_DIR}/gate0_report.json",
            f"{PUBLIC_EVIDENCE_DIR}/pilot_summary.json",
            f"{PUBLIC_EVIDENCE_DIR}/limitations.md",
            f"{PUBLIC_EVIDENCE_DIR}/evidence_index.json",
            f"{RUNTIME_DIR}/harness_protocol.json",
            f"{RUNTIME_DIR}/representative_composite_plan.json",
            f"{RUNTIME_DIR}/consumed_field_liveness_manifest.json",
            f"{RUNTIME_DIR}/runtime_dependency_manifest.json",
            f"{RUNTIME_DIR}/capability_epoch_public.json",
            f"{RUNTIME_DIR}/runtime_profile.json",
            f"{RUNTIME_DIR}/runtime_identity.json",
            f"{RUNTIME_DIR}/evidence_index.json",
            f"{RUNTIME_DIR}/{KERNEL_BUNDLE_DIR}/{KERNEL_MANIFEST_FILE}",
        }
    )


def _validate_kernel_bundle(runtime: Path) -> None:
    bundle_root = (runtime / KERNEL_BUNDLE_DIR).resolve()
    manifest_path = bundle_root / KERNEL_MANIFEST_FILE
    try:
        manifest = HarnessKernelManifest.model_validate(_read_json_object(manifest_path))
    except Exception as exc:
        raise HarnessReleaseLoadError(
            "kernel_bundle_invalid",
            "runtime kernel manifest failed validation",
        ) from exc
    if manifest.package_name != KERNEL_PACKAGE_NAME:
        raise HarnessReleaseLoadError(
            "kernel_bundle_invalid",
            "runtime kernel package identity differs from the harness SDK",
        )
    if manifest.entry_module != f"{KERNEL_PACKAGE_NAME}.runtime_entry":
        raise HarnessReleaseLoadError(
            "kernel_bundle_invalid",
            "runtime kernel entry module differs from the harness SDK",
        )
    if tuple(manifest.capability_flags) != HARNESS_KERNEL_CAPABILITY_FLAGS:
        raise HarnessReleaseLoadError(
            "kernel_bundle_invalid",
            "runtime kernel capabilities are not the harness-only profile",
        )
    for relative_text, expected_digest in manifest.files.items():
        relative = PurePosixPath(relative_text)
        if relative.is_absolute() or ".." in relative.parts:
            raise HarnessReleaseLoadError("kernel_bundle_invalid", "unsafe kernel manifest path")
        path = (bundle_root / Path(*relative.parts)).resolve()
        if path != bundle_root and bundle_root not in path.parents:
            raise HarnessReleaseLoadError("kernel_bundle_invalid", "kernel manifest path escaped")
        if not path.is_file() or path.is_symlink() or _file_digest(path) != expected_digest:
            raise HarnessReleaseLoadError(
                "kernel_bundle_invalid",
                "runtime kernel file differs from its manifest",
            )


def _load_typed_runtime(
    generation: Path,
    manifest: RuntimeHarnessReleaseManifest,
) -> tuple[
    RuntimeCapabilityEpochProjection,
    RuntimeHarnessProfileProjection,
    HarnessProtocol,
    RuntimeDependencyManifest,
    CompositeRunPlan,
    RuntimeReleasedIdentity,
    RuntimePublicEvidenceIndex,
]:
    evidence = generation / PUBLIC_EVIDENCE_DIR
    runtime = generation / RUNTIME_DIR
    try:
        protocol = HarnessProtocol.model_validate(_read_json_object(runtime / "harness_protocol.json"))
        dependencies = RuntimeDependencyManifest.model_validate(
            _read_json_object(runtime / "runtime_dependency_manifest.json")
        )
        representative_plan = CompositeRunPlan.model_validate(
            _read_json_object(runtime / "representative_composite_plan.json")
        )
        epoch = RuntimeCapabilityEpochProjection.model_validate(
            _read_json_object(runtime / "capability_epoch_public.json")
        )
        profile = RuntimeHarnessProfileProjection.model_validate(
            _read_json_object(runtime / "runtime_profile.json")
        )
        runtime_identity = RuntimeReleasedIdentity.model_validate(
            _read_json_object(runtime / "runtime_identity.json")
        )
        index = RuntimePublicEvidenceIndex.model_validate(
            _read_json_object(runtime / "evidence_index.json")
        )
    except HarnessReleaseLoadError:
        raise
    except Exception as exc:
        raise HarnessReleaseLoadError(
            "release_contract_invalid",
            "released runtime contract validation failed",
        ) from exc
    if protocol.source_digest() != manifest.protocol_source_digest:
        raise HarnessReleaseLoadError("release_identity_mismatch", "protocol identity changed")
    if dependencies.manifest_digest() != manifest.dependency_manifest_digest:
        raise HarnessReleaseLoadError("release_identity_mismatch", "dependency identity changed")
    if (
        representative_plan.source_protocol_digest != protocol.source_digest()
        or representative_plan.compiled_semantic_digest != manifest.compiled_semantic_digest
        or representative_plan.dependency_manifest != dependencies
        or representative_plan.dependency_manifest_digest != dependencies.manifest_digest()
    ):
        raise HarnessReleaseLoadError(
            "release_identity_mismatch",
            "representative plan crossed protocol or dependency identity",
        )
    if (
        epoch.epoch_id != manifest.epoch_id
        or epoch.epoch_manifest_digest != manifest.epoch_manifest_digest
        or epoch.deployment != manifest.deployment
    ):
        raise HarnessReleaseLoadError("release_identity_mismatch", "epoch identity changed")
    if profile.deployment != manifest.deployment or profile.profile_digest != manifest.profile_digest:
        raise HarnessReleaseLoadError("release_identity_mismatch", "profile identity changed")
    if (
        runtime_identity.epoch_manifest_digest != manifest.epoch_manifest_digest
        or runtime_identity.deployment != manifest.deployment
        or runtime_identity.protocol_source_digest != manifest.protocol_source_digest
        or runtime_identity.compiled_semantic_digest != manifest.compiled_semantic_digest
        or runtime_identity.representative_task_envelope_digest
        != representative_plan.task_envelope_digest
        or runtime_identity.dependency_manifest_digest != manifest.dependency_manifest_digest
        or runtime_identity.profile_digest != manifest.profile_digest
    ):
        raise HarnessReleaseLoadError("release_identity_mismatch", "runtime identity graph crossed")
    if (
        index.protocol_source_digest != manifest.protocol_source_digest
        or index.compiled_semantic_digest != manifest.compiled_semantic_digest
        or index.dependency_manifest_digest != manifest.dependency_manifest_digest
        or index.epoch_manifest_digest != manifest.epoch_manifest_digest
        or index.profile_digest != manifest.profile_digest
    ):
        raise HarnessReleaseLoadError("release_identity_mismatch", "evidence identity graph crossed")
    for path, expected_digest in index.artifacts.items():
        artifact_path = evidence / Path(*PurePosixPath(path).parts)
        if not artifact_path.is_file() or _file_digest(artifact_path) != expected_digest:
            raise HarnessReleaseLoadError(
                "release_evidence_invalid",
                "public release evidence differs from its evidence index",
            )
    duplicate_pairs = (
        (evidence / "protocol/source.json", runtime / "harness_protocol.json"),
        (evidence / "protocol/compiled_plan.json", runtime / "representative_composite_plan.json"),
        (
            evidence / "protocol/consumed_field_liveness_manifest.json",
            runtime / "consumed_field_liveness_manifest.json",
        ),
        (evidence / "runtime/dependency_manifest.json", runtime / "runtime_dependency_manifest.json"),
        (evidence / "capability_epoch_public.json", runtime / "capability_epoch_public.json"),
        (evidence / "evidence_index.json", runtime / "evidence_index.json"),
    )
    if any(public.read_bytes() != runtime_copy.read_bytes() for public, runtime_copy in duplicate_pairs):
        raise HarnessReleaseLoadError(
            "release_duplicate_mismatch",
            "runtime and public release artifacts differ",
        )
    dependency_tools = {
        tool.tool_id: (tool.implementation_digest, tool.policy_digest)
        for tool in dependencies.trusted_tools
    }
    epoch_tools = {
        tool.tool_id: (tool.implementation_digest, tool.policy_digest)
        for tool in epoch.trusted_tools
    }
    if dependency_tools != epoch_tools:
        raise HarnessReleaseLoadError(
            "release_tool_authority_mismatch",
            "released tool dependencies differ from epoch authority",
        )
    return (
        epoch,
        profile,
        protocol,
        dependencies,
        representative_plan,
        runtime_identity,
        index,
    )


def validate_harness_release_generation(
    generation_path: str | Path,
    *,
    forbidden_markers: Sequence[str] = (),
) -> tuple[
    RuntimeHarnessReleaseManifest,
    RuntimeCapabilityEpochProjection,
    RuntimeHarnessProfileProjection,
    HarnessProtocol,
    RuntimeDependencyManifest,
    CompositeRunPlan,
    RuntimeReleasedIdentity,
    RuntimePublicEvidenceIndex,
]:
    generation = Path(generation_path).expanduser().resolve()
    if not generation.is_dir() or generation.is_symlink():
        raise HarnessReleaseLoadError("release_missing", "release generation is missing or unsafe")
    if generation.parent.name != RELEASES_DIR:
        raise HarnessReleaseLoadError(
            "release_path_invalid",
            "release generation is outside the immutable release root",
        )
    manifest_path = generation / RELEASE_MANIFEST_PATH
    try:
        manifest = RuntimeHarnessReleaseManifest.model_validate(_read_json_object(manifest_path))
    except HarnessReleaseLoadError:
        raise
    except Exception as exc:
        raise HarnessReleaseLoadError(
            "release_manifest_invalid",
            "release manifest failed validation",
        ) from exc
    if generation.name != manifest.release_digest:
        raise HarnessReleaseLoadError(
            "release_identity_mismatch",
            "release directory name differs from content identity",
        )
    actual_digests = _generation_file_digests(generation)
    if actual_digests != manifest.file_digests:
        raise HarnessReleaseLoadError(
            "release_files_changed",
            "release files differ from the immutable manifest",
        )
    missing = sorted(path for path in _required_layout() if not (generation / path).is_file())
    if missing:
        raise HarnessReleaseLoadError(
            "release_layout_incomplete",
            "release generation is missing required runtime artifacts",
        )
    typed = _load_typed_runtime(generation, manifest)
    _scan_generation(generation, forbidden_markers=tuple(forbidden_markers))
    _validate_kernel_bundle(generation / RUNTIME_DIR)
    return (manifest, *typed)


def load_active_harness_release(
    project_root: str | Path,
    *,
    forbidden_markers: Sequence[str] = (),
) -> LoadedHarnessRelease:
    root = Path(project_root).expanduser().resolve()
    pointer_path = root / ACTIVE_RELEASE_FILE
    if not pointer_path.is_file() or pointer_path.is_symlink():
        raise HarnessReleaseLoadError("active_release_missing", "factory project has no active release")
    raw_pointer = _read_json_object(pointer_path)
    if raw_pointer.get("runtime_kind") != "harness":
        raise HarnessReleaseLoadError(
            "unsupported_runtime_kind",
            "the active release is not a repo-repair harness runtime",
        )
    try:
        assert_no_resolved_credentials(raw_pointer)
        pointer = RuntimeActiveReleasePointer.model_validate(raw_pointer)
    except Exception as exc:
        raise HarnessReleaseLoadError(
            "active_release_invalid",
            "active release pointer failed validation",
        ) from exc
    expected_relative = (PurePosixPath(RELEASES_DIR) / pointer.release_digest).as_posix()
    if pointer.release_path != expected_relative:
        raise HarnessReleaseLoadError(
            "active_release_path_mismatch",
            "active release path is not its content-addressed generation",
        )
    generation = (root / Path(*PurePosixPath(pointer.release_path).parts)).resolve()
    expected_generation = (root / RELEASES_DIR / pointer.release_digest).resolve()
    if generation != expected_generation or generation.is_symlink():
        raise HarnessReleaseLoadError(
            "active_release_path_mismatch",
            "active release generation escaped its immutable release root",
        )
    (
        manifest,
        epoch,
        profile,
        protocol,
        dependencies,
        representative_plan,
        runtime_identity,
        evidence_index,
    ) = validate_harness_release_generation(
        generation,
        forbidden_markers=forbidden_markers,
    )
    if (
        pointer.release_digest != manifest.release_digest
        or pointer.manifest_digest != manifest.manifest_digest
    ):
        raise HarnessReleaseLoadError(
            "active_release_identity_mismatch",
            "active pointer and release manifest identities differ",
        )
    return LoadedHarnessRelease(
        project_root=root,
        generation_path=generation,
        pointer=pointer,
        manifest=manifest,
        epoch=epoch,
        profile=profile,
        protocol=protocol,
        dependencies=dependencies,
        representative_plan=representative_plan,
        runtime_identity=runtime_identity,
        evidence_index=evidence_index,
    )


__all__ = [
    "ACTIVE_RELEASE_FILE",
    "HARNESS_RELEASE_SCHEMA_VERSION",
    "HarnessReleaseLoadError",
    "LoadedHarnessRelease",
    "PUBLIC_EVIDENCE_DIR",
    "RELEASES_DIR",
    "RELEASE_MANIFEST_PATH",
    "RUNTIME_DIR",
    "RuntimeActiveReleasePointer",
    "RuntimeCapabilityEpochProjection",
    "RuntimeHarnessProfileProjection",
    "RuntimeHarnessReleaseManifest",
    "RuntimePublicEvidenceIndex",
    "RuntimeReleasedIdentity",
    "load_active_harness_release",
    "validate_harness_release_generation",
]
