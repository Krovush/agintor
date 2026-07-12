from __future__ import annotations

import json
import os
import platform
import re
import sys
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ...authority.public_tasks import assert_public_payload, task_envelope_public_projection
from ...contracts.epochs import (
    DeploymentIdentity,
    REPO_REPAIR_TRUSTED_TOOL_IDS,
    TaskEnvelope,
)
from ...contracts.harness import HarnessPublicSessionContext, RuntimeDependencyManifest
from ...contracts.outcomes import PairKey, pair_key_digest
from ...contracts.run_evidence import (
    EnvironmentEvidence,
    RunEvidence,
    ToolReceiptEvidence,
    assert_no_resolved_credentials,
    runtime_environment_evidence_digest,
)
from ...core.identity import canonical_identity_digest
from ...isolation.commands import IsolatedCommandBackend, IsolatedCommandPolicy
from ..harness_profile import HarnessDeploymentProfile
from ..evidence import (
    ProviderCallDetail,
    PublicVerificationEvidence,
    assemble_run_evidence,
    public_verification_action_digest,
    public_verification_plan_digest,
)
from ...repositories.workspaces import (
    RepositorySnapshotError,
    TaskWorkspace,
    materialize_task_workspace,
    repository_snapshot_digest,
    resolve_local_snapshot_uri,
)
from ..api.composite_compiler import CompositeCompilationError, compile_composite_run_plan
from ..kernel.composite_artifacts import ArtifactDeliveryEvidence, ArtifactEvidence
from ..kernel.composite_budget import AggregateBudgetSnapshot
from ..kernel.composite_provider import (
    ControlledProvider,
    CredentialReference,
    ProviderCallResult,
    ProviderExecutionProvenance,
)
from ..kernel.composite_runtime import (
    CompositeRunResult,
    CompositeRuntime,
    CompositeRuntimeError,
    ScratchWorkspaceBinding,
)
from ..kernel.repair_tools import (
    PublicVerificationResult,
    RepairToolReceipt,
    TrustedRepairToolService,
)
from .harness_release_loader import (
    HarnessReleaseLoadError,
    LoadedHarnessRelease,
    load_active_harness_release,
    validate_harness_release_generation,
)


HARNESS_SOLVE_RESULT_SCHEMA_VERSION = "repo-repair-harness-solve-result-v1"
HARNESS_SOLVE_RESULT_FILE = "harness_solve_result.json"
CONTROLLED_RUN_EVIDENCE_DIR = "controlled"
CONTROLLED_RUN_EVIDENCE_FILE = "run_evidence.json"
CONTROLLED_RUN_EVIDENCE_REF = (
    f"{CONTROLLED_RUN_EVIDENCE_DIR}/{CONTROLLED_RUN_EVIDENCE_FILE}"
)

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")


class HarnessSolveError(RuntimeError):
    """A fail-closed error raised before a trustworthy run result exists."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class DeploymentBoundProvider(ControlledProvider, Protocol):
    """A provider adapter bound to the deployment declared by a release."""

    deployment_identity: DeploymentIdentity


class HarnessSolveModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)


def _require_digest(value: str, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _DIGEST_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return normalized


def _require_identifier(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not _IDENTIFIER_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a portable identifier")
    return normalized


class HarnessReleaseExecutionIdentity(HarnessSolveModel):
    release_digest: str
    release_manifest_digest: str
    epoch_id: str
    epoch_manifest_digest: str
    deployment: DeploymentIdentity
    protocol_source_digest: str
    dependency_manifest_digest: str
    profile_digest: str
    release_evidence_index_digest: str

    @field_validator(
        "release_digest",
        "release_manifest_digest",
        "epoch_manifest_digest",
        "protocol_source_digest",
        "dependency_manifest_digest",
        "profile_digest",
        "release_evidence_index_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)


class HarnessTaskExecutionIdentity(HarnessSolveModel):
    task_manifest_id: str
    task_envelope_digest: str
    capability_epoch: Literal["repo-repair-v1"] = "repo-repair-v1"
    data_state: Literal["development"] = "development"
    split_manifest_digest: str
    snapshot_id: str
    snapshot_digest: str

    @field_validator("task_envelope_digest", "split_manifest_digest", "snapshot_digest")
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)


class HarnessCompiledExecutionIdentity(HarnessSolveModel):
    compiled_semantic_digest: str
    compiler_dependency_id: str
    compiler_interface_version: str
    compiler_implementation_digest: str
    harness_contract_implementation_digest: str
    kernel_implementation_digest: str

    @field_validator(
        "compiled_semantic_digest",
        "compiler_implementation_digest",
        "harness_contract_implementation_digest",
        "kernel_implementation_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)


class HarnessProviderRoundReference(HarnessSolveModel):
    call_id: str
    turn_index: int = Field(ge=0)
    request_digest: str
    reservation_id: str
    status: str
    response_id: str | None = None
    response_digest: str
    usage_digest: str
    response_kind: Literal["tool_request", "terminal"]
    tool_request_id: str | None = None

    @field_validator("request_digest", "response_digest", "usage_digest")
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)


class HarnessContextReference(HarnessSolveModel):
    call_id: str
    manifest_digest: str

    @field_validator("manifest_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _require_digest(value, "manifest_digest")


class HarnessArtifactReference(HarnessSolveModel):
    artifact_id: str
    producer_call_id: str
    payload_digest: str
    byte_size: int = Field(ge=0)
    consumer_call_ids: tuple[str, ...]

    @field_validator("payload_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _require_digest(value, "payload_digest")


class HarnessArtifactDeliveryReference(HarnessSolveModel):
    artifact_id: str
    producer_call_id: str
    consumer_call_id: str
    payload_digest: str

    @field_validator("payload_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _require_digest(value, "payload_digest")


class HarnessToolReceiptReference(HarnessSolveModel):
    receipt_id: str
    call_id: str
    tool_id: str
    phase: Literal["actor_tool", "terminal_public_verification"]
    tool_request_id: str | None = None
    verification_step_id: str | None = None
    status: str
    output_digest: str
    receipt_digest: str
    charged: bool

    @field_validator("output_digest", "receipt_digest")
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)


class HarnessRunEvidenceIndex(HarnessSolveModel):
    task_envelope_digest: str
    compiled_semantic_digest: str
    release_evidence_index_digest: str
    provider_rounds: tuple[HarnessProviderRoundReference, ...] = ()
    contexts: tuple[HarnessContextReference, ...] = ()
    artifacts: tuple[HarnessArtifactReference, ...] = ()
    artifact_deliveries: tuple[HarnessArtifactDeliveryReference, ...] = ()
    tool_receipts: tuple[HarnessToolReceiptReference, ...] = ()
    public_verification_receipt_ids: tuple[str, ...] = ()
    public_command_evidence_digests: tuple[str, ...] = ()
    index_digest: str = ""

    @field_validator(
        "task_envelope_digest",
        "compiled_semantic_digest",
        "release_evidence_index_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @field_validator("public_command_evidence_digests")
    @classmethod
    def validate_command_digests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_require_digest(item, "command evidence digest") for item in value)

    @model_validator(mode="after")
    def bind_index(self) -> "HarnessRunEvidenceIndex":
        for label, values, key in (
            ("provider round", self.provider_rounds, lambda item: (item.call_id, item.turn_index)),
            ("context", self.contexts, lambda item: item.call_id),
            ("artifact", self.artifacts, lambda item: item.artifact_id),
            (
                "artifact delivery",
                self.artifact_deliveries,
                lambda item: (item.artifact_id, item.consumer_call_id),
            ),
            ("tool receipt", self.tool_receipts, lambda item: item.receipt_id),
        ):
            identities = [key(item) for item in values]
            if len(identities) != len(set(identities)):
                raise ValueError(f"duplicate {label} evidence identity")
        payload = self.model_dump(mode="json", exclude={"index_digest"})
        digest = canonical_identity_digest(payload, domain="harness-run-evidence-index")
        if self.index_digest and self.index_digest != digest:
            raise ValueError("run evidence index digest mismatch")
        if not self.index_digest:
            object.__setattr__(self, "index_digest", digest)
        return self


class HarnessControlledRunEvidenceReference(HarnessSolveModel):
    relative_path: Literal[CONTROLLED_RUN_EVIDENCE_REF] = CONTROLLED_RUN_EVIDENCE_REF
    evidence_id: str
    evidence_digest: str
    pair_key_digest: str
    runtime_environment_digest: str
    release_digest: str
    release_manifest_digest: str
    task_manifest_digest: str
    protocol_digest: str
    compiled_semantic_digest: str

    @field_validator("evidence_id")
    @classmethod
    def validate_evidence_id(cls, value: str) -> str:
        return _require_identifier(value, "evidence_id")

    @field_validator(
        "evidence_digest",
        "pair_key_digest",
        "runtime_environment_digest",
        "release_digest",
        "release_manifest_digest",
        "task_manifest_digest",
        "protocol_digest",
        "compiled_semantic_digest",
    )
    @classmethod
    def validate_reference_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)


class HarnessSubmittedPatch(HarnessSolveModel):
    unified_diff: str
    patch_digest: str
    byte_size: int = Field(ge=0)

    @field_validator("patch_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _require_digest(value, "patch_digest")

    @model_validator(mode="after")
    def validate_patch(self) -> "HarnessSubmittedPatch":
        if self.byte_size != len(self.unified_diff.encode("utf-8")):
            raise ValueError("submitted patch byte_size mismatch")
        digest = canonical_identity_digest(self.unified_diff, domain="final-unified-diff")
        if self.patch_digest != digest:
            raise ValueError("submitted patch digest mismatch")
        return self


class HarnessPublicVerificationSummary(HarnessSolveModel):
    status: Literal["not_run", "passed", "failed"]
    receipt_ids: tuple[str, ...] = ()
    command_evidence_digests: tuple[str, ...] = ()

    @field_validator("command_evidence_digests")
    @classmethod
    def validate_digests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_require_digest(item, "command evidence digest") for item in value)

    @model_validator(mode="after")
    def validate_shape(self) -> "HarnessPublicVerificationSummary":
        if self.status == "not_run":
            if self.receipt_ids or self.command_evidence_digests:
                raise ValueError("not-run public verification may not claim evidence")
        elif not self.receipt_ids:
            raise ValueError("completed public verification requires receipts")
        return self


class HarnessProviderFailureSummary(HarnessSolveModel):
    status: Literal["failed", "rejected", "cancelled", "deadline_exceeded"]
    reservation_id: str | None = None
    timeout_ms: int = Field(ge=0)
    failure_kind: str
    request_sent: bool
    usage_status: str | None = None
    cost_status: str | None = None
    ambiguous_post_send: bool
    accounting_healthy: bool
    budget_metric: str | None = None
    error_type: str | None = None


class HarnessSolveFailure(HarnessSolveModel):
    kind: str
    call_id: str | None = None
    provider: HarnessProviderFailureSummary | None = None

    @field_validator("kind")
    @classmethod
    def validate_kind(cls, value: str) -> str:
        return _require_identifier(value, "failure kind")


class HarnessTerminationSummary(HarnessSolveModel):
    final_actor_call_id: str
    status: Literal["completed", "public_verification_failed", "runtime_failure"]
    failure_kind: str | None = None

    @model_validator(mode="after")
    def validate_failure_kind(self) -> "HarnessTerminationSummary":
        if self.status == "runtime_failure" and self.failure_kind is None:
            raise ValueError("runtime failure termination requires a failure kind")
        if self.status != "runtime_failure" and self.failure_kind is not None:
            raise ValueError("successful termination may not carry a failure kind")
        return self


class HarnessSolveResult(HarnessSolveModel):
    schema_version: Literal[HARNESS_SOLVE_RESULT_SCHEMA_VERSION] = (
        HARNESS_SOLVE_RESULT_SCHEMA_VERSION
    )
    run_id: str
    workspace_id: str
    status: Literal["completed", "public_verification_failed", "failed"]
    execution_mode: Literal["deterministic_replay", "live_provider"]
    live_inference_status: Literal["not_run", "completed", "failed"]
    real_inference_requests_sent: int = Field(ge=0)
    release: HarnessReleaseExecutionIdentity
    task: HarnessTaskExecutionIdentity
    compiled: HarnessCompiledExecutionIdentity
    final_workspace_digest: str
    source_snapshot_unchanged: Literal[True] = True
    immutable_base_unchanged: Literal[True] = True
    submitted_patch: HarnessSubmittedPatch | None = None
    public_verification: HarnessPublicVerificationSummary
    termination: HarnessTerminationSummary
    budget: AggregateBudgetSnapshot
    evidence: HarnessRunEvidenceIndex
    controlled_run_evidence: HarnessControlledRunEvidenceReference | None = None
    failure: HarnessSolveFailure | None = None
    eligible_for_evaluator_submission: bool
    capability_promotion_authorized: Literal[False] = False
    result_digest: str = ""

    @field_validator("run_id", "workspace_id")
    @classmethod
    def validate_id(cls, value: str, info: Any) -> str:
        return _require_identifier(value, info.field_name)

    @field_validator("final_workspace_digest")
    @classmethod
    def validate_workspace_digest(cls, value: str) -> str:
        return _require_digest(value, "final_workspace_digest")

    @model_validator(mode="after")
    def validate_result(self) -> "HarnessSolveResult":
        if self.status == "failed":
            if self.failure is None or self.submitted_patch is not None:
                raise ValueError("failed solves require a failure and no submitted patch")
            if self.public_verification.status != "not_run":
                raise ValueError("failed solves may not claim public verification")
            if self.termination.status != "runtime_failure":
                raise ValueError("failed solve termination must be runtime_failure")
        else:
            if self.failure is not None or self.submitted_patch is None:
                raise ValueError("completed execution requires a submitted patch and no failure")
            if self.status == "completed":
                if self.public_verification.status != "passed" or self.termination.status != "completed":
                    raise ValueError("completed solve requires passed public verification")
            elif (
                self.public_verification.status != "failed"
                or self.termination.status != "public_verification_failed"
            ):
                raise ValueError("public-verification failure status is internally inconsistent")
        expected_submission = (
            self.status in {"completed", "public_verification_failed"}
            and self.budget.reconciled
            and self.budget.healthy
            and self.controlled_run_evidence is not None
        )
        if self.eligible_for_evaluator_submission != expected_submission:
            raise ValueError(
                "evaluator-submission eligibility differs from execution and accounting status"
            )
        ProviderExecutionProvenance(
            execution_mode=self.execution_mode,
            live_inference_status=self.live_inference_status,
            real_inference_requests_sent=self.real_inference_requests_sent,
        )
        if self.evidence.task_envelope_digest != self.task.task_envelope_digest:
            raise ValueError("run evidence crossed task identity")
        if self.evidence.compiled_semantic_digest != self.compiled.compiled_semantic_digest:
            raise ValueError("run evidence crossed compiled identity")
        if self.evidence.release_evidence_index_digest != self.release.release_evidence_index_digest:
            raise ValueError("run evidence crossed release evidence identity")
        if self.controlled_run_evidence is not None:
            reference = self.controlled_run_evidence
            if (
                reference.release_digest != self.release.release_digest
                or reference.release_manifest_digest != self.release.release_manifest_digest
                or reference.task_manifest_digest != self.task.task_envelope_digest
                or reference.protocol_digest != self.release.protocol_source_digest
                or reference.compiled_semantic_digest
                != self.compiled.compiled_semantic_digest
            ):
                raise ValueError("controlled RunEvidence reference crossed solve identity")
        payload = self.model_dump(mode="json", exclude={"result_digest"})
        digest = canonical_identity_digest(payload, domain="harness-solve-result")
        if self.result_digest and self.result_digest != digest:
            raise ValueError("harness solve result digest mismatch")
        if not self.result_digest:
            object.__setattr__(self, "result_digest", digest)
        return self


def _normalize_public_task(
    task: TaskEnvelope,
) -> TaskEnvelope:
    if not isinstance(task, TaskEnvelope):
        raise HarnessSolveError(
            "task_contract_required",
            "solve accepts a validated TaskEnvelope, not an untyped task payload",
        )
    try:
        normalized = TaskEnvelope.model_validate(task.model_dump(mode="python"))
        task_envelope_public_projection(normalized)
        assert_no_resolved_credentials(normalized.model_dump(mode="json"))
    except Exception as exc:
        raise HarnessSolveError("public_task_invalid", "task failed the public boundary") from exc
    if normalized.data_state != "development":
        raise HarnessSolveError(
            "sealed_task_forbidden",
            "the public solve path accepts development tasks only",
        )
    return normalized


def _assert_task_bound_to_release(task: TaskEnvelope, release: LoadedHarnessRelease) -> None:
    epoch = release.epoch
    if task.runtime_contract_version != epoch.runtime_contract_version:
        raise HarnessSolveError("task_release_mismatch", "task runtime contract differs from release")
    if task.epoch_id != epoch.epoch_id or task.epoch_manifest_digest != epoch.epoch_manifest_digest:
        raise HarnessSolveError("task_release_mismatch", "task epoch differs from active release")
    if task.capability_epoch != epoch.capability_epoch:
        raise HarnessSolveError("task_release_mismatch", "task capability differs from active release")
    if task.split_manifest_digest != epoch.development_split_digest:
        raise HarnessSolveError("task_release_mismatch", "task split differs from active release")
    release_tools = tuple(tool.tool_id for tool in epoch.trusted_tools)
    if set(task.allowed_capabilities) != set(release_tools) or tuple(
        task.allowed_capabilities
    ) != REPO_REPAIR_TRUSTED_TOOL_IDS:
        raise HarnessSolveError("task_release_mismatch", "task tool authority differs from release")
    if not task.ceilings.is_within(epoch.per_run_ceilings):
        raise HarnessSolveError("task_ceiling_exceeded", "task ceilings exceed active release authority")


def _provider_deployment(provider: ControlledProvider) -> DeploymentIdentity:
    value = getattr(provider, "deployment_identity", None)
    if callable(value):
        value = value()
    if value is None:
        raise HarnessSolveError(
            "provider_deployment_missing",
            "provider adapter must declare its immutable deployment_identity",
        )
    try:
        if isinstance(value, DeploymentIdentity):
            payload = value.model_dump(mode="python")
        elif isinstance(value, Mapping):
            payload = dict(value)
        else:
            raise TypeError("deployment identity is not a contract or mapping")
        deployment = DeploymentIdentity.model_validate(payload)
        assert_no_resolved_credentials(deployment.model_dump(mode="json"))
        return deployment
    except HarnessSolveError:
        raise
    except Exception as exc:
        raise HarnessSolveError(
            "provider_deployment_invalid",
            "provider deployment identity is invalid",
        ) from exc


def _provider_execution_provenance(
    provider: ControlledProvider,
) -> ProviderExecutionProvenance:
    value = getattr(provider, "execution_provenance", None)
    if callable(value):
        value = value()
    if value is None:
        raise HarnessSolveError(
            "provider_provenance_missing",
            "provider adapter must declare execution provenance",
        )
    try:
        if isinstance(value, ProviderExecutionProvenance):
            payload = value.model_dump(mode="python")
        elif isinstance(value, Mapping):
            payload = dict(value)
        else:
            raise TypeError("provider provenance is not a contract or mapping")
        return ProviderExecutionProvenance.model_validate(payload)
    except Exception as exc:
        raise HarnessSolveError(
            "provider_provenance_invalid",
            "provider execution provenance failed validation",
        ) from exc


def _final_provider_execution_provenance(
    provider: ControlledProvider,
    initial: ProviderExecutionProvenance,
) -> ProviderExecutionProvenance:
    current = _provider_execution_provenance(provider)
    if current.execution_mode != initial.execution_mode:
        raise HarnessSolveError(
            "provider_provenance_changed",
            "provider execution mode changed during solve",
        )
    if current.execution_mode == "deterministic_replay" and current != initial:
        raise HarnessSolveError(
            "provider_provenance_changed",
            "deterministic replay provenance changed during solve",
        )
    return current


def _normalize_credential_reference(
    value: CredentialReference | None,
    *,
    deployment: DeploymentIdentity,
) -> CredentialReference | None:
    if value is None:
        return None
    if not isinstance(value, CredentialReference):
        raise HarnessSolveError(
            "credential_reference_required",
            "credential transport accepts CredentialReference only",
        )
    try:
        normalized = CredentialReference.model_validate(value.model_dump(mode="python"))
        assert_no_resolved_credentials(normalized.model_dump(mode="json"))
    except Exception as exc:
        raise HarnessSolveError(
            "credential_reference_invalid",
            "credential reference failed boundary validation",
        ) from exc
    if normalized.provider_name.casefold() != deployment.provider.casefold():
        raise HarnessSolveError(
            "credential_provider_mismatch",
            "credential reference provider differs from active deployment",
        )
    return normalized


def _normalize_public_session_context(
    value: HarnessPublicSessionContext | Mapping[str, Any] | None,
    *,
    release: LoadedHarnessRelease,
) -> HarnessPublicSessionContext | None:
    if value is None:
        return None
    try:
        normalized = HarnessPublicSessionContext.model_validate(
            value.model_dump(mode="python")
            if isinstance(value, HarnessPublicSessionContext)
            else value
        )
        payload = normalized.model_dump(mode="json")
        assert_no_resolved_credentials(payload)
        assert_public_payload(payload)
    except Exception as exc:
        raise HarnessSolveError(
            "public_session_context_invalid",
            "public session context failed the harness solve boundary",
        ) from exc
    if normalized.active_release_digest != release.manifest.release_digest:
        raise HarnessSolveError(
            "session_release_mismatch",
            "public session context is pinned to a different immutable release",
        )
    return normalized


def _credential_reference_from_release(release: LoadedHarnessRelease) -> CredentialReference:
    profile = HarnessDeploymentProfile.model_validate(release.profile.profile)
    return CredentialReference(
        provider_name=profile.provider,
        api_key_env=profile.endpoint.api_key_env,
        api_key_file_env=profile.endpoint.api_key_file_env,
    )


def _command_policy_from_release(release: LoadedHarnessRelease) -> IsolatedCommandPolicy:
    profile = HarnessDeploymentProfile.model_validate(release.profile.profile)
    return profile.command_container_policy.to_isolated_command_policy()


def _validate_command_backend_policy(
    command_backend: IsolatedCommandBackend,
    release: LoadedHarnessRelease,
) -> None:
    expected = _command_policy_from_release(release)
    raw_policy = getattr(command_backend, "policy", None)
    if raw_policy is None:
        raise HarnessSolveError(
            "command_policy_missing",
            "command backend must expose the frozen IsolatedCommandPolicy",
        )
    try:
        if isinstance(raw_policy, IsolatedCommandPolicy):
            payload = raw_policy.model_dump(mode="python")
        elif isinstance(raw_policy, Mapping):
            payload = dict(raw_policy)
        elif callable(getattr(raw_policy, "model_dump", None)):
            payload = raw_policy.model_dump(mode="python")
        else:
            payload = raw_policy
        actual = IsolatedCommandPolicy.model_validate(payload)
    except Exception as exc:
        raise HarnessSolveError(
            "command_policy_invalid",
            "command backend policy failed isolated-command validation",
        ) from exc
    if actual != expected:
        raise HarnessSolveError(
            "command_policy_mismatch",
            "command backend policy differs from the frozen deployment profile",
        )


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _assert_workspace_location(
    candidate: Path,
    *,
    source_root: Path,
    generation_path: Path,
) -> None:
    if _paths_overlap(candidate, source_root):
        raise HarnessSolveError(
            "workspace_source_overlap",
            "run-artifact workspace overlaps the immutable repository snapshot",
        )
    if _paths_overlap(candidate, generation_path):
        raise HarnessSolveError(
            "workspace_release_overlap",
            "run-artifact workspace overlaps the immutable release generation",
        )


def _claim_run_artifact_workspace(
    *,
    run_artifact_workspace: str | Path | None,
    run_root: str | Path | None,
    run_id: str,
    source_root: Path,
    generation_path: Path,
) -> Path:
    if (run_artifact_workspace is None) == (run_root is None):
        raise HarnessSolveError(
            "workspace_selection_invalid",
            "provide exactly one explicit run-artifact workspace or caller run root",
        )
    if run_artifact_workspace is not None:
        candidate = Path(run_artifact_workspace).expanduser().resolve()
        _assert_workspace_location(
            candidate,
            source_root=source_root,
            generation_path=generation_path,
        )
        candidate.parent.mkdir(parents=True, exist_ok=True)
        try:
            candidate.mkdir(exist_ok=False)
        except FileExistsError as exc:
            raise HarnessSolveError(
                "workspace_already_exists",
                "run-artifact workspaces are single-use and may not be reused",
            ) from exc
        return candidate

    root = Path(run_root).expanduser().resolve()
    if root == source_root or source_root in root.parents:
        raise HarnessSolveError(
            "workspace_source_overlap",
            "caller run root cannot contain the immutable repository snapshot",
        )
    if root == generation_path or generation_path in root.parents:
        raise HarnessSolveError(
            "workspace_release_overlap",
            "caller run root cannot contain the immutable release generation",
        )
    root.mkdir(parents=True, exist_ok=True)
    for _ in range(32):
        candidate = (root / f"{run_id}-{uuid.uuid4().hex}").resolve()
        _assert_workspace_location(
            candidate,
            source_root=source_root,
            generation_path=generation_path,
        )
        try:
            candidate.mkdir(exist_ok=False)
        except FileExistsError:
            continue
        return candidate
    raise HarnessSolveError(
        "workspace_allocation_failed",
        "could not allocate a collision-free run-artifact workspace",
    )


def _execution_identity(value: str | None, *, prefix: str) -> str:
    if value is None:
        return f"{prefix}.{uuid.uuid4().hex}"
    try:
        return _require_identifier(value, f"{prefix} identity")
    except ValueError as exc:
        raise HarnessSolveError(
            "execution_identity_invalid",
            f"explicit {prefix} identity is not a portable identifier",
        ) from exc


def _usage_digest(value: Any) -> str:
    return canonical_identity_digest(value.model_dump(mode="json"), domain="provider-usage")


def _command_evidence_digests(result: PublicVerificationResult | None) -> tuple[str, ...]:
    if result is None:
        return ()
    return tuple(
        canonical_identity_digest(
            item.model_dump(mode="json"),
            domain="isolated-command-evidence",
        )
        for item in result.command_evidence
    )


def _tool_receipt_references(
    receipts: Sequence[RepairToolReceipt],
) -> tuple[HarnessToolReceiptReference, ...]:
    return tuple(
        HarnessToolReceiptReference(
            receipt_id=receipt.receipt_id,
            call_id=receipt.call_id,
            tool_id=receipt.tool_id,
            phase=receipt.phase,
            tool_request_id=receipt.tool_request_id,
            verification_step_id=receipt.verification_step_id,
            status=receipt.status.value,
            output_digest=receipt.output_digest,
            receipt_digest=canonical_identity_digest(
                receipt.model_dump(mode="json"),
                domain="repair-tool-receipt",
            ),
            charged=receipt.charged,
        )
        for receipt in receipts
    )


def _artifact_references(
    artifacts: Sequence[ArtifactEvidence],
) -> tuple[HarnessArtifactReference, ...]:
    return tuple(
        HarnessArtifactReference(
            artifact_id=item.artifact.artifact_id,
            producer_call_id=item.artifact.producer_call_id,
            payload_digest=item.artifact.payload_digest,
            byte_size=item.artifact.byte_size,
            consumer_call_ids=item.actual_consumer_call_ids,
        )
        for item in artifacts
    )


def _delivery_references(
    deliveries: Sequence[ArtifactDeliveryEvidence],
) -> tuple[HarnessArtifactDeliveryReference, ...]:
    return tuple(
        HarnessArtifactDeliveryReference(
            artifact_id=item.artifact_id,
            producer_call_id=item.producer_call_id,
            consumer_call_id=item.consumer_call_id,
            payload_digest=item.payload_digest,
        )
        for item in deliveries
    )


def _success_evidence_index(
    result: CompositeRunResult,
    *,
    release: LoadedHarnessRelease,
) -> HarnessRunEvidenceIndex:
    provider_rounds = tuple(
        HarnessProviderRoundReference(
            call_id=call.call_id,
            turn_index=round_.turn_index,
            request_digest=round_.request_digest,
            reservation_id=round_.reservation_id,
            status=round_.status,
            response_id=round_.response_id,
            response_digest=round_.response_digest,
            usage_digest=_usage_digest(round_.usage),
            response_kind=round_.response_kind,
            tool_request_id=round_.tool_request_id,
        )
        for call in result.actor_calls
        for round_ in call.provider_rounds
    )
    command_digests = _command_evidence_digests(result.public_verification)
    return HarnessRunEvidenceIndex(
        task_envelope_digest=result.task_envelope_digest,
        compiled_semantic_digest=result.compiled_semantic_digest,
        release_evidence_index_digest=release.evidence_index.index_digest,
        provider_rounds=provider_rounds,
        contexts=tuple(
            HarnessContextReference(
                call_id=context.call_id,
                manifest_digest=context.manifest_digest,
            )
            for context in result.context_manifests
        ),
        artifacts=_artifact_references(result.artifacts),
        artifact_deliveries=_delivery_references(result.artifact_deliveries),
        tool_receipts=_tool_receipt_references(result.tool_receipts),
        public_verification_receipt_ids=(
            result.public_verification.receipt_ids
            if result.public_verification is not None
            else ()
        ),
        public_command_evidence_digests=command_digests,
    )


def _partial_evidence_index(
    runtime: CompositeRuntime,
    tool_service: TrustedRepairToolService,
    *,
    task: TaskEnvelope,
    compiled_semantic_digest: str,
    release: LoadedHarnessRelease,
) -> HarnessRunEvidenceIndex:
    return HarnessRunEvidenceIndex(
        task_envelope_digest=task.task_manifest_digest,
        compiled_semantic_digest=compiled_semantic_digest,
        release_evidence_index_digest=release.evidence_index.index_digest,
        artifacts=_artifact_references(runtime.artifacts.evidence()),
        artifact_deliveries=_delivery_references(runtime.artifacts.deliveries()),
        tool_receipts=_tool_receipt_references(tool_service.receipts()),
    )


def _provider_failure_summary(
    result: ProviderCallResult | None,
) -> HarnessProviderFailureSummary | None:
    if result is None or result.failure is None or result.status.value == "succeeded":
        return None
    failure = result.failure
    return HarnessProviderFailureSummary(
        status=result.status.value,
        reservation_id=result.reservation_id,
        timeout_ms=result.timeout_ms,
        failure_kind=failure.kind.value,
        request_sent=failure.request_sent,
        usage_status=(failure.usage_status.value if failure.usage_status is not None else None),
        cost_status=(failure.cost_status.value if failure.cost_status is not None else None),
        ambiguous_post_send=failure.ambiguous_post_send,
        accounting_healthy=failure.accounting_healthy,
        budget_metric=failure.budget_metric,
        error_type=failure.error_type,
    )


def _release_execution_identity(release: LoadedHarnessRelease) -> HarnessReleaseExecutionIdentity:
    return HarnessReleaseExecutionIdentity(
        release_digest=release.manifest.release_digest,
        release_manifest_digest=release.manifest.manifest_digest,
        epoch_id=release.epoch.epoch_id,
        epoch_manifest_digest=release.epoch.epoch_manifest_digest,
        deployment=release.manifest.deployment,
        protocol_source_digest=release.manifest.protocol_source_digest,
        dependency_manifest_digest=release.manifest.dependency_manifest_digest,
        profile_digest=release.manifest.profile_digest,
        release_evidence_index_digest=release.evidence_index.index_digest,
    )


def _task_execution_identity(task: TaskEnvelope) -> HarnessTaskExecutionIdentity:
    return HarnessTaskExecutionIdentity(
        task_manifest_id=task.task_manifest_id,
        task_envelope_digest=task.task_manifest_digest,
        capability_epoch=task.capability_epoch,
        data_state=task.data_state,
        split_manifest_digest=task.split_manifest_digest,
        snapshot_id=task.workspace_snapshot.snapshot_id,
        snapshot_digest=task.workspace_snapshot.digest,
    )


def _compiled_execution_identity(
    compiled_semantic_digest: str,
    dependencies: RuntimeDependencyManifest,
) -> HarnessCompiledExecutionIdentity:
    return HarnessCompiledExecutionIdentity(
        compiled_semantic_digest=compiled_semantic_digest,
        compiler_dependency_id=dependencies.compiler.dependency_id,
        compiler_interface_version=dependencies.compiler.interface_version,
        compiler_implementation_digest=dependencies.compiler.implementation_digest,
        harness_contract_implementation_digest=dependencies.harness_contract.implementation_digest,
        kernel_implementation_digest=dependencies.kernel.implementation_digest,
    )


def _verify_release_unchanged(
    release: LoadedHarnessRelease,
) -> None:
    try:
        current, *_ = validate_harness_release_generation(
            release.generation_path,
        )
    except Exception as exc:
        raise HarnessSolveError(
            "release_changed_during_run",
            "active release did not remain immutable during solve",
        ) from exc
    if current != release.manifest:
        raise HarnessSolveError(
            "release_changed_during_run",
            "active release identity changed during solve",
        )


def _normalize_pair_key(
    value: PairKey | Mapping[str, Any] | None,
    *,
    task: TaskEnvelope,
    release: LoadedHarnessRelease,
) -> PairKey | None:
    if value is None:
        return None
    try:
        payload = value.model_dump(mode="python") if isinstance(value, PairKey) else dict(value)
        pair_key = PairKey.model_validate(payload)
    except Exception as exc:
        raise HarnessSolveError(
            "pair_key_invalid",
            "evaluator PairKey failed strict validation",
        ) from exc
    if pair_key.task_manifest_id != task.task_manifest_id:
        raise HarnessSolveError(
            "pair_key_task_mismatch",
            "evaluator PairKey crossed the public task identity",
        )
    if (
        pair_key.provider_config_digest
        != release.manifest.deployment.provider_config_digest
    ):
        raise HarnessSolveError(
            "pair_key_provider_mismatch",
            "evaluator PairKey crossed the frozen provider configuration",
        )
    return pair_key


def _runtime_environment(
    *,
    pair_key: PairKey,
    task: TaskEnvelope,
    release: LoadedHarnessRelease,
) -> EnvironmentEvidence:
    profile = HarnessDeploymentProfile.model_validate(release.profile.profile)
    image = profile.command_container_policy.image
    _, marker, image_digest = image.rpartition("@sha256:")
    if not marker:
        raise HarnessSolveError(
            "command_image_identity_missing",
            "frozen command image lacks its required content digest",
        )
    payload = {
        "environment_id": pair_key.environment_id,
        "command_container_policy_digest": (
            release.manifest.deployment.command_container_policy_digest
        ),
        "python_identity": (
            f"{sys.implementation.name}-{platform.python_version()}"
        ),
        "platform_identity": platform.platform(),
        "workspace_snapshot_digest": task.workspace_snapshot.digest,
        "container_image_digest": image_digest.lower(),
        "network_policy": "none",
        "filesystem_policy": "scratch-workspace-only",
    }
    return EnvironmentEvidence(
        **payload,
        runtime_environment_digest=runtime_environment_evidence_digest(payload),
    )


def _provider_call_details(
    result: CompositeRunResult,
    *,
    release: LoadedHarnessRelease,
) -> tuple[ProviderCallDetail, ...]:
    details: list[ProviderCallDetail] = []
    sequence_no = 0
    deployment = release.manifest.deployment
    for call in result.actor_calls:
        for round_ in call.provider_rounds:
            sequence_no += 1
            details.append(
                ProviderCallDetail(
                    provider_call_id=(
                        f"provider.{call.call_id}.{round_.turn_index:04d}.attempt.0000"
                    ),
                    sequence_no=sequence_no,
                    call_id=call.call_id,
                    actor_id=call.actor_id,
                    turn_index=round_.turn_index,
                    attempt_index=0,
                    runtime_context_manifest_digest=call.context_manifest_digest,
                    reservation_id=round_.reservation_id,
                    deployment_id=deployment.deployment_id,
                    provider=deployment.provider,
                    model=deployment.model,
                    provider_config_digest=deployment.provider_config_digest,
                    request_digest=round_.request_digest,
                    status=round_.status,
                    request_sent=True,
                    response_id=round_.response_id,
                    response_digest=round_.response_digest,
                    response_kind=round_.response_kind,
                    tool_request_id=round_.tool_request_id,
                    started_at_ms=round_.started_at_ms,
                    finished_at_ms=round_.finished_at_ms,
                )
            )
    return tuple(details)


def _run_tool_receipts(
    result: CompositeRunResult,
    *,
    plan: Any,
) -> tuple[ToolReceiptEvidence, ...]:
    actions = {
        action.step_id: action for action in plan.public_verification.actions
    }
    status_map = {
        "succeeded": "succeeded",
        "failed": "failed",
        "timed_out": "timed_out",
        "output_limit": "blocked",
        "launch_failed": "blocked",
        "budget_rejected": "blocked",
    }
    evidence: list[ToolReceiptEvidence] = []
    for sequence_no, receipt in enumerate(result.tool_receipts, start=1):
        if receipt.phase == "actor_tool":
            invocation_digest = receipt.arguments_digest
        else:
            action = actions.get(str(receipt.verification_step_id))
            if action is None:
                raise HarnessSolveError(
                    "public_verification_receipt_mismatch",
                    "terminal tool receipt crossed the compiled verification plan",
                )
            invocation_digest = public_verification_action_digest(action)
        try:
            status = status_map[receipt.status.value]
        except KeyError as exc:
            raise HarnessSolveError(
                "tool_receipt_status_invalid",
                "runtime emitted an unsupported tool receipt status",
            ) from exc
        evidence.append(
            ToolReceiptEvidence(
                tool_call_id=f"tool.{receipt.receipt_id}",
                sequence_no=sequence_no,
                call_id=receipt.call_id,
                tool_id=receipt.tool_id,
                phase=receipt.phase,
                tool_request_id=receipt.tool_request_id,
                verification_step_id=receipt.verification_step_id,
                invocation_digest=invocation_digest,
                receipt_id=receipt.receipt_id,
                receipt_digest=canonical_identity_digest(
                    receipt.model_dump(mode="json"),
                    domain="repair-tool-receipt",
                ),
                status=status,
                output_digest=receipt.output_digest,
                output_bytes=receipt.output_bytes,
                retry_index=0,
                started_at_ms=receipt.started_at_ms,
                finished_at_ms=receipt.finished_at_ms,
            )
        )
    return tuple(evidence)


def _assert_completed_provider_provenance(
    provenance: ProviderExecutionProvenance,
    result: CompositeRunResult,
) -> None:
    provider_round_count = sum(
        len(call.provider_rounds) for call in result.actor_calls
    )
    if provider_round_count <= 0:
        raise HarnessSolveError(
            "provider_round_evidence_missing",
            "completed solve lacks provider request evidence",
        )
    if provenance.execution_mode == "live_provider" and (
        provenance.live_inference_status != "completed"
        or provenance.real_inference_requests_sent != provider_round_count
    ):
        raise HarnessSolveError(
            "provider_provenance_incomplete",
            "successful live solve provenance does not match sent provider rounds",
        )


def _assemble_controlled_run_evidence(
    *,
    plan: Any,
    task: TaskEnvelope,
    release: LoadedHarnessRelease,
    result: CompositeRunResult,
    pair_key: PairKey,
    provenance: ProviderExecutionProvenance,
) -> RunEvidence:
    provider_calls = _provider_call_details(result, release=release)
    tool_receipts = _run_tool_receipts(result, plan=plan)
    terminal_receipts = tuple(
        receipt
        for receipt in tool_receipts
        if receipt.phase == "terminal_public_verification"
    )
    completed_at_ms = max(
        [
            *(call.finished_at_ms for call in provider_calls),
            *(receipt.finished_at_ms for receipt in tool_receipts),
        ],
        default=0,
    )
    environment = _runtime_environment(
        pair_key=pair_key,
        task=task,
        release=release,
    )
    runtime_payload = result.model_dump(mode="json")
    assert_public_payload(runtime_payload)
    assert_no_resolved_credentials(runtime_payload)
    environment_healthy = bool(
        result.source_snapshot_unchanged
        and all(receipt.source_snapshot_unchanged for receipt in result.tool_receipts)
        and all(receipt.immutable_base_unchanged for receipt in result.tool_receipts)
    )
    try:
        return assemble_run_evidence(
            plan=plan,
            task=task,
            epoch=release.epoch,
            release_digest=release.manifest.release_digest,
            release_manifest_digest=release.manifest.manifest_digest,
            profile_digest=release.manifest.profile_digest,
            execution_mode=provenance.execution_mode,
            live_inference_status=provenance.live_inference_status,
            real_inference_requests_sent=provenance.real_inference_requests_sent,
            result=result,
            pair_key=pair_key,
            provider_calls=provider_calls,
            tool_receipts=tool_receipts,
            retries=(),
            public_verification=PublicVerificationEvidence(
                status=result.public_verification_status,
                plan_digest=public_verification_plan_digest(plan),
                patch_digest=result.final_patch_digest,
                action_receipt_digests=tuple(
                    receipt.receipt_digest for receipt in terminal_receipts
                ),
                completed_at_ms=completed_at_ms,
            ),
            environment=environment,
            final_budget=result.budget,
            no_leakage=True,
            environment_healthy=environment_healthy,
            completed_at_ms=completed_at_ms,
        )
    except Exception as exc:
        raise HarnessSolveError(
            "run_evidence_assembly_failed",
            "source runtime facts did not reconcile into authoritative RunEvidence",
        ) from exc


def _controlled_run_evidence_reference(
    evidence: RunEvidence,
) -> HarnessControlledRunEvidenceReference:
    return HarnessControlledRunEvidenceReference(
        evidence_id=evidence.evidence_id,
        evidence_digest=evidence.evidence_digest,
        pair_key_digest=pair_key_digest(evidence.pair_key),
        runtime_environment_digest=evidence.environment.runtime_environment_digest,
        release_digest=evidence.release_digest,
        release_manifest_digest=evidence.release_manifest_digest,
        task_manifest_digest=evidence.task_manifest_digest,
        protocol_digest=evidence.protocol_digest,
        compiled_semantic_digest=evidence.compiled_semantic_digest,
    )


def _persist_controlled_run_evidence(
    evidence: RunEvidence,
    workspace: Path,
) -> HarnessControlledRunEvidenceReference:
    payload = evidence.model_dump(mode="json")
    try:
        assert_public_payload(payload)
        assert_no_resolved_credentials(payload)
    except Exception as exc:
        raise HarnessSolveError(
            "run_evidence_persistence_refused",
            "controlled RunEvidence failed leakage or credential validation",
        ) from exc
    target = workspace / CONTROLLED_RUN_EVIDENCE_DIR / CONTROLLED_RUN_EVIDENCE_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)
        persisted = RunEvidence.model_validate_json(target.read_text(encoding="utf-8"))
        if persisted != evidence:
            raise HarnessSolveError(
                "run_evidence_persistence_mismatch",
                "controlled RunEvidence changed during persistence",
            )
    finally:
        temporary.unlink(missing_ok=True)
    return _controlled_run_evidence_reference(evidence)


def load_controlled_run_evidence(
    workspace: str | Path,
    reference: HarnessControlledRunEvidenceReference | Mapping[str, Any],
) -> RunEvidence:
    """Load and cross-bind the full evidence behind a public solve reference."""

    try:
        normalized_reference = HarnessControlledRunEvidenceReference.model_validate(
            reference.model_dump(mode="python")
            if isinstance(reference, HarnessControlledRunEvidenceReference)
            else dict(reference)
        )
        root = Path(workspace).expanduser().resolve()
        target = (root / Path(*normalized_reference.relative_path.split("/"))).resolve()
        if root not in target.parents or target.is_symlink() or not target.is_file():
            raise ValueError("controlled RunEvidence path escaped its run workspace")
        evidence = RunEvidence.model_validate_json(target.read_text(encoding="utf-8"))
        expected = _controlled_run_evidence_reference(evidence)
        if expected != normalized_reference:
            raise ValueError("controlled RunEvidence reference digest mismatch")
        return evidence
    except Exception as exc:
        if isinstance(exc, HarnessSolveError):
            raise
        raise HarnessSolveError(
            "run_evidence_load_failed",
            "controlled RunEvidence could not be loaded from its public reference",
        ) from exc


def _persist_result(
    result: HarnessSolveResult,
    workspace: Path,
) -> None:
    payload = result.model_dump(mode="json")
    try:
        assert_public_payload(payload)
        assert_no_resolved_credentials(payload)
    except Exception as exc:
        raise HarnessSolveError(
            "result_publication_refused",
            "solve result failed the public and credential boundary",
        ) from exc
    target = workspace / HARNESS_SOLVE_RESULT_FILE
    temporary = workspace / f".{HARNESS_SOLVE_RESULT_FILE}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def execute_harness_solve(
    project_root: str | Path,
    task: TaskEnvelope,
    *,
    provider: ControlledProvider,
    command_backend: IsolatedCommandBackend,
    run_artifact_workspace: str | Path | None = None,
    run_root: str | Path | None = None,
    snapshot_source_root: str | Path | None = None,
    run_id: str | None = None,
    workspace_id: str | None = None,
    pair_key: PairKey | Mapping[str, Any] | None = None,
    credential_reference: CredentialReference | None = None,
    public_session_context: HarnessPublicSessionContext | Mapping[str, Any] | None = None,
) -> HarnessSolveResult:
    """Execute one public task against the immutable active harness release.

    The provider and command backend are injected boundaries. This function never
    selects a provider, model, profile, credential value, or command environment.
    """

    normalized_task = _normalize_public_task(task)
    try:
        release = load_active_harness_release(project_root)
    except HarnessReleaseLoadError as exc:
        raise HarnessSolveError(exc.code, str(exc)) from exc
    _assert_task_bound_to_release(normalized_task, release)
    normalized_pair_key = _normalize_pair_key(
        pair_key,
        task=normalized_task,
        release=release,
    )
    normalized_public_session_context = _normalize_public_session_context(
        public_session_context,
        release=release,
    )
    deployment = _provider_deployment(provider)
    if deployment != release.manifest.deployment:
        raise HarnessSolveError(
            "provider_deployment_mismatch",
            "provider adapter deployment differs from the active release",
        )
    initial_provenance = _provider_execution_provenance(provider)
    if (
        initial_provenance.execution_mode == "live_provider"
        and (
            initial_provenance.live_inference_status != "not_run"
            or initial_provenance.real_inference_requests_sent != 0
        )
    ):
        raise HarnessSolveError(
            "provider_provenance_not_fresh",
            "live provider provenance must begin as a fresh not-run solve",
        )
    if initial_provenance.execution_mode == "deterministic_replay":
        if credential_reference is not None:
            raise HarnessSolveError(
                "replay_credential_forbidden",
                "deterministic replay may not receive a provider credential reference",
            )
        normalized_credentials = None
    else:
        frozen_credentials = _credential_reference_from_release(release)
        if credential_reference is not None:
            supplied = _normalize_credential_reference(
                credential_reference,
                deployment=deployment,
            )
            if supplied != frozen_credentials:
                raise HarnessSolveError(
                    "credential_reference_mismatch",
                    "credential reference differs from the frozen deployment profile",
                )
        normalized_credentials = _normalize_credential_reference(
            frozen_credentials,
            deployment=deployment,
        )
    if not callable(getattr(command_backend, "run", None)):
        raise HarnessSolveError(
            "command_backend_invalid",
            "command backend does not implement the isolated run boundary",
        )
    _validate_command_backend_policy(command_backend, release)
    try:
        source_root = (
            resolve_local_snapshot_uri(normalized_task.workspace_snapshot.uri)
            if snapshot_source_root is None
            else resolve_local_snapshot_uri(str(snapshot_source_root))
        )
        if repository_snapshot_digest(source_root) != normalized_task.workspace_snapshot.digest:
            raise RepositorySnapshotError("snapshot digest mismatch")
    except Exception as exc:
        raise HarnessSolveError(
            "snapshot_validation_failed",
            "workspace snapshot does not match its immutable public reference",
        ) from exc
    try:
        plan = compile_composite_run_plan(
            normalized_task,
            release.protocol,
            release.dependencies,
        )
    except CompositeCompilationError as exc:
        raise HarnessSolveError(
            "released_protocol_compilation_failed",
            "released protocol could not compile for the public task",
        ) from exc
    if (
        plan.task_envelope_digest != normalized_task.task_manifest_digest
        or plan.source_protocol_digest != release.manifest.protocol_source_digest
        or plan.dependency_manifest != release.dependencies
        or plan.dependency_manifest_digest != release.manifest.dependency_manifest_digest
    ):
        raise HarnessSolveError(
            "compiled_identity_mismatch",
            "compiled run plan crossed task, protocol, or dependency identity",
        )

    run_id = _execution_identity(run_id, prefix="run")
    workspace_id = _execution_identity(workspace_id, prefix="workspace")
    workspace_path = _claim_run_artifact_workspace(
        run_artifact_workspace=run_artifact_workspace,
        run_root=run_root,
        run_id=run_id,
        source_root=source_root,
        generation_path=release.generation_path,
    )
    try:
        task_workspace: TaskWorkspace = materialize_task_workspace(
            normalized_task.workspace_snapshot,
            workspace_path / "repository",
            source_root=source_root,
        )
        tool_service = TrustedRepairToolService(
            normalized_task,
            task_workspace,
            command_backend,
        )
        runtime = CompositeRuntime(
            plan,
            normalized_task,
            ScratchWorkspaceBinding(
                workspace_id=workspace_id,
                workspace_digest=normalized_task.workspace_snapshot.digest,
            ),
            provider,
            run_id=run_id,
            credential_reference=normalized_credentials,
            tool_interface=tool_service,
            public_session_context=normalized_public_session_context,
        )
    except Exception as exc:
        raise HarnessSolveError(
            "runtime_initialization_failed",
            "trusted repair runtime could not initialize",
        ) from exc

    try:
        runtime_result = runtime.run()
    except CompositeRuntimeError as exc:
        if not tool_service.source_snapshot_unchanged() or not tool_service.immutable_base_unchanged():
            raise HarnessSolveError(
                "immutable_source_changed",
                "source snapshot or immutable base changed during failed solve",
            ) from exc
        _verify_release_unchanged(release)
        budget = runtime.ledger.snapshot()
        final_provenance = _final_provider_execution_provenance(
            provider,
            initial_provenance,
        )
        result = HarnessSolveResult(
            run_id=run_id,
            workspace_id=workspace_id,
            status="failed",
            execution_mode=final_provenance.execution_mode,
            live_inference_status=final_provenance.live_inference_status,
            real_inference_requests_sent=final_provenance.real_inference_requests_sent,
            release=_release_execution_identity(release),
            task=_task_execution_identity(normalized_task),
            compiled=_compiled_execution_identity(
                plan.compiled_semantic_digest,
                release.dependencies,
            ),
            final_workspace_digest=tool_service.current_workspace_digest(),
            public_verification=HarnessPublicVerificationSummary(status="not_run"),
            termination=HarnessTerminationSummary(
                final_actor_call_id=plan.termination.final_actor_call_id,
                status="runtime_failure",
                failure_kind=exc.kind,
            ),
            budget=budget,
            evidence=_partial_evidence_index(
                runtime,
                tool_service,
                task=normalized_task,
                compiled_semantic_digest=plan.compiled_semantic_digest,
                release=release,
            ),
            failure=HarnessSolveFailure(
                kind=exc.kind,
                call_id=exc.call_id,
                provider=_provider_failure_summary(exc.provider_result),
            ),
            eligible_for_evaluator_submission=False,
        )
        _persist_result(result, workspace_path)
        return result

    if runtime_result.status not in {"completed", "public_verification_failed"}:
        raise HarnessSolveError(
            "runtime_completion_invalid",
            "trusted repair runtime returned a non-final execution status",
        )
    if not tool_service.source_snapshot_unchanged() or not tool_service.immutable_base_unchanged():
        raise HarnessSolveError(
            "immutable_source_changed",
            "source snapshot or immutable base changed during solve",
        )
    _verify_release_unchanged(release)
    verification = runtime_result.public_verification
    if verification is None:
        raise HarnessSolveError(
            "public_verification_missing",
            "trusted repair runtime omitted final public verification",
        )
    command_digests = _command_evidence_digests(verification)
    final_provenance = _final_provider_execution_provenance(
        provider,
        initial_provenance,
    )
    _assert_completed_provider_provenance(final_provenance, runtime_result)
    controlled_run_evidence = None
    if normalized_pair_key is not None:
        assembled_run_evidence = _assemble_controlled_run_evidence(
            plan=plan,
            task=normalized_task,
            release=release,
            result=runtime_result,
            pair_key=normalized_pair_key,
            provenance=final_provenance,
        )
        controlled_run_evidence = _persist_controlled_run_evidence(
            assembled_run_evidence,
            workspace_path,
        )
    result = HarnessSolveResult(
        run_id=run_id,
        workspace_id=workspace_id,
        status=runtime_result.status,
        execution_mode=final_provenance.execution_mode,
        live_inference_status=final_provenance.live_inference_status,
        real_inference_requests_sent=final_provenance.real_inference_requests_sent,
        release=_release_execution_identity(release),
        task=_task_execution_identity(normalized_task),
        compiled=_compiled_execution_identity(
            plan.compiled_semantic_digest,
            release.dependencies,
        ),
        final_workspace_digest=runtime_result.final_workspace_digest,
        submitted_patch=HarnessSubmittedPatch(
            unified_diff=runtime_result.final_patch,
            patch_digest=runtime_result.final_patch_digest,
            byte_size=len(runtime_result.final_patch.encode("utf-8")),
        ),
        public_verification=HarnessPublicVerificationSummary(
            status=runtime_result.public_verification_status,
            receipt_ids=verification.receipt_ids,
            command_evidence_digests=command_digests,
        ),
        termination=HarnessTerminationSummary(
            final_actor_call_id=plan.termination.final_actor_call_id,
            status=runtime_result.status,
        ),
        budget=runtime_result.budget,
        evidence=_success_evidence_index(runtime_result, release=release),
        controlled_run_evidence=controlled_run_evidence,
        eligible_for_evaluator_submission=(
            runtime_result.status in {"completed", "public_verification_failed"}
            and runtime_result.budget.reconciled
            and runtime_result.budget.healthy
            and controlled_run_evidence is not None
        ),
    )
    _persist_result(result, workspace_path)
    return result


__all__ = [
    "CONTROLLED_RUN_EVIDENCE_DIR",
    "CONTROLLED_RUN_EVIDENCE_FILE",
    "CONTROLLED_RUN_EVIDENCE_REF",
    "DeploymentBoundProvider",
    "HARNESS_SOLVE_RESULT_FILE",
    "HARNESS_SOLVE_RESULT_SCHEMA_VERSION",
    "HarnessArtifactDeliveryReference",
    "HarnessArtifactReference",
    "HarnessCompiledExecutionIdentity",
    "HarnessControlledRunEvidenceReference",
    "HarnessContextReference",
    "HarnessProviderFailureSummary",
    "HarnessProviderRoundReference",
    "HarnessPublicVerificationSummary",
    "HarnessReleaseExecutionIdentity",
    "HarnessRunEvidenceIndex",
    "HarnessSolveError",
    "HarnessSolveFailure",
    "HarnessSolveResult",
    "HarnessSubmittedPatch",
    "HarnessTaskExecutionIdentity",
    "HarnessTerminationSummary",
    "HarnessToolReceiptReference",
    "execute_harness_solve",
    "load_controlled_run_evidence",
]
