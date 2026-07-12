from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from ..contracts.harness import CompositeRunPlan, HarnessProtocol, RuntimeDependencyManifest
from ..contracts.run_evidence import assert_no_resolved_credentials
from ..runtime.sdk.bundle import bundle_runtime_kernel, validate_kernel_bundle
from ..utils import file_digest
from .harness_release_contracts import (
    ActiveReleasePointer,
    CapabilityEpochPublicProjection,
    Gate0CompletedReport,
    Gate0NotRunReport,
    Gate0PreregistrationPublic,
    HARNESS_RELEASE_SCHEMA_VERSION,
    HarnessReleaseManifest,
    HarnessReleaseRequest,
    HarnessRuntimeProfileProjection,
    MaterializedHarnessRelease,
    PilotNotRunSummary,
    PublicEvidenceIndex,
)


RELEASES_DIR = "releases"
ACTIVE_RELEASE_FILE = "active_release.json"
PUBLIC_EVIDENCE_DIR = "public_release_evidence"
RUNTIME_DIR = "runtime"
RELEASE_MANIFEST_PATH = f"{PUBLIC_EVIDENCE_DIR}/release_manifest.json"
_STAGING_DIR = ".release_staging"
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


def materialize_harness_release(
    *,
    project_root: str | Path,
    request: HarnessReleaseRequest,
) -> MaterializedHarnessRelease:
    """Build and validate an immutable generation without advancing factory state."""

    if request.runtime_kind != "harness":
        raise ValueError("repo-repair-v1 exports only runtime_kind='harness'")
    root = Path(project_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    releases_root = root / RELEASES_DIR
    staging_root = root / _STAGING_DIR
    releases_root.mkdir(parents=True, exist_ok=True)
    staging_root.mkdir(parents=True, exist_ok=True)
    stage = staging_root / f"generation-{uuid.uuid4().hex}"
    stage.mkdir()
    try:
        _write_generation(stage, request)
        file_digests = _generation_file_digests(stage, exclude={RELEASE_MANIFEST_PATH})
        release_digest = _release_digest(file_digests)
        profile_projection = HarnessRuntimeProfileProjection.from_profile(
            request.deployment_profile,
            request.deployment,
        )
        manifest = HarnessReleaseManifest(
            release_digest=release_digest,
            epoch_id=request.epoch.epoch_id,
            epoch_manifest_digest=request.epoch.epoch_manifest_digest,
            deployment=request.deployment,
            protocol_source_digest=request.selected_protocol.source_digest(),
            compiled_semantic_digest=request.representative_plan.compiled_semantic_digest,
            dependency_manifest_digest=request.dependency_manifest.manifest_digest(),
            profile_digest=profile_projection.profile_digest,
            gate0_status=request.gate0_report.status,
            file_digests=file_digests,
        )
        _write_json(stage / RELEASE_MANIFEST_PATH, manifest.model_dump(mode="json"))
        validate_harness_generation(stage)
        final = releases_root / release_digest
        if final.exists():
            existing = validate_harness_generation(final)
            if existing != manifest:
                raise ValueError("existing content-addressed generation differs from staged release")
            shutil.rmtree(stage)
        else:
            try:
                stage.replace(final)
            except FileExistsError:
                existing = validate_harness_generation(final)
                if existing != manifest:
                    raise ValueError("concurrent release generation digest collision")
                shutil.rmtree(stage, ignore_errors=True)
        validated = validate_harness_generation(final)
        return MaterializedHarnessRelease(
            project_root=str(root),
            generation_path=str(final),
            manifest=validated,
        )
    except Exception:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        raise


def advance_active_release(
    *,
    project_root: str | Path,
    materialized: MaterializedHarnessRelease,
) -> ActiveReleasePointer:
    """Validate an existing generation and atomically advance only the pointer."""

    root = Path(project_root).expanduser().resolve()
    if Path(materialized.project_root).resolve() != root:
        raise ValueError("materialized release belongs to another factory project")
    generation = Path(materialized.generation_path).resolve()
    expected = (root / RELEASES_DIR / materialized.manifest.release_digest).resolve()
    if generation != expected:
        raise ValueError("materialized generation path is not content-addressed under this project")
    manifest = validate_harness_generation(generation)
    if manifest != materialized.manifest:
        raise ValueError("materialized release manifest changed before pointer advancement")
    pointer = ActiveReleasePointer(
        release_digest=manifest.release_digest,
        release_path=f"{RELEASES_DIR}/{manifest.release_digest}",
        manifest_digest=manifest.manifest_digest,
    )
    _atomic_write_json(root / ACTIVE_RELEASE_FILE, pointer.model_dump(mode="json"))
    return pointer


def publish_harness_release(
    *,
    project_root: str | Path,
    request: HarnessReleaseRequest,
    before_pointer_advance: Callable[[MaterializedHarnessRelease], None] | None = None,
) -> tuple[MaterializedHarnessRelease, ActiveReleasePointer]:
    """Convenience publication: materialize, optional transaction hook, advance."""

    materialized = materialize_harness_release(project_root=project_root, request=request)
    if before_pointer_advance is not None:
        before_pointer_advance(materialized)
    pointer = advance_active_release(
        project_root=project_root,
        materialized=materialized,
    )
    return materialized, pointer


def load_active_release_pointer(project_root: str | Path) -> ActiveReleasePointer | None:
    path = Path(project_root).expanduser().resolve() / ACTIVE_RELEASE_FILE
    if not path.is_file():
        return None
    return ActiveReleasePointer.model_validate(json.loads(path.read_text(encoding="utf-8")))


def validate_harness_generation(
    generation_path: str | Path,
) -> HarnessReleaseManifest:
    generation = Path(generation_path).expanduser().resolve()
    if not generation.is_dir():
        raise FileNotFoundError(f"release generation is missing: {generation}")
    if generation.parent.name != RELEASES_DIR and generation.parent.name != _STAGING_DIR:
        raise ValueError("release generation is outside an approved generation root")
    for path in generation.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"release generations may not contain symlinks: {path}")
        if path.is_dir() and path.name in _CONTROLLED_ROOT_NAMES:
            raise ValueError(f"controlled state cannot be nested in a release: {path.name}")
    manifest_path = generation / RELEASE_MANIFEST_PATH
    if not manifest_path.is_file():
        raise FileNotFoundError("release manifest is missing")
    manifest = HarnessReleaseManifest.model_validate(
        json.loads(manifest_path.read_text(encoding="utf-8"))
    )
    if generation.parent.name == RELEASES_DIR and generation.name != manifest.release_digest:
        raise ValueError("release directory name does not match its content digest")
    actual = _generation_file_digests(generation, exclude={RELEASE_MANIFEST_PATH})
    if actual != manifest.file_digests:
        missing = sorted(set(manifest.file_digests) - set(actual))
        unexpected = sorted(set(actual) - set(manifest.file_digests))
        changed = sorted(
            path
            for path in set(actual) & set(manifest.file_digests)
            if actual[path] != manifest.file_digests[path]
        )
        raise ValueError(
            f"release file validation failed: missing={missing}, unexpected={unexpected}, changed={changed}"
        )
    if _release_digest(actual) != manifest.release_digest:
        raise ValueError("release content digest changed")
    _validate_required_layout(generation)
    _validate_typed_release_files(generation, manifest)
    _scan_generation(generation)
    validate_kernel_bundle(generation / RUNTIME_DIR)
    validate_source_hidden_runtime_bundle(generation / RUNTIME_DIR)
    return manifest


def validate_source_hidden_runtime_bundle(runtime_dir: str | Path) -> None:
    runtime = Path(runtime_dir).resolve()
    bundle_root = runtime / "runtime_sdk"
    script = (
        "import sys; "
        f"sys.path.insert(0, {str(bundle_root)!r}); "
        "from agintor_runtime.contracts.harness import HarnessProtocol, CompositeRunPlan; "
        "from agintor_runtime.runtime.api.composite_compiler import load_canonical_harness_seed; "
        "from agintor_runtime.runtime.kernel.composite_runtime import CompositeRuntime; "
        "seed = load_canonical_harness_seed(); "
        "assert isinstance(seed.protocol, HarnessProtocol); "
        "print(seed.reference.source_protocol_digest)"
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script],
        cwd=str(runtime),
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        shell=False,
        timeout=30.0,
        check=False,
        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0,
    )
    if completed.returncode != 0:
        raise ValueError(f"source-hidden runtime bundle import failed: {completed.stderr[-2000:]}")


def _write_generation(stage: Path, request: HarnessReleaseRequest) -> None:
    evidence = stage / PUBLIC_EVIDENCE_DIR
    runtime = stage / RUNTIME_DIR
    protocol_payload = request.selected_protocol.model_dump(mode="json", exclude_none=True)
    plan_payload = request.representative_plan.model_dump(mode="json", exclude_none=True)
    liveness_payload = request.representative_plan.liveness_manifest.model_dump(
        mode="json", exclude_none=True
    )
    dependency_payload = request.dependency_manifest.model_dump(mode="json", exclude_none=True)
    epoch_projection = CapabilityEpochPublicProjection.from_epoch(request.epoch)
    profile_projection = HarnessRuntimeProfileProjection.from_profile(
        request.deployment_profile,
        request.deployment,
    )
    capability_payload = {
        "search_execution_mode": request.search_execution_mode,
        "capability_promotion_authorized": (
            request.capability_promotion_authorized
        ),
        "capability_promotion_reason": request.capability_promotion_reason,
    }
    public_payloads = [
        protocol_payload,
        plan_payload,
        liveness_payload,
        dependency_payload,
        epoch_projection.model_dump(mode="json"),
        profile_projection.model_dump(mode="json"),
        [record.model_dump(mode="json") for record in request.search_lineage],
        [decision.model_dump(mode="json") for decision in request.selection_decisions],
        request.gate0_preregistration.model_dump(mode="json"),
        request.gate0_report.model_dump(mode="json"),
        request.pilot_summary.model_dump(mode="json"),
        list(request.limitations),
        capability_payload,
    ]
    for payload in public_payloads:
        _scan_public_value(payload)

    _write_json(evidence / "protocol/source.json", protocol_payload)
    _write_json(evidence / "protocol/compiled_plan.json", plan_payload)
    _write_json(evidence / "protocol/consumed_field_liveness_manifest.json", liveness_payload)
    _write_json(evidence / "runtime/dependency_manifest.json", dependency_payload)
    _write_json(evidence / "capability_epoch_public.json", epoch_projection.model_dump(mode="json"))
    _write_jsonl(
        evidence / "search/transaction_lineage_public.jsonl",
        [record.model_dump(mode="json") for record in request.search_lineage],
    )
    _write_jsonl(
        evidence / "search/selection_decisions_public.jsonl",
        [decision.model_dump(mode="json") for decision in request.selection_decisions],
    )
    _write_json(
        evidence / "search/capability_authority.json",
        capability_payload,
    )
    _write_json(evidence / "gate0_preregistration.json", request.gate0_preregistration.model_dump(mode="json"))
    _write_json(evidence / "gate0_report.json", request.gate0_report.model_dump(mode="json"))
    _write_json(evidence / "pilot_summary.json", request.pilot_summary.model_dump(mode="json"))
    _write_text(
        evidence / "limitations.md",
        "# Limitations\n\n" + "\n".join(f"- {item}" for item in request.limitations) + "\n",
    )

    _write_json(runtime / "harness_protocol.json", protocol_payload)
    _write_json(runtime / "representative_composite_plan.json", plan_payload)
    _write_json(runtime / "consumed_field_liveness_manifest.json", liveness_payload)
    _write_json(runtime / "runtime_dependency_manifest.json", dependency_payload)
    _write_json(runtime / "capability_epoch_public.json", epoch_projection.model_dump(mode="json"))
    _write_json(runtime / "runtime_profile.json", profile_projection.model_dump(mode="json"))
    _write_json(
        runtime / "runtime_identity.json",
        {
            "runtime_kind": "harness",
            "epoch_manifest_digest": request.epoch.epoch_manifest_digest,
            "deployment": request.deployment.model_dump(mode="json"),
            "protocol_source_digest": request.selected_protocol.source_digest(),
            "compiled_semantic_digest": request.representative_plan.compiled_semantic_digest,
            "representative_task_envelope_digest": request.representative_plan.task_envelope_digest,
            "dependency_manifest_digest": request.dependency_manifest.manifest_digest(),
            "profile_digest": profile_projection.profile_digest,
        },
    )

    artifact_paths = [
        "capability_epoch_public.json",
        "protocol/source.json",
        "protocol/compiled_plan.json",
        "protocol/consumed_field_liveness_manifest.json",
        "runtime/dependency_manifest.json",
        "search/transaction_lineage_public.jsonl",
        "search/selection_decisions_public.jsonl",
        "search/capability_authority.json",
        "gate0_preregistration.json",
        "gate0_report.json",
        "pilot_summary.json",
        "limitations.md",
    ]
    artifact_digests = {
        path: file_digest(evidence / path)
        for path in artifact_paths
    }
    index = PublicEvidenceIndex(
        protocol_source_digest=request.selected_protocol.source_digest(),
        compiled_semantic_digest=request.representative_plan.compiled_semantic_digest,
        dependency_manifest_digest=request.dependency_manifest.manifest_digest(),
        epoch_manifest_digest=request.epoch.epoch_manifest_digest,
        profile_digest=profile_projection.profile_digest,
        artifacts=artifact_digests,
    )
    _write_json(evidence / "evidence_index.json", index.model_dump(mode="json"))
    _write_json(runtime / "evidence_index.json", index.model_dump(mode="json"))
    bundle_runtime_kernel(runtime, profile="harness")


def _validate_required_layout(generation: Path) -> None:
    required = {
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
    }
    missing = sorted(path for path in required if not (generation / path).is_file())
    if missing:
        raise FileNotFoundError(f"release generation is incomplete: {missing}")


def _validate_typed_release_files(generation: Path, manifest: HarnessReleaseManifest) -> None:
    evidence = generation / PUBLIC_EVIDENCE_DIR
    runtime = generation / RUNTIME_DIR
    protocol = HarnessProtocol.model_validate(_read_json(evidence / "protocol/source.json"))
    plan = CompositeRunPlan.model_validate(_read_json(evidence / "protocol/compiled_plan.json"))
    dependencies = RuntimeDependencyManifest.model_validate(
        _read_json(evidence / "runtime/dependency_manifest.json")
    )
    CapabilityEpochPublicProjection.model_validate(_read_json(evidence / "capability_epoch_public.json"))
    Gate0PreregistrationPublic.model_validate(_read_json(evidence / "gate0_preregistration.json"))
    gate0_payload = _read_json(evidence / "gate0_report.json")
    if manifest.gate0_status == "completed":
        Gate0CompletedReport.model_validate(gate0_payload)
    else:
        Gate0NotRunReport.model_validate(gate0_payload)
    PilotNotRunSummary.model_validate(_read_json(evidence / "pilot_summary.json"))
    index = PublicEvidenceIndex.model_validate(_read_json(evidence / "evidence_index.json"))
    profile = HarnessRuntimeProfileProjection.model_validate(_read_json(runtime / "runtime_profile.json"))
    if protocol.source_digest() != manifest.protocol_source_digest:
        raise ValueError("released protocol identity differs from release manifest")
    if plan.source_protocol_digest != protocol.source_digest():
        raise ValueError("released representative plan crossed the protocol")
    if plan.compiled_semantic_digest != manifest.compiled_semantic_digest:
        raise ValueError("released compiled identity differs from release manifest")
    if dependencies != plan.dependency_manifest:
        raise ValueError("released dependency manifest crossed the representative plan")
    if dependencies.manifest_digest() != manifest.dependency_manifest_digest:
        raise ValueError("released dependency identity differs from release manifest")
    if profile.deployment != manifest.deployment or profile.profile_digest != manifest.profile_digest:
        raise ValueError("released runtime profile crossed deployment or profile identity")
    if index.protocol_source_digest != manifest.protocol_source_digest:
        raise ValueError("public evidence index crossed released protocol")
    for path, digest in index.artifacts.items():
        if file_digest(evidence / path) != digest:
            raise ValueError(f"public evidence index digest mismatch for {path}")
    duplicate_pairs = (
        (evidence / "protocol/source.json", runtime / "harness_protocol.json"),
        (evidence / "protocol/compiled_plan.json", runtime / "representative_composite_plan.json"),
        (evidence / "protocol/consumed_field_liveness_manifest.json", runtime / "consumed_field_liveness_manifest.json"),
        (evidence / "runtime/dependency_manifest.json", runtime / "runtime_dependency_manifest.json"),
        (evidence / "capability_epoch_public.json", runtime / "capability_epoch_public.json"),
        (evidence / "evidence_index.json", runtime / "evidence_index.json"),
    )
    for public_path, runtime_path in duplicate_pairs:
        if public_path.read_bytes() != runtime_path.read_bytes():
            raise ValueError(f"runtime/public release artifact mismatch: {public_path.name}")


def _scan_generation(generation: Path) -> None:
    for path in sorted(generation.rglob("*")):
        if not path.is_file():
            continue
        raw = path.read_bytes()
        suffix = path.suffix.casefold()
        if suffix == ".json":
            _scan_public_value(json.loads(raw.decode("utf-8")))
        elif suffix == ".jsonl":
            for line in raw.decode("utf-8").splitlines():
                if line.strip():
                    _scan_public_value(json.loads(line))
        elif suffix == ".md":
            _scan_public_value(raw.decode("utf-8"))


def _scan_public_value(value: Any, *, path: str = "root") -> None:
    assert_no_resolved_credentials(value)
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = _normalized_key(str(raw_key))
            if key in _FORBIDDEN_PUBLIC_KEYS:
                raise ValueError(f"public release contains forbidden field {path}.{raw_key}")
            if key.startswith("sealed_") or key.startswith("evaluator_") or key.startswith("canary_"):
                raise ValueError(f"public release contains forbidden authority field {path}.{raw_key}")
            _scan_public_value(item, path=f"{path}.{raw_key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            _scan_public_value(item, path=f"{path}[{index}]")
        return
    if isinstance(value, str):
        if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
            raise ValueError(f"public release contains resolved credential material at {path}")
        if _looks_like_source_absolute_path(value):
            raise ValueError(f"public release contains a source absolute path at {path}")


def _looks_like_source_absolute_path(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return False
    if _WINDOWS_ABSOLUTE_RE.search(stripped) or "file://" in stripped.casefold():
        return True
    if _POSIX_ABSOLUTE_RE.search(stripped):
        return True
    return False


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def _generation_file_digests(generation: Path, *, exclude: set[str]) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in sorted(generation.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(generation).as_posix()
        if relative in exclude:
            continue
        if PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts:
            raise ValueError(f"unsafe release file path: {relative}")
        files[relative] = file_digest(path)
    return dict(sorted(files.items()))


def _release_digest(file_digests: Mapping[str, str]) -> str:
    from ..core.identity import evidence_digest

    return evidence_digest(
        {"kind": HARNESS_RELEASE_SCHEMA_VERSION, "files": dict(sorted(file_digests.items()))}
    )


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    _write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    text = "".join(
        json.dumps(dict(row), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for row in rows
    )
    _write_text(path, text)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


__all__ = [
    "ACTIVE_RELEASE_FILE",
    "PUBLIC_EVIDENCE_DIR",
    "RELEASES_DIR",
    "RUNTIME_DIR",
    "advance_active_release",
    "load_active_release_pointer",
    "materialize_harness_release",
    "publish_harness_release",
    "validate_harness_generation",
    "validate_source_hidden_runtime_bundle",
]
