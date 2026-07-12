from __future__ import annotations

import argparse
import json
import os
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..authority.public_tasks import assert_public_payload
from ..authority.roles import current_process_role
from ..contracts.epochs import ResearchEpochManifest, TaskEnvelope
from ..contracts.outcomes import PairKey
from ..contracts.run_evidence import RunEvidence, assert_no_resolved_credentials
from ..core.identity import evidence_digest
from ..isolation.commands import DockerCommandBackend
from ..isolation.replay import (
    IsolatedCommandReplayBackend,
    IsolatedCommandReplayBinding,
    load_isolated_command_replay_manifest,
)
from ..runtime.harness_profile import HarnessDeploymentProfile
from ..runtime.sdk.harness_release_loader import (
    HarnessReleaseLoadError,
    LoadedHarnessRelease,
    load_active_harness_release,
)
from ..storage.proof_records import ImmutableProofRecordStore
from .contracts import EvaluationContract, evaluation_canary_digests
from .harness_service import (
    HarnessEvaluationCommandPlan,
    HarnessEvaluationDigestAssertions,
    HarnessEvaluationDryRunManifest,
    HarnessEvaluationPublicResult,
    HarnessEvaluationRejected,
    HarnessEvaluationService,
    harness_evaluation_public_result,
)
from .runners.repo_patch_backends import IsolatedRepoPatchCommandBackend


HARNESS_EVALUATION_ENTRY_REQUEST_SCHEMA_VERSION = (
    "repo-repair-harness-evaluation-entry-request-v1"
)
HARNESS_EVALUATION_ENTRY_ERROR_SCHEMA_VERSION = (
    "repo-repair-harness-evaluation-entry-error-v1"
)
HARNESS_EVALUATION_ENTRY_DRY_RUN_SCHEMA_VERSION = (
    "repo-repair-harness-evaluation-entry-dry-run-v1"
)


class HarnessEvaluationEntryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)


class HarnessEvaluationReplayExecution(HarnessEvaluationEntryModel):
    mode: Literal["replay"] = "replay"
    command_manifest_path: str = Field(min_length=1)

    @field_validator("command_manifest_path")
    @classmethod
    def validate_manifest_path(cls, value: str) -> str:
        return _nonempty_path(value, "command_manifest_path")


class HarnessEvaluationLiveExecution(HarnessEvaluationEntryModel):
    mode: Literal["live"] = "live"


HarnessEvaluationExecution = Annotated[
    HarnessEvaluationReplayExecution | HarnessEvaluationLiveExecution,
    Field(discriminator="mode"),
]


class HarnessEvaluationFileRequest(HarnessEvaluationEntryModel):
    """Evaluator-owned request. This model must never enter a public process."""

    schema_version: Literal[HARNESS_EVALUATION_ENTRY_REQUEST_SCHEMA_VERSION] = (
        HARNESS_EVALUATION_ENTRY_REQUEST_SCHEMA_VERSION
    )
    operation: Literal["dry_run", "evaluate"]
    execution: HarnessEvaluationExecution
    epoch: ResearchEpochManifest
    contract: EvaluationContract
    task: TaskEnvelope
    submitted_unified_diff: str = Field(min_length=1)
    pair_key: PairKey
    run_evidence: RunEvidence | None = None
    digest_assertions: HarnessEvaluationDigestAssertions | None = None
    proof_store_root: str = Field(min_length=1)

    @field_validator("proof_store_root")
    @classmethod
    def validate_proof_store_root(cls, value: str) -> str:
        return _nonempty_path(value, "proof_store_root")

    @field_validator("submitted_unified_diff")
    @classmethod
    def validate_patch_text(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("submitted_unified_diff may not contain NUL")
        if "\r" in value:
            raise ValueError("submitted_unified_diff must use canonical LF line endings")
        return value

    @model_validator(mode="after")
    def validate_operation_inputs(self) -> "HarnessEvaluationFileRequest":
        if self.operation == "evaluate" and self.run_evidence is None:
            raise ValueError("evaluate requires exact RunEvidence")
        if self.operation == "dry_run" and self.run_evidence is not None:
            raise ValueError("dry_run forbids RunEvidence because no outcome may be issued")
        assert_no_resolved_credentials(self.model_dump(mode="json"))
        return self


class HarnessEvaluationEntryErrorResult(HarnessEvaluationEntryModel):
    schema_version: Literal[HARNESS_EVALUATION_ENTRY_ERROR_SCHEMA_VERSION] = (
        HARNESS_EVALUATION_ENTRY_ERROR_SCHEMA_VERSION
    )
    status: Literal["failed"] = "failed"
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)


_PUBLIC_DRY_RUN_IDENTITY_FIELDS = frozenset(
    {
        "release_digest",
        "release_manifest_digest",
        "epoch_manifest_digest",
        "task_manifest_digest",
        "pair_key_digest",
        "protocol_digest",
        "compiled_semantic_digest",
        "dependency_manifest_digest",
        "compiler_digest",
        "kernel_digest",
        "tool_manifest_digest",
        "profile_digest",
        "provider_config_digest",
        "decoding_policy_digest",
        "price_schedule_digest",
        "command_container_policy_digest",
        "isolated_evaluation_environment_digest",
        "patch_digest",
    }
)


class HarnessEvaluationPublicDryRunMount(HarnessEvaluationEntryModel):
    source_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    target: Literal["/workspace"] = "/workspace"
    repository_working_directory: Literal["/workspace/repo"] = "/workspace/repo"
    access: Literal["read_write_scratch"] = "read_write_scratch"
    immutable_source_not_mounted: Literal[True] = True
    network_policy: Literal["none"] = "none"


class HarnessEvaluationPublicDryRun(HarnessEvaluationEntryModel):
    """Public dry-run projection; nonpublic command details never enter it."""

    schema_version: Literal[HARNESS_EVALUATION_ENTRY_DRY_RUN_SCHEMA_VERSION] = (
        HARNESS_EVALUATION_ENTRY_DRY_RUN_SCHEMA_VERSION
    )
    status: Literal["not_run"] = "not_run"
    execution_mode: Literal["replay", "live"]
    manifest_digest: str = ""
    identity: dict[str, str]
    execution_backend_id: str = Field(min_length=1)
    execution_backend_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    mounts: tuple[HarnessEvaluationPublicDryRunMount, ...] = Field(min_length=1)
    public_commands: tuple[HarnessEvaluationCommandPlan, ...] = Field(min_length=1)
    nonpublic_command_details_withheld: Literal[True] = True
    backend_invocations: Literal[0] = 0
    real_docker_requests_sent: Literal[0] = 0

    @field_validator("identity")
    @classmethod
    def validate_identity(cls, value: dict[str, str]) -> dict[str, str]:
        if set(value) != _PUBLIC_DRY_RUN_IDENTITY_FIELDS:
            raise ValueError("public evaluator dry-run identity has an invalid field set")
        normalized = {str(key): str(item).strip().lower() for key, item in value.items()}
        if any(
            len(item) != 64 or any(character not in "0123456789abcdef" for character in item)
            for item in normalized.values()
        ):
            raise ValueError("public evaluator dry-run identities must be lowercase SHA-256 digests")
        return dict(sorted(normalized.items()))

    @model_validator(mode="after")
    def bind_manifest_digest(self) -> "HarnessEvaluationPublicDryRun":
        if any(command.phase == "sealed_check" for command in self.public_commands):
            raise ValueError("nonpublic evaluator commands are forbidden in the public dry-run")
        payload = self.model_dump(mode="python", exclude={"manifest_digest"})
        computed = evidence_digest(
            {
                "kind": HARNESS_EVALUATION_ENTRY_DRY_RUN_SCHEMA_VERSION,
                **payload,
            }
        )
        if self.manifest_digest and self.manifest_digest != computed:
            raise ValueError("public evaluator dry-run manifest digest mismatch")
        if not self.manifest_digest:
            object.__setattr__(self, "manifest_digest", computed)
        return self


class HarnessEvaluationEntryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        super().__init__(message)


def _nonempty_path(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or "\x00" in normalized:
        raise ValueError(f"{field_name} must be a nonempty NUL-free path")
    return normalized


def _require_evaluator_role() -> None:
    try:
        role = current_process_role()
    except RuntimeError as exc:
        raise HarnessEvaluationEntryError(
            "process_role_invalid",
            "evaluation entrypoint received an unsupported process role",
        ) from exc
    if role != "evaluator":
        raise HarnessEvaluationEntryError(
            "evaluator_role_required",
            "harness evaluation entrypoint runs only with AGINTOR_PROCESS_ROLE=evaluator",
        )


def _json_object_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"request JSON contains duplicate key {key!r}")
        result[key] = value
    return result


def _read_json_object(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise HarnessEvaluationEntryError(
            "request_path_invalid",
            "evaluation request file may not be a symbolic link",
        )
    try:
        raw = path.read_bytes()
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_json_object_no_duplicates,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise HarnessEvaluationEntryError(
            "request_json_invalid",
            "evaluation request file is not strict UTF-8 JSON",
        ) from exc
    if not isinstance(payload, Mapping):
        raise HarnessEvaluationEntryError(
            "request_json_invalid",
            "evaluation request JSON root must be an object",
        )
    return dict(payload)


def _resolve_request_relative_path(raw_path: str, request_path: Path) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = request_path.parent / candidate
    return candidate.resolve()


def _path_is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _assert_no_symlink_components(path: Path, *, code: str, label: str) -> None:
    for component in (path, *path.parents):
        if component.exists() and component.is_symlink():
            raise HarnessEvaluationEntryError(
                code,
                f"{label} may not cross a symbolic link",
            )


def _validate_public_output_path(
    *,
    output_path: Path,
    request_path: Path,
    request: HarnessEvaluationFileRequest,
    project_root: Path,
    release: LoadedHarnessRelease,
) -> Path:
    candidate = output_path.expanduser().absolute()
    _assert_no_symlink_components(
        candidate,
        code="public_output_path_invalid",
        label="public evaluator output path",
    )
    output = candidate.resolve()
    replay_manifest = (
        _resolve_request_relative_path(
            request.execution.command_manifest_path,
            request_path,
        )
        if isinstance(request.execution, HarnessEvaluationReplayExecution)
        else None
    )
    immutable_roots = (
        (project_root / "releases").resolve(),
        release.generation_path.resolve(),
        Path(request.task.workspace_snapshot.uri).expanduser().resolve(),
        Path(request.contract.sealed_fixture.uri).expanduser().resolve(),
    )
    if (
        output == request_path
        or output == (project_root / "active_release.json").resolve()
        or (replay_manifest is not None and output == replay_manifest)
        or any(_path_is_within(output, root) for root in immutable_roots)
    ):
        raise HarnessEvaluationEntryError(
            "public_output_path_invalid",
            "public evaluator output overlaps request, replay, release, or immutable source inputs",
        )
    return output


def _load_bound_release(
    project_root: Path,
    request: HarnessEvaluationFileRequest,
) -> tuple[LoadedHarnessRelease, HarnessDeploymentProfile]:
    try:
        release = load_active_harness_release(
            project_root,
            forbidden_markers=(
                *(canary.value for canary in request.contract.canaries),
                *evaluation_canary_digests(request.contract),
            ),
        )
        profile = HarnessDeploymentProfile.model_validate(release.profile.profile)
        profile.validate_deployment_identity(release.manifest.deployment)
    except HarnessReleaseLoadError:
        raise
    except Exception as exc:
        raise HarnessEvaluationEntryError(
            "active_release_invalid",
            "active harness release or frozen command policy failed validation",
        ) from exc
    return release, profile


def _bound_epoch_resolver(
    *,
    project_root: Path,
    release: LoadedHarnessRelease,
    epoch: ResearchEpochManifest,
):
    normalized_epoch = ResearchEpochManifest.model_validate(epoch.model_dump(mode="python"))

    def resolve(epoch_manifest_digest: str) -> ResearchEpochManifest:
        if (
            epoch_manifest_digest != release.manifest.epoch_manifest_digest
            or normalized_epoch.epoch_manifest_digest
            != release.manifest.epoch_manifest_digest
            or normalized_epoch.epoch_id != release.manifest.epoch_id
        ):
            raise HarnessEvaluationEntryError(
                "epoch_release_mismatch",
                "evaluator epoch authority differs from the exact active release",
            )
        current = load_active_harness_release(project_root)
        if (
            current.pointer != release.pointer
            or current.manifest != release.manifest
            or current.runtime_identity != release.runtime_identity
        ):
            raise HarnessEvaluationEntryError(
                "active_release_changed",
                "active harness release changed while binding evaluator authority",
            )
        return normalized_epoch

    return resolve


def _build_command_backend(
    *,
    request: HarnessEvaluationFileRequest,
    request_path: Path,
    release: LoadedHarnessRelease,
    profile: HarnessDeploymentProfile,
) -> tuple[IsolatedRepoPatchCommandBackend, IsolatedCommandReplayBackend | None]:
    policy = profile.command_container_policy.to_isolated_command_policy()
    replay_backend: IsolatedCommandReplayBackend | None = None
    if isinstance(request.execution, HarnessEvaluationReplayExecution):
        manifest_path = _resolve_request_relative_path(
            request.execution.command_manifest_path,
            request_path,
        )
        try:
            manifest = load_isolated_command_replay_manifest(
                manifest_path,
                forbidden_markers=(
                    *(canary.value for canary in request.contract.canaries),
                    *evaluation_canary_digests(request.contract),
                ),
            )
            binding = IsolatedCommandReplayBinding.from_runtime_inputs(
                release_digest=release.manifest.release_digest,
                task=request.task,
                command_policy_digest=profile.command_container_policy_digest,
            )
            replay_backend = IsolatedCommandReplayBackend(
                manifest,
                expected_binding=binding,
                policy=policy,
            )
            command_backend = replay_backend
        except Exception as exc:
            raise HarnessEvaluationEntryError(
                "command_replay_manifest_invalid",
                "command replay manifest failed exact release, task, or policy binding",
            ) from exc
    else:
        command_backend = DockerCommandBackend(policy)
    return (
        IsolatedRepoPatchCommandBackend(
            command_backend,
            environment_identity=profile.command_container_policy.model_dump(mode="json"),
        ),
        replay_backend,
    )


def _proof_store(
    *,
    request: HarnessEvaluationFileRequest,
    request_path: Path,
    project_root: Path,
    public_output_path: Path | None,
) -> ImmutableProofRecordStore:
    candidate = Path(request.proof_store_root).expanduser()
    if not candidate.is_absolute():
        candidate = request_path.parent / candidate
    _assert_no_symlink_components(
        candidate,
        code="proof_store_path_invalid",
        label="controlled proof-store path",
    )
    root = candidate.resolve()
    output = public_output_path.resolve() if public_output_path is not None else None
    immutable_sources = (
        Path(request.task.workspace_snapshot.uri).expanduser().resolve(),
        Path(request.contract.sealed_fixture.uri).expanduser().resolve(),
    )
    if (
        root == request_path
        or root in request_path.parents
        or root == project_root
        or output == root
        or (output is not None and root in output.parents)
        or any(
            _path_is_within(root, source) or _path_is_within(source, root)
            for source in immutable_sources
        )
    ):
        raise HarnessEvaluationEntryError(
            "proof_store_path_invalid",
            "controlled proof-store root must be separate from request, output, and project roots",
        )
    return ImmutableProofRecordStore(root)


def _assert_safe_output(
    payload: Mapping[str, Any],
    *,
    contract: EvaluationContract,
) -> None:
    assert_no_resolved_credentials(payload)
    assert_public_payload(
        payload,
        canary_values=tuple(canary.value for canary in contract.canaries),
        canary_digests=evaluation_canary_digests(contract),
    )
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    forbidden = (
        *(canary.value for canary in contract.canaries),
        *evaluation_canary_digests(contract),
    )
    if any(marker and marker in encoded for marker in forbidden):
        raise HarnessEvaluationEntryError(
            "public_output_refused",
            "evaluation output contains evaluator-only canary material",
        )


def _public_dry_run_projection(
    manifest: HarnessEvaluationDryRunManifest,
    *,
    execution_mode: Literal["replay", "live"],
) -> HarnessEvaluationPublicDryRun:
    identity = manifest.identity.model_dump(
        mode="json",
        exclude={"evaluation_contract_digest", "fixture_digest"},
    )
    identity["isolated_evaluation_environment_digest"] = identity.pop(
        "evaluator_environment_digest"
    )
    commands = tuple(
        command for command in manifest.commands if command.phase != "sealed_check"
    )
    return HarnessEvaluationPublicDryRun(
        execution_mode=execution_mode,
        identity=identity,
        execution_backend_id=manifest.execution_backend_id,
        execution_backend_digest=manifest.execution_backend_digest,
        mounts=tuple(
            HarnessEvaluationPublicDryRunMount(
                source_snapshot_digest=mount.source_snapshot_digest,
            )
            for mount in manifest.mounts
        ),
        public_commands=commands,
    )


def execute_harness_evaluation_file(
    project_root: str | Path,
    request_path: str | Path,
    *,
    public_output_path: str | Path | None = None,
) -> HarnessEvaluationPublicDryRun | HarnessEvaluationPublicResult:
    _require_evaluator_role()
    project = Path(project_root).expanduser().resolve()
    request_file = Path(request_path).expanduser().resolve()
    try:
        request = HarnessEvaluationFileRequest.model_validate(
            _read_json_object(request_file)
        )
    except HarnessEvaluationEntryError:
        raise
    except Exception as exc:
        raise HarnessEvaluationEntryError(
            "evaluation_request_invalid",
            "structured evaluator request failed strict validation",
        ) from exc

    release, profile = _load_bound_release(project, request)
    validated_public_output = (
        _validate_public_output_path(
            output_path=Path(public_output_path),
            request_path=request_file,
            request=request,
            project_root=project,
            release=release,
        )
        if public_output_path is not None
        else None
    )
    command_backend, replay_backend = _build_command_backend(
        request=request,
        request_path=request_file,
        release=release,
        profile=profile,
    )
    service = HarnessEvaluationService(
        project_root=project,
        proof_store=_proof_store(
            request=request,
            request_path=request_file,
            project_root=project,
            public_output_path=validated_public_output,
        ),
        command_backend=command_backend,
        epoch_resolver=_bound_epoch_resolver(
            project_root=project,
            release=release,
            epoch=request.epoch,
        ),
    )
    if request.operation == "dry_run":
        internal_manifest = service.dry_run(
            contract=request.contract,
            task=request.task,
            submitted_unified_diff=request.submitted_unified_diff,
            pair_key=request.pair_key,
            digest_assertions=request.digest_assertions,
        )
        result: HarnessEvaluationPublicDryRun | HarnessEvaluationPublicResult = (
            _public_dry_run_projection(
                internal_manifest,
                execution_mode=request.execution.mode,
            )
        )
    else:
        if request.run_evidence is None:  # Rejected by the model; keeps typing exact.
            raise HarnessEvaluationEntryError(
                "run_evidence_required",
                "evaluate requires exact RunEvidence",
            )
        result = harness_evaluation_public_result(
            service.evaluate(
                contract=request.contract,
                task=request.task,
                submitted_unified_diff=request.submitted_unified_diff,
                run_evidence=request.run_evidence,
                pair_key=request.pair_key,
                digest_assertions=request.digest_assertions,
            )
        )
        if replay_backend is not None:
            try:
                replay_backend.assert_reconciled()
            except Exception as exc:
                raise HarnessEvaluationEntryError(
                    "command_replay_reconciliation_failed",
                    "evaluation did not consume the exact command replay manifest",
                ) from exc
    _assert_safe_output(result.model_dump(mode="json"), contract=request.contract)
    return result


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise HarnessEvaluationEntryError(
            "output_path_invalid",
            "public evaluator output must be a regular file path",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(dict(payload), handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _error_result(exc: Exception) -> HarnessEvaluationEntryErrorResult:
    if isinstance(
        exc,
        (
            HarnessEvaluationEntryError,
            HarnessEvaluationRejected,
            HarnessReleaseLoadError,
        ),
    ):
        return HarnessEvaluationEntryErrorResult(code=exc.code, message=str(exc))
    return HarnessEvaluationEntryErrorResult(
        code="harness_evaluation_entry_failed",
        message=f"{type(exc).__name__}: evaluator entry operation failed",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agintor.evaluation.harness_entrypoint")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--request-json", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--public-output-path")
    args = parser.parse_args(argv)

    request_path = Path(args.request_json).expanduser().resolve()
    output_path = Path(args.output_json).expanduser().resolve()
    public_output_path = Path(
        args.public_output_path or args.output_json
    ).expanduser().resolve()
    try:
        if output_path == request_path or public_output_path == request_path:
            raise HarnessEvaluationEntryError(
                "output_path_invalid",
                "public evaluator output may not replace its sealed request file",
            )
        result = execute_harness_evaluation_file(
            args.project_root,
            request_path,
            public_output_path=public_output_path,
        )
        _write_json_atomic(output_path, result.model_dump(mode="json"))
        return 0
    except Exception as exc:
        error = _error_result(exc)
        path_refused = error.code in {
            "output_path_invalid",
            "proof_store_path_invalid",
            "public_output_path_invalid",
        }
        if output_path != request_path and not (
            output_path == public_output_path and path_refused
        ):
            _write_json_atomic(output_path, error.model_dump(mode="json"))
        return 2


__all__ = [
    "HARNESS_EVALUATION_ENTRY_DRY_RUN_SCHEMA_VERSION",
    "HARNESS_EVALUATION_ENTRY_ERROR_SCHEMA_VERSION",
    "HARNESS_EVALUATION_ENTRY_REQUEST_SCHEMA_VERSION",
    "HarnessEvaluationEntryError",
    "HarnessEvaluationEntryErrorResult",
    "HarnessEvaluationExecution",
    "HarnessEvaluationFileRequest",
    "HarnessEvaluationLiveExecution",
    "HarnessEvaluationPublicDryRun",
    "HarnessEvaluationPublicDryRunMount",
    "HarnessEvaluationReplayExecution",
    "execute_harness_evaluation_file",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
