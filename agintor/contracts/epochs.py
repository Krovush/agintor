from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..core.identity import canonical_identity_digest, task_digest
from ..core.versioning import RUNTIME_CONTRACT_VERSION


CapabilityEpoch = Literal["repo-repair-v1"]
DataState = Literal["development", "sealed_confirmation"]
TrustedToolId = Literal[
    "repo.search",
    "repo.read",
    "repo.public_test",
    "repo.edit",
    "repo.diff",
]

REPO_REPAIR_CAPABILITY_EPOCH = "repo-repair-v1"
REPO_REPAIR_DATA_STATES = ("development", "sealed_confirmation")
REPO_REPAIR_TRUSTED_TOOL_IDS = (
    "repo.search",
    "repo.read",
    "repo.public_test",
    "repo.edit",
    "repo.diff",
)
REPO_REPAIR_MUTATION_FIELDS = (
    "actors[*].task_view",
    "actors[*].instruction",
    "actors[*].tool_ids",
    "actors[*].budget_share_bps",
    "artifact_channels",
    "revision",
    "termination.final_actor_id",
)

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class EpochContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)


def _require_digest(value: str, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _DIGEST_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return normalized


def _require_nonempty(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} may not be empty")
    return normalized


def _assert_contract_version(value: str) -> str:
    normalized = str(value or "").strip()
    if normalized != RUNTIME_CONTRACT_VERSION:
        raise ValueError(
            "runtime_contract_version does not match this Agintor build: "
            f"expected {RUNTIME_CONTRACT_VERSION!r}, got {normalized!r}"
        )
    return normalized


def _public_relative_path(value: str, field_name: str) -> str:
    normalized = str(value or ".").strip().replace("\\", "/") or "."
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field_name} must be a workspace-relative path")
    return path.as_posix()


def _model_payload(value: BaseModel) -> dict[str, Any]:
    return value.model_dump(mode="python", exclude_none=True)


class TaskCeilings(EpochContractModel):
    max_model_calls: int = Field(gt=0)
    max_input_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    max_cached_tokens: int = Field(ge=0)
    max_cache_write_tokens: int = Field(default=0, ge=0)
    max_tool_calls: int = Field(gt=0)
    max_tool_output_bytes: int = Field(gt=0)
    max_artifact_bytes: int = Field(gt=0)
    max_patch_bytes: int = Field(gt=0)
    max_retries: int = Field(ge=0)
    max_wall_time_ms: int = Field(gt=0)
    provider_deadline_ms: int = Field(gt=0)
    max_known_cost_usd: float = Field(ge=0.0)
    max_estimated_cost_usd: float = Field(ge=0.0)

    @model_validator(mode="after")
    def validate_deadline(self) -> "TaskCeilings":
        if self.provider_deadline_ms > self.max_wall_time_ms:
            raise ValueError("provider_deadline_ms may not exceed max_wall_time_ms")
        if self.max_estimated_cost_usd < self.max_known_cost_usd:
            raise ValueError("max_estimated_cost_usd may not be less than max_known_cost_usd")
        return self

    def is_within(self, outer: "TaskCeilings") -> bool:
        return all(
            getattr(self, field_name) <= getattr(outer, field_name)
            for field_name in type(self).model_fields
        )


class WorkspaceSnapshotRef(EpochContractModel):
    snapshot_id: str
    uri: str
    digest: str
    format: Literal["directory"] = "directory"
    immutable: Literal[True] = True

    @field_validator("snapshot_id", "uri")
    @classmethod
    def validate_nonempty(cls, value: str, info: Any) -> str:
        return _require_nonempty(value, info.field_name)

    @field_validator("digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _require_digest(value, "workspace_snapshot.digest")


class PublicReproductionStep(EpochContractModel):
    step_id: str
    argv: tuple[str, ...]
    cwd: str = "."
    timeout_ms: int = Field(gt=0)
    expected_exit_codes: tuple[int, ...] = (0,)

    @field_validator("step_id")
    @classmethod
    def validate_step_id(cls, value: str) -> str:
        return _require_nonempty(value, "step_id")

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not str(part).strip() for part in value):
            raise ValueError("public reproduction argv must contain nonempty arguments")
        return tuple(str(part) for part in value)

    @field_validator("cwd")
    @classmethod
    def validate_cwd(cls, value: str) -> str:
        return _public_relative_path(value, "public reproduction cwd")

    @field_validator("expected_exit_codes")
    @classmethod
    def validate_expected_exit_codes(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if not value:
            raise ValueError("expected_exit_codes may not be empty")
        if len(value) != len(set(value)):
            raise ValueError("expected_exit_codes may not contain duplicates")
        return value


class DeploymentIdentity(EpochContractModel):
    deployment_id: str
    provider: str
    model: str
    provider_config_digest: str
    decoding_policy_digest: str
    price_schedule_digest: str
    command_container_policy_digest: str

    @field_validator("deployment_id", "provider", "model")
    @classmethod
    def validate_nonempty(cls, value: str, info: Any) -> str:
        return _require_nonempty(value, info.field_name)

    @field_validator(
        "provider_config_digest",
        "decoding_policy_digest",
        "price_schedule_digest",
        "command_container_policy_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)


class TrustedToolAuthority(EpochContractModel):
    tool_id: TrustedToolId
    implementation_digest: str
    policy_digest: str
    network_access: Literal[False] = False

    @field_validator("implementation_digest", "policy_digest")
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)


class SearchEnvelope(EpochContractModel):
    strategy: Literal["one_plus_lambda", "fixed_beam"] = "one_plus_lambda"
    max_steps: int = Field(gt=0)
    offspring_per_step: int = Field(gt=0)
    sampling_replicates: int = Field(gt=0)
    task_panel_digest: str
    racing_enabled: Literal[False] = False
    parallel_candidates: Literal[False] = False

    @field_validator("task_panel_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _require_digest(value, "task_panel_digest")


class FeedbackRules(EpochContractModel):
    development_outcomes_visible_to_search: Literal[True] = True
    sealed_confirmation_feedback_visible: Literal[False] = False
    adaptive_validation: Literal[False] = False
    shadow_feedback: Literal[False] = False


class PromotionMargins(EpochContractModel):
    minimum_complete_repair_gain: int = Field(default=1, ge=1)
    minimum_paired_receipts: int = Field(default=1, ge=1)
    maximum_pair_regressions: int = Field(default=0, ge=0)


class StopRule(EpochContractModel):
    max_candidate_evaluations: int = Field(gt=0)
    max_consecutive_non_improving_steps: int = Field(gt=0)
    stop_on_envelope_exhaustion: Literal[True] = True


class EvaluatorAuthority(EpochContractModel):
    evaluator_id: str
    evaluator_identity_digest: str
    evaluation_policy_digest: str
    outcome_schema: Literal["repo-repair-outcome-v1"] = "repo-repair-outcome-v1"

    @field_validator("evaluator_id")
    @classmethod
    def validate_evaluator_id(cls, value: str) -> str:
        return _require_nonempty(value, "evaluator_id")

    @field_validator("evaluator_identity_digest", "evaluation_policy_digest")
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)


class ResearchEpochManifest(EpochContractModel):
    runtime_contract_version: str = RUNTIME_CONTRACT_VERSION
    epoch_id: str
    epoch_manifest_digest: str = ""
    capability_epoch: CapabilityEpoch = REPO_REPAIR_CAPABILITY_EPOCH
    promotion_capable: Literal[True] = True
    data_states: tuple[DataState, DataState] = REPO_REPAIR_DATA_STATES
    task_manifest_digest: str
    development_split_digest: str
    sealed_confirmation_split_digest: str
    deployment: DeploymentIdentity
    per_run_ceilings: TaskCeilings
    search_envelope: SearchEnvelope
    trusted_tools: tuple[TrustedToolAuthority, ...]
    mutation_surface: tuple[str, ...] = REPO_REPAIR_MUTATION_FIELDS
    feedback_rules: FeedbackRules = Field(default_factory=FeedbackRules)
    promotion_margins: PromotionMargins = Field(default_factory=PromotionMargins)
    stop_rule: StopRule
    evaluator_authority: EvaluatorAuthority

    @field_validator("runtime_contract_version")
    @classmethod
    def validate_contract_version(cls, value: str) -> str:
        return _assert_contract_version(value)

    @field_validator("epoch_id")
    @classmethod
    def validate_epoch_id(cls, value: str) -> str:
        return _require_nonempty(value, "epoch_id")

    @field_validator(
        "task_manifest_digest",
        "development_split_digest",
        "sealed_confirmation_split_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @model_validator(mode="after")
    def validate_epoch(self) -> "ResearchEpochManifest":
        if tuple(self.data_states) != REPO_REPAIR_DATA_STATES:
            raise ValueError(
                "repo-repair-v1 has exactly two data states: development and sealed_confirmation"
            )
        if self.development_split_digest == self.sealed_confirmation_split_digest:
            raise ValueError("development and sealed-confirmation split digests must differ")
        tool_ids = tuple(tool.tool_id for tool in self.trusted_tools)
        if tool_ids != REPO_REPAIR_TRUSTED_TOOL_IDS:
            raise ValueError(
                "repo-repair-v1 trusted tools must be exactly "
                + ", ".join(REPO_REPAIR_TRUSTED_TOOL_IDS)
            )
        if not self.mutation_surface:
            raise ValueError("mutation_surface may not be empty")
        if len(self.mutation_surface) != len(set(self.mutation_surface)):
            raise ValueError("mutation_surface may not contain duplicates")
        unsupported = sorted(set(self.mutation_surface) - set(REPO_REPAIR_MUTATION_FIELDS))
        if unsupported:
            raise ValueError(f"unsupported repo-repair-v1 mutation fields: {unsupported}")
        if self.stop_rule.max_candidate_evaluations < self.search_envelope.offspring_per_step:
            raise ValueError("stop rule cannot be smaller than one offspring batch")
        computed = research_epoch_manifest_digest(self)
        if self.epoch_manifest_digest and self.epoch_manifest_digest != computed:
            raise ValueError("epoch_manifest_digest does not match the epoch manifest")
        if not self.epoch_manifest_digest:
            object.__setattr__(self, "epoch_manifest_digest", computed)
        return self


class TaskEnvelope(EpochContractModel):
    runtime_contract_version: str = RUNTIME_CONTRACT_VERSION
    task_manifest_id: str
    task_manifest_digest: str = ""
    epoch_id: str
    epoch_manifest_digest: str
    capability_epoch: CapabilityEpoch = REPO_REPAIR_CAPABILITY_EPOCH
    data_state: DataState
    split_manifest_digest: str
    issue: str
    workspace_snapshot: WorkspaceSnapshotRef
    public_reproduction: tuple[PublicReproductionStep, ...]
    allowed_capabilities: tuple[TrustedToolId, ...] = REPO_REPAIR_TRUSTED_TOOL_IDS
    ceilings: TaskCeilings

    @field_validator("runtime_contract_version")
    @classmethod
    def validate_contract_version(cls, value: str) -> str:
        return _assert_contract_version(value)

    @field_validator("task_manifest_id", "epoch_id", "issue")
    @classmethod
    def validate_nonempty(cls, value: str, info: Any) -> str:
        return _require_nonempty(value, info.field_name)

    @field_validator("epoch_manifest_digest", "split_manifest_digest")
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @model_validator(mode="after")
    def validate_task(self) -> "TaskEnvelope":
        if not self.public_reproduction:
            raise ValueError("repo-repair-v1 requires public reproduction steps")
        step_ids = [step.step_id for step in self.public_reproduction]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("public reproduction step_id values must be unique")
        if tuple(self.allowed_capabilities) != REPO_REPAIR_TRUSTED_TOOL_IDS:
            raise ValueError(
                "repo-repair-v1 allowed_capabilities must be the fixed trusted tool set"
            )
        computed = task_envelope_digest(self)
        if self.task_manifest_digest and self.task_manifest_digest != computed:
            raise ValueError("task_manifest_digest does not match the public task envelope")
        if not self.task_manifest_digest:
            object.__setattr__(self, "task_manifest_digest", computed)
        return self


def research_epoch_identity_payload(
    manifest: ResearchEpochManifest | Mapping[str, Any],
) -> dict[str, Any]:
    if isinstance(manifest, ResearchEpochManifest):
        payload = manifest.model_dump(mode="python", exclude_none=True)
    else:
        payload = dict(manifest)
    payload.pop("epoch_manifest_digest", None)
    return payload


def research_epoch_manifest_digest(
    manifest: ResearchEpochManifest | Mapping[str, Any],
) -> str:
    return canonical_identity_digest(
        research_epoch_identity_payload(manifest),
        domain="research-epoch-manifest",
    )


def task_envelope_identity_payload(
    task: TaskEnvelope | Mapping[str, Any],
) -> dict[str, Any]:
    if isinstance(task, TaskEnvelope):
        return {
            "runtime_contract_version": task.runtime_contract_version,
            "task_manifest_id": task.task_manifest_id,
            "epoch_id": task.epoch_id,
            "epoch_manifest_digest": task.epoch_manifest_digest,
            "capability_epoch": task.capability_epoch,
            "data_state": task.data_state,
            "split_manifest_digest": task.split_manifest_digest,
            "issue": task.issue,
            "workspace_snapshot": _model_payload(task.workspace_snapshot),
            "public_reproduction": [_model_payload(step) for step in task.public_reproduction],
            "allowed_capabilities": list(task.allowed_capabilities),
            "ceilings": _model_payload(task.ceilings),
        }
    payload = dict(task)
    payload.pop("task_manifest_digest", None)
    return payload


def task_envelope_digest(task: TaskEnvelope | Mapping[str, Any]) -> str:
    return task_digest(task_envelope_identity_payload(task))


def assert_task_bound_to_epoch(
    task: TaskEnvelope,
    epoch: ResearchEpochManifest,
) -> None:
    if task.runtime_contract_version != epoch.runtime_contract_version:
        raise ValueError("task and epoch runtime contract versions do not match")
    if task.epoch_id != epoch.epoch_id:
        raise ValueError("task epoch_id does not match the pinned research epoch")
    if task.epoch_manifest_digest != epoch.epoch_manifest_digest:
        raise ValueError("task epoch_manifest_digest does not match the pinned research epoch")
    if task.capability_epoch != epoch.capability_epoch:
        raise ValueError("task capability epoch does not match the pinned research epoch")
    expected_split = (
        epoch.development_split_digest
        if task.data_state == "development"
        else epoch.sealed_confirmation_split_digest
    )
    if task.split_manifest_digest != expected_split:
        raise ValueError("task split_manifest_digest does not match its epoch data state")
    if not task.ceilings.is_within(epoch.per_run_ceilings):
        raise ValueError("task ceilings exceed the pinned research epoch envelope")


def require_supported_capability_epoch(value: str | None) -> CapabilityEpoch:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(
            "an explicit capability epoch is required; generic goals are not promotion-capable"
        )
    if normalized != REPO_REPAIR_CAPABILITY_EPOCH:
        raise ValueError(
            f"unsupported capability epoch {normalized!r}; only repo-repair-v1 is supported"
        )
    return REPO_REPAIR_CAPABILITY_EPOCH


def ceilings_usage_within(
    usage: Mapping[str, int | float],
    ceilings: TaskCeilings,
) -> bool:
    field_names = set(TaskCeilings.model_fields)
    unknown = sorted(set(usage) - field_names)
    if unknown:
        raise ValueError(f"unknown task-ceiling usage fields: {unknown}")
    return all(float(value) <= float(getattr(ceilings, name)) for name, value in usage.items())


__all__ = [
    "CapabilityEpoch",
    "DataState",
    "DeploymentIdentity",
    "EpochContractModel",
    "EvaluatorAuthority",
    "FeedbackRules",
    "PromotionMargins",
    "PublicReproductionStep",
    "REPO_REPAIR_CAPABILITY_EPOCH",
    "REPO_REPAIR_DATA_STATES",
    "REPO_REPAIR_MUTATION_FIELDS",
    "REPO_REPAIR_TRUSTED_TOOL_IDS",
    "ResearchEpochManifest",
    "SearchEnvelope",
    "StopRule",
    "TaskCeilings",
    "TaskEnvelope",
    "TrustedToolAuthority",
    "TrustedToolId",
    "WorkspaceSnapshotRef",
    "assert_task_bound_to_epoch",
    "ceilings_usage_within",
    "require_supported_capability_epoch",
    "research_epoch_identity_payload",
    "research_epoch_manifest_digest",
    "task_envelope_digest",
    "task_envelope_identity_payload",
]
