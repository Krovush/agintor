from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..contracts.epochs import (
    DeploymentIdentity,
    PromotionMargins,
    ResearchEpochManifest,
    SearchEnvelope,
    StopRule,
    TaskCeilings,
    TrustedToolAuthority,
)
from ..contracts.harness import (
    CompositeRunPlan,
    HarnessProtocol,
    RuntimeDependencyManifest,
)
from ..core.identity import evidence_digest
from ..runtime.harness_profile import (
    HarnessDeploymentProfile,
    harness_deployment_profile_digest,
)


HARNESS_RELEASE_SCHEMA_VERSION = "repo-repair-harness-release-v1"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class HarnessReleaseModel(BaseModel):
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


class PublicSearchLineageRecord(HarnessReleaseModel):
    sequence_no: int = Field(ge=0)
    transaction_id: str
    operator: Literal[
        "actor_split",
        "channel_add",
        "channel_rewire",
        "revision_insert",
        "revision_remove",
        "instruction_rewrite",
    ]
    parent_protocol_digest: str
    child_protocol_digest: str
    transaction_digest: str
    mechanism_hypothesis_digest: str
    status: Literal["accepted", "rejected"]

    @field_validator("transaction_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _identifier(value, "transaction_id")

    @field_validator(
        "parent_protocol_digest",
        "child_protocol_digest",
        "transaction_digest",
        "mechanism_hypothesis_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _digest(value, info.field_name)


class PublicSelectionDecision(HarnessReleaseModel):
    sequence_no: int = Field(ge=0)
    decision_id: str
    incumbent_protocol_digest: str
    candidate_protocol_digest: str
    selected_protocol_digest: str
    decision: Literal["retain_incumbent", "retain_candidate", "reject_invalid"]
    reason_codes: tuple[str, ...] = Field(min_length=1)
    evidence_digests: tuple[str, ...] = ()

    @field_validator("decision_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _identifier(value, "decision_id")

    @field_validator(
        "incumbent_protocol_digest",
        "candidate_protocol_digest",
        "selected_protocol_digest",
    )
    @classmethod
    def validate_protocol_digest(cls, value: str, info: Any) -> str:
        return _digest(value, info.field_name)

    @field_validator("evidence_digests")
    @classmethod
    def validate_evidence_digests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_digest(item, "evidence_digest") for item in value)
        if normalized != tuple(sorted(set(normalized))):
            raise ValueError("evidence_digests must be unique and sorted")
        return normalized

    @field_validator("reason_codes")
    @classmethod
    def validate_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_identifier(item, "reason_code") for item in value)
        if len(normalized) != len(set(normalized)):
            raise ValueError("reason_codes may not contain duplicates")
        return normalized


class Gate0PreregistrationPublic(HarnessReleaseModel):
    preregistration_id: str
    preregistration_digest: str = ""
    panel_digest: str
    deterministic_suite_digest: str
    planned_provider_calls: int = Field(gt=0)
    frozen_thresholds: dict[str, float] = Field(min_length=1)

    @field_validator("preregistration_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _identifier(value, "preregistration_id")

    @field_validator("panel_digest", "deterministic_suite_digest")
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _digest(value, info.field_name)

    @model_validator(mode="after")
    def bind_digest(self) -> "Gate0PreregistrationPublic":
        payload = self.model_dump(mode="python", exclude={"preregistration_digest"})
        computed = evidence_digest({"kind": "gate0-preregistration-public-v1", **payload})
        if self.preregistration_digest and self.preregistration_digest != computed:
            raise ValueError("preregistration_digest does not match frozen Gate0 inputs")
        if not self.preregistration_digest:
            object.__setattr__(self, "preregistration_digest", computed)
        return self


class Gate0NotRunReport(HarnessReleaseModel):
    status: Literal["not_run"] = "not_run"
    preregistration_digest: str
    reason: Literal["real_inference_not_authorized"] = "real_inference_not_authorized"

    @field_validator("preregistration_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _digest(value, "preregistration_digest")


class Gate0CompletedReport(HarnessReleaseModel):
    status: Literal["completed"] = "completed"
    provenance: Literal["authorized_live"] = "authorized_live"
    live_status: Literal["executed"] = "executed"
    preregistration_digest: str
    authorization_digest: str
    profile_digest: str
    manifest_digest: str
    execution_digest: str
    analysis_digest: str
    numerical_gate_passed: Literal[True] = True
    scheduled_call_count: int = Field(gt=0)
    completed_call_count: int = Field(gt=0)
    real_inference_requests_sent: int = Field(gt=0)
    total_known_cost_usd: float = Field(ge=0.0)
    total_estimated_cost_usd: float = Field(ge=0.0)

    @field_validator(
        "preregistration_digest",
        "authorization_digest",
        "profile_digest",
        "manifest_digest",
        "execution_digest",
        "analysis_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _digest(value, info.field_name)

    @model_validator(mode="after")
    def validate_completion(self) -> "Gate0CompletedReport":
        if self.completed_call_count != self.scheduled_call_count:
            raise ValueError("completed Gate0 report must cover its exact schedule")
        return self


class PilotNotRunSummary(HarnessReleaseModel):
    status: Literal["not_run"] = "not_run"
    pilot_id: str
    planned_task_manifest_digest: str
    non_confirmatory: Literal[True] = True
    reason: Literal["real_inference_not_authorized"] = "real_inference_not_authorized"

    @field_validator("pilot_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _identifier(value, "pilot_id")

    @field_validator("planned_task_manifest_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _digest(value, "planned_task_manifest_digest")


class CapabilityEpochPublicProjection(HarnessReleaseModel):
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

    @classmethod
    def from_epoch(cls, epoch: ResearchEpochManifest) -> "CapabilityEpochPublicProjection":
        return cls(
            runtime_contract_version=epoch.runtime_contract_version,
            epoch_id=epoch.epoch_id,
            epoch_manifest_digest=epoch.epoch_manifest_digest,
            capability_epoch=epoch.capability_epoch,
            promotion_capable=epoch.promotion_capable,
            task_manifest_digest=epoch.task_manifest_digest,
            development_split_digest=epoch.development_split_digest,
            deployment=epoch.deployment,
            per_run_ceilings=epoch.per_run_ceilings,
            search_envelope=epoch.search_envelope,
            trusted_tools=epoch.trusted_tools,
            mutation_surface=epoch.mutation_surface,
            promotion_margins=epoch.promotion_margins,
            stop_rule=epoch.stop_rule,
        )


class HarnessRuntimeProfileProjection(HarnessReleaseModel):
    runtime_kind: Literal["harness"] = "harness"
    deployment: DeploymentIdentity
    profile_digest: str
    profile: dict[str, Any]

    @field_validator("profile_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _digest(value, "profile_digest")

    @classmethod
    def from_profile(
        cls,
        profile: HarnessDeploymentProfile,
        deployment: DeploymentIdentity,
    ) -> "HarnessRuntimeProfileProjection":
        profile.validate_deployment_identity(deployment)
        payload = profile.model_dump(mode="json")
        return cls(
            deployment=deployment,
            profile_digest=harness_deployment_profile_digest(profile),
            profile=payload,
        )


class HarnessReleaseRequest(HarnessReleaseModel):
    runtime_kind: Literal["harness"] = "harness"
    epoch: ResearchEpochManifest
    selected_protocol: HarnessProtocol
    representative_plan: CompositeRunPlan
    dependency_manifest: RuntimeDependencyManifest
    deployment_profile: HarnessDeploymentProfile
    deployment: DeploymentIdentity
    search_lineage: tuple[PublicSearchLineageRecord, ...] = Field(min_length=1)
    selection_decisions: tuple[PublicSelectionDecision, ...] = Field(min_length=1)
    gate0_preregistration: Gate0PreregistrationPublic
    gate0_report: Gate0NotRunReport | Gate0CompletedReport
    search_execution_mode: Literal["offline_scripted", "live_provider"] = (
        "offline_scripted"
    )
    capability_promotion_authorized: bool = False
    capability_promotion_reason: str = (
        "implementation evidence only; live-provider proof is required for capability promotion"
    )
    pilot_summary: PilotNotRunSummary
    limitations: tuple[str, ...] = Field(min_length=1)

    @field_validator("limitations")
    @classmethod
    def validate_nonempty_strings(cls, value: tuple[str, ...], info: Any) -> tuple[str, ...]:
        normalized = tuple(str(item).strip() for item in value)
        if any(not item for item in normalized):
            raise ValueError(f"{info.field_name} may not contain empty values")
        if len(normalized) != len(set(normalized)):
            raise ValueError(f"{info.field_name} may not contain duplicates")
        return normalized

    @field_validator("capability_promotion_reason")
    @classmethod
    def validate_capability_reason(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("capability_promotion_reason may not be empty")
        return normalized

    @model_validator(mode="after")
    def validate_identity_graph(self) -> "HarnessReleaseRequest":
        protocol_digest = self.selected_protocol.source_digest()
        plan = self.representative_plan
        if plan.source_protocol_digest != protocol_digest:
            raise ValueError("representative plan is compiled from another HarnessProtocol")
        if plan.dependency_manifest != self.dependency_manifest:
            raise ValueError("representative plan dependency manifest differs from release input")
        if plan.dependency_manifest_digest != self.dependency_manifest.manifest_digest():
            raise ValueError("representative plan dependency identity mismatch")
        if self.deployment != self.epoch.deployment:
            raise ValueError("release deployment differs from the pinned research epoch")
        if not plan.budget_ledger.aggregate_ceiling.is_within(self.epoch.per_run_ceilings):
            raise ValueError("representative plan exceeds the pinned epoch ceilings")
        epoch_tools = {
            tool.tool_id: (tool.implementation_digest, tool.policy_digest)
            for tool in self.epoch.trusted_tools
        }
        dependency_tools = {
            tool.tool_id: (tool.implementation_digest, tool.policy_digest)
            for tool in self.dependency_manifest.trusted_tools
        }
        if epoch_tools != dependency_tools:
            raise ValueError("runtime dependency tools differ from epoch tool authority")
        self.deployment_profile.validate_deployment_identity(self.deployment)
        if self.deployment != self.epoch.deployment:
            raise ValueError("deployment profile differs from the pinned research epoch")
        if self.gate0_report.preregistration_digest != self.gate0_preregistration.preregistration_digest:
            raise ValueError("Gate0 report crossed its preregistration")
        if self.search_execution_mode == "live_provider":
            if (
                not isinstance(self.gate0_report, Gate0CompletedReport)
                or not self.capability_promotion_authorized
            ):
                raise ValueError(
                    "live capability release requires completed Gate0 and capability authority"
                )
        elif (
            not isinstance(self.gate0_report, Gate0NotRunReport)
            or self.capability_promotion_authorized
        ):
            raise ValueError(
                "offline release must remain a non-capability Gate0-not-run release"
            )
        sequences = [record.sequence_no for record in self.search_lineage]
        if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
            raise ValueError("public search lineage sequence numbers must be unique and ordered")
        decision_sequences = [record.sequence_no for record in self.selection_decisions]
        if decision_sequences != sorted(decision_sequences) or len(decision_sequences) != len(set(decision_sequences)):
            raise ValueError("public selection decision sequence numbers must be unique and ordered")
        if self.selection_decisions[-1].selected_protocol_digest != protocol_digest:
            raise ValueError("final public selection decision does not select the released protocol")
        return self


class PublicEvidenceIndex(HarnessReleaseModel):
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
            _relative_path(path, "evidence artifact path"): _digest(digest, "evidence artifact digest")
            for path, digest in value.items()
        }
        return dict(sorted(normalized.items()))

    @model_validator(mode="after")
    def bind_digest(self) -> "PublicEvidenceIndex":
        payload = self.model_dump(mode="python", exclude={"index_digest"})
        computed = evidence_digest({"kind": "public-release-evidence-index-v1", **payload})
        if self.index_digest and self.index_digest != computed:
            raise ValueError("evidence index digest mismatch")
        if not self.index_digest:
            object.__setattr__(self, "index_digest", computed)
        return self


class HarnessReleaseManifest(HarnessReleaseModel):
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

    @field_validator("file_digests")
    @classmethod
    def validate_files(cls, value: dict[str, str]) -> dict[str, str]:
        normalized = {
            _relative_path(path, "release file path"): _digest(digest, "release file digest")
            for path, digest in value.items()
        }
        if "public_release_evidence/release_manifest.json" in normalized:
            raise ValueError("release manifest cannot include its own file digest")
        return dict(sorted(normalized.items()))

    @model_validator(mode="after")
    def bind_manifest(self) -> "HarnessReleaseManifest":
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


class ActiveReleasePointer(HarnessReleaseModel):
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


class MaterializedHarnessRelease(HarnessReleaseModel):
    project_root: str
    generation_path: str
    manifest: HarnessReleaseManifest


__all__ = [
    "ActiveReleasePointer",
    "CapabilityEpochPublicProjection",
    "Gate0CompletedReport",
    "Gate0NotRunReport",
    "Gate0PreregistrationPublic",
    "HARNESS_RELEASE_SCHEMA_VERSION",
    "HarnessReleaseManifest",
    "HarnessReleaseRequest",
    "HarnessRuntimeProfileProjection",
    "MaterializedHarnessRelease",
    "PilotNotRunSummary",
    "PublicEvidenceIndex",
    "PublicSearchLineageRecord",
    "PublicSelectionDecision",
]
