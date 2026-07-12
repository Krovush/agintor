from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..core.identity import canonical_identity_digest, composite_plan_digest, protocol_digest
from ..core.versioning import RUNTIME_CONTRACT_VERSION
from .epochs import TaskCeilings, TrustedToolId


HARNESS_PROTOCOL_SCHEMA_VERSION = "repo-repair-harness-v1"
COMPOSITE_RUN_PLAN_SCHEMA_VERSION = "repo-repair-composite-plan-v1"
RUNTIME_DEPENDENCY_MANIFEST_SCHEMA_VERSION = "repo-repair-runtime-dependencies-v1"
CONSUMED_FIELD_LIVENESS_SCHEMA_VERSION = "repo-repair-consumed-fields-v1"
PUBLIC_SESSION_CONTEXT_SCHEMA_VERSION = "repo-repair-public-session-context-v1"

TaskViewField = Literal[
    "issue",
    "workspace",
    "public_reproduction",
    "session_public_carryover",
]
_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_PORTABLE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_SECRET_VALUE_PATTERNS = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
_FORBIDDEN_PUBLIC_SESSION_TEXT_FRAGMENTS = (
    "api_key",
    "authorization",
    "bearer ",
    "credential",
    "evaluator",
    "full_context",
    "hidden",
    "long_term",
    "password",
    "predictor",
    "private_key",
    "raw_patch",
    "repository_snapshot",
    "sealed",
    "source_uri",
    "token",
    "workspace_snapshot",
)
_FORBIDDEN_PUBLIC_SESSION_TEXT_PHRASES = (
    "full context",
    "pre-call context",
    "raw patch",
    "repository snapshot",
    "workspace snapshot",
)
NO_PUBLIC_SESSION_CONTEXT_DIGEST = canonical_identity_digest(
    {"kind": "no-public-session-context"},
    domain="harness-public-session-context",
)


class HarnessContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        use_enum_values=True,
    )


def _require_identifier(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not _IDENTIFIER_RE.fullmatch(normalized):
        raise ValueError(
            f"{field_name} must start with a lowercase letter and contain only "
            "lowercase letters, digits, '.', '_', or '-'"
        )
    return normalized


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


def _require_portable_id(value: str, field_name: str) -> str:
    normalized = _require_nonempty(value, field_name)
    if (
        not _PORTABLE_ID_RE.fullmatch(normalized)
        or normalized.startswith(".")
        or ".." in normalized
        or "/" in normalized
        or "\\" in normalized
    ):
        raise ValueError(f"{field_name} must be a portable public identifier")
    return normalized


def _safe_artifact_ref(value: str) -> str:
    normalized = str(value or "").strip().replace("\\", "/")
    if not normalized:
        raise ValueError("artifact_ref may not be empty")
    if "://" in normalized or normalized.startswith(("/", ".")):
        raise ValueError("artifact_ref may not traverse or reference absolute/hidden filesystem state")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or any(part.startswith(".") for part in path.parts):
        raise ValueError("artifact_ref may not traverse or hide filesystem state")
    return normalized


def _assert_public_session_text(value: str, *, field_name: str) -> None:
    normalized = str(value)
    lowered = normalized.casefold()
    compact = re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")
    if any(pattern.search(normalized) for pattern in _SECRET_VALUE_PATTERNS):
        raise ValueError(f"{field_name} contains resolved credential material")
    for fragment in _FORBIDDEN_PUBLIC_SESSION_TEXT_FRAGMENTS:
        if fragment in compact or fragment in lowered:
            raise ValueError(f"{field_name} references non-public session state: {fragment}")
    for phrase in _FORBIDDEN_PUBLIC_SESSION_TEXT_PHRASES:
        if phrase in lowered:
            raise ValueError(f"{field_name} references non-public session state: {phrase}")


class HarnessActor(HarnessContractModel):
    actor_id: str
    task_view: tuple[TaskViewField, ...] = Field(min_length=1)
    instruction: str = Field(min_length=1)
    tool_ids: tuple[TrustedToolId, ...] = Field(min_length=1)
    budget_share_bps: int = Field(gt=0, le=10_000)

    @field_validator("actor_id")
    @classmethod
    def validate_actor_id(cls, value: str) -> str:
        return _require_identifier(value, "actor_id")

    @field_validator("task_view")
    @classmethod
    def validate_task_view(
        cls,
        value: tuple[TaskViewField, ...],
    ) -> tuple[TaskViewField, ...]:
        if len(value) != len(set(value)):
            raise ValueError("task_view may not contain duplicate fields")
        return value

    @field_validator("tool_ids")
    @classmethod
    def validate_tool_ids(
        cls,
        value: tuple[TrustedToolId, ...],
    ) -> tuple[TrustedToolId, ...]:
        if len(value) != len(set(value)):
            raise ValueError("tool_ids may not contain duplicates")
        return tuple(sorted(value))


class HarnessArtifactChannel(HarnessContractModel):
    channel_id: str
    producer_actor_id: str
    consumer_actor_id: str
    payload_kind: Literal["text"] = "text"

    @field_validator("channel_id", "producer_actor_id", "consumer_actor_id")
    @classmethod
    def validate_identifier(cls, value: str, info: Any) -> str:
        return _require_identifier(value, info.field_name)

    @model_validator(mode="after")
    def validate_distinct_actors(self) -> "HarnessArtifactChannel":
        if self.producer_actor_id == self.consumer_actor_id:
            raise ValueError("artifact channels must connect two different actors")
        return self


class HarnessRevision(HarnessContractModel):
    actor_id: str
    feedback_channel_id: str
    instruction: str = Field(min_length=1)

    @field_validator("actor_id", "feedback_channel_id")
    @classmethod
    def validate_identifier(cls, value: str, info: Any) -> str:
        return _require_identifier(value, info.field_name)


class HarnessTermination(HarnessContractModel):
    final_actor_id: str

    @field_validator("final_actor_id")
    @classmethod
    def validate_final_actor_id(cls, value: str) -> str:
        return _require_identifier(value, "final_actor_id")


class HarnessProtocol(HarnessContractModel):
    schema_version: Literal[HARNESS_PROTOCOL_SCHEMA_VERSION] = HARNESS_PROTOCOL_SCHEMA_VERSION
    actors: tuple[HarnessActor, ...] = Field(min_length=1)
    artifact_channels: tuple[HarnessArtifactChannel, ...] = ()
    revision: HarnessRevision | None = None
    termination: HarnessTermination

    @model_validator(mode="after")
    def validate_protocol(self) -> "HarnessProtocol":
        actor_ids = [actor.actor_id for actor in self.actors]
        if len(actor_ids) != len(set(actor_ids)):
            raise ValueError("actor_id values must be unique")
        if sum(actor.budget_share_bps for actor in self.actors) != 10_000:
            raise ValueError("actor budget_share_bps values must sum to 10000")

        actor_id_set = set(actor_ids)
        if self.termination.final_actor_id not in actor_id_set:
            raise ValueError("termination.final_actor_id must reference an actor")

        channel_ids = [channel.channel_id for channel in self.artifact_channels]
        if len(channel_ids) != len(set(channel_ids)):
            raise ValueError("channel_id values must be unique")
        for channel in self.artifact_channels:
            if channel.producer_actor_id not in actor_id_set:
                raise ValueError(
                    f"channel {channel.channel_id!r} references an unknown producer actor"
                )
            if channel.consumer_actor_id not in actor_id_set:
                raise ValueError(
                    f"channel {channel.channel_id!r} references an unknown consumer actor"
                )

        if self.revision is not None:
            if self.revision.actor_id != self.termination.final_actor_id:
                raise ValueError("the one revising actor must also be the final actor")
            channels = {
                channel.channel_id: channel for channel in self.artifact_channels
            }
            feedback = channels.get(self.revision.feedback_channel_id)
            if feedback is None:
                raise ValueError("revision.feedback_channel_id must reference a channel")
            if feedback.consumer_actor_id != self.revision.actor_id:
                raise ValueError(
                    "the revision feedback channel must be delivered to the revising actor"
                )
        return self

    def source_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)

    def source_digest(self) -> str:
        return protocol_digest(self.source_payload())

    def mutable_field_paths(self) -> tuple[str, ...]:
        paths: list[str] = []
        for actor in self.actors:
            prefix = f"actors[{actor.actor_id}]"
            paths.extend(
                (
                    f"{prefix}.task_view",
                    f"{prefix}.instruction",
                    f"{prefix}.tool_ids",
                    f"{prefix}.budget_share_bps",
                )
            )
        for channel in self.artifact_channels:
            prefix = f"artifact_channels[{channel.channel_id}]"
            paths.extend(
                (
                    f"{prefix}.producer_actor_id",
                    f"{prefix}.consumer_actor_id",
                )
            )
        if self.revision is not None:
            paths.extend(
                (
                    "revision.actor_id",
                    "revision.feedback_channel_id",
                    "revision.instruction",
                )
            )
        paths.append("termination.final_actor_id")
        return tuple(paths)


class DependencyRef(HarnessContractModel):
    dependency_id: str
    interface_version: str = Field(min_length=1)
    implementation_digest: str

    @field_validator("dependency_id")
    @classmethod
    def validate_dependency_id(cls, value: str) -> str:
        return _require_identifier(value, "dependency_id")

    @field_validator("implementation_digest")
    @classmethod
    def validate_implementation_digest(cls, value: str) -> str:
        return _require_digest(value, "implementation_digest")


class TrustedToolDependency(HarnessContractModel):
    tool_id: TrustedToolId
    interface_version: str = Field(min_length=1)
    implementation_digest: str
    policy_digest: str

    @field_validator("implementation_digest", "policy_digest")
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)


class RuntimeDependencyManifest(HarnessContractModel):
    schema_version: Literal[RUNTIME_DEPENDENCY_MANIFEST_SCHEMA_VERSION] = (
        RUNTIME_DEPENDENCY_MANIFEST_SCHEMA_VERSION
    )
    runtime_contract_version: str = RUNTIME_CONTRACT_VERSION
    compiler: DependencyRef
    harness_contract: DependencyRef
    kernel: DependencyRef
    trusted_tools: tuple[TrustedToolDependency, ...]

    @model_validator(mode="after")
    def validate_manifest(self) -> "RuntimeDependencyManifest":
        tool_ids = [tool.tool_id for tool in self.trusted_tools]
        if len(tool_ids) != len(set(tool_ids)):
            raise ValueError("trusted tool dependencies must have unique tool_id values")
        if tool_ids != sorted(tool_ids):
            raise ValueError("trusted tool dependencies must be sorted by tool_id")
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def manifest_digest(self) -> str:
        return canonical_identity_digest(
            self.identity_payload(),
            domain="runtime-dependency-manifest",
        )


class CanonicalHarnessSeedReference(HarnessContractModel):
    seed_id: str
    capability_epoch: Literal["repo-repair-v1"] = "repo-repair-v1"
    resource_path: str = Field(min_length=1)
    source_protocol_digest: str

    @field_validator("seed_id")
    @classmethod
    def validate_seed_id(cls, value: str) -> str:
        return _require_identifier(value, "seed_id")

    @field_validator("source_protocol_digest")
    @classmethod
    def validate_source_protocol_digest(cls, value: str) -> str:
        return _require_digest(value, "source_protocol_digest")


class HarnessSeedDocument(HarnessContractModel):
    reference: CanonicalHarnessSeedReference
    protocol: HarnessProtocol


class CompositeCompilerMetadata(HarnessContractModel):
    compiler_id: str
    compiler_version: str = Field(min_length=1)
    harness_contract_id: str
    harness_schema_version: Literal[HARNESS_PROTOCOL_SCHEMA_VERSION]
    composite_plan_schema_version: Literal[COMPOSITE_RUN_PLAN_SCHEMA_VERSION]
    required_kernel_capabilities: tuple[str, ...] = Field(min_length=1)
    trusted_tool_ids: tuple[TrustedToolId, ...]
    canonical_seed: CanonicalHarnessSeedReference

    @field_validator("compiler_id", "harness_contract_id")
    @classmethod
    def validate_identifier(cls, value: str, info: Any) -> str:
        return _require_identifier(value, info.field_name)

    @model_validator(mode="after")
    def validate_metadata(self) -> "CompositeCompilerMetadata":
        if len(self.required_kernel_capabilities) != len(
            set(self.required_kernel_capabilities)
        ):
            raise ValueError("required_kernel_capabilities may not contain duplicates")
        if len(self.trusted_tool_ids) != len(set(self.trusted_tool_ids)):
            raise ValueError("trusted_tool_ids may not contain duplicates")
        return self


class HarnessPublicSessionLimits(HarnessContractModel):
    max_entries: int = Field(default=8, ge=0, le=64)
    max_total_bytes: int = Field(default=4096, ge=0, le=262_144)
    max_summary_bytes: int = Field(default=512, ge=0, le=16_384)


class HarnessPublicCarryoverRef(HarnessContractModel):
    artifact_ref: str
    artifact_digest: str
    summary: str
    public_safe: Literal[True] = True
    carryover_digest: str = ""

    @field_validator("artifact_ref")
    @classmethod
    def validate_artifact_ref(cls, value: str) -> str:
        return _safe_artifact_ref(value)

    @field_validator("artifact_digest")
    @classmethod
    def validate_artifact_digest(cls, value: str) -> str:
        return _require_digest(value, "artifact_digest")

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        normalized = _require_nonempty(value, "summary")
        _assert_public_session_text(normalized, field_name="summary")
        return normalized

    @model_validator(mode="after")
    def bind_digest(self) -> "HarnessPublicCarryoverRef":
        computed = harness_public_carryover_ref_digest(self)
        if self.carryover_digest and self.carryover_digest != computed:
            raise ValueError("carryover digest does not match the public artifact reference")
        if not self.carryover_digest:
            object.__setattr__(self, "carryover_digest", computed)
        return self


class HarnessPublicSessionContext(HarnessContractModel):
    schema_version: Literal[PUBLIC_SESSION_CONTEXT_SCHEMA_VERSION] = (
        PUBLIC_SESSION_CONTEXT_SCHEMA_VERSION
    )
    session_id: str
    active_release_digest: str
    session_manifest_digest: str
    next_sequence: int = Field(ge=0)
    parent_message_id: str | None = None
    limits: HarnessPublicSessionLimits = Field(default_factory=HarnessPublicSessionLimits)
    carryover: tuple[HarnessPublicCarryoverRef, ...] = ()
    context_digest: str = ""

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str) -> str:
        return _require_portable_id(value, "session_id")

    @field_validator("parent_message_id")
    @classmethod
    def validate_parent_message_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_portable_id(value, "parent_message_id")

    @field_validator("active_release_digest", "session_manifest_digest")
    @classmethod
    def validate_digests(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @model_validator(mode="after")
    def validate_context(self) -> "HarnessPublicSessionContext":
        if len(self.carryover) > self.limits.max_entries:
            raise ValueError("public session carryover exceeds the configured entry limit")
        artifact_refs = [entry.artifact_ref for entry in self.carryover]
        if len(artifact_refs) != len(set(artifact_refs)):
            raise ValueError("public session carryover artifact_refs must be unique")
        total_bytes = 0
        for entry in self.carryover:
            if len(entry.summary.encode("utf-8")) > self.limits.max_summary_bytes:
                raise ValueError("public session carryover summary exceeds the configured summary byte limit")
            total_bytes += len(
                _canonical_public_session_bytes(entry.model_dump(mode="json"))
            )
        if total_bytes > self.limits.max_total_bytes:
            raise ValueError("public session carryover exceeds the configured total byte limit")
        computed = public_session_context_digest(
            self.model_dump(mode="json", exclude={"context_digest"})
        )
        if self.context_digest and self.context_digest != computed:
            raise ValueError("public session context digest mismatch")
        if not self.context_digest:
            object.__setattr__(self, "context_digest", computed)
        return self

    def actor_visible_value(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "parent_message_id": self.parent_message_id,
            "next_sequence": self.next_sequence,
            "carryover": [
                entry.model_dump(mode="json")
                for entry in self.carryover
            ],
            "carryover_count": len(self.carryover),
            "context_digest": self.context_digest,
        }


def _canonical_public_session_bytes(value: Any) -> bytes:
    import json

    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def harness_public_carryover_ref_digest(
    ref: HarnessPublicCarryoverRef | Mapping[str, Any],
) -> str:
    payload = ref.model_dump(mode="python", exclude={"carryover_digest"}) if isinstance(
        ref,
        HarnessPublicCarryoverRef,
    ) else dict(ref)
    payload.pop("carryover_digest", None)
    return canonical_identity_digest(
        {"kind": "harness-public-carryover-ref", "ref": payload},
        domain="harness-public-carryover-ref",
    )


def public_session_context_digest(
    context: HarnessPublicSessionContext | Mapping[str, Any] | None,
) -> str:
    if context is None:
        return NO_PUBLIC_SESSION_CONTEXT_DIGEST
    if isinstance(context, HarnessPublicSessionContext):
        payload = context.model_dump(mode="json", exclude={"context_digest"})
    else:
        payload = dict(context)
        payload.pop("context_digest", None)
    return canonical_identity_digest(
        {"kind": "harness-public-session-context", "context": payload},
        domain="harness-public-session-context",
    )


class ContextReadPlan(HarnessContractModel):
    read_id: str
    source_kind: Literal["task", "workspace", "artifact", "prior_actor_output", "session"]
    source_ref: str
    required: Literal[True] = True


class ArtifactWritePlan(HarnessContractModel):
    channel_id: str
    artifact_id: str
    producer_call_id: str
    payload_kind: Literal["text"] = "text"
    immutable: Literal[True] = True
    max_bytes: int = Field(gt=0)


class ArtifactDeliveryPlan(HarnessContractModel):
    channel_id: str
    artifact_id: str
    producer_call_id: str
    consumer_call_id: str
    deliver_before_call: Literal[True] = True


class ActorCallPlan(HarnessContractModel):
    call_id: str
    actor_id: str
    call_kind: Literal["initial", "revision"]
    instruction: str
    revision_instruction: str | None = None
    context_reads: tuple[ContextReadPlan, ...]
    artifact_writes: tuple[ArtifactWritePlan, ...]
    tool_ids: tuple[TrustedToolId, ...]
    budget_share_bps: int = Field(gt=0, le=10_000)
    emits_final_patch: bool = False

    @model_validator(mode="after")
    def validate_call_kind(self) -> "ActorCallPlan":
        if self.call_kind == "revision" and not self.revision_instruction:
            raise ValueError("revision calls require revision_instruction")
        if self.call_kind == "initial" and self.revision_instruction is not None:
            raise ValueError("initial calls may not set revision_instruction")
        return self


class CompositePlanStage(HarnessContractModel):
    stage_id: str
    stage_index: int = Field(ge=0)
    call_ids: tuple[str, ...] = Field(min_length=1)
    depends_on_stage_ids: tuple[str, ...] = ()
    inbound_channel_ids: tuple[str, ...] = ()
    fork: bool = False
    join: bool = False
    revision: bool = False


class PublicVerificationActionPlan(HarnessContractModel):
    step_id: str
    argv: tuple[str, ...] = Field(min_length=1)
    cwd: str
    timeout_ms: int = Field(gt=0)
    expected_exit_codes: tuple[int, ...] = Field(min_length=1)


class PublicVerificationPlan(HarnessContractModel):
    required: Literal[True] = True
    actions: tuple[PublicVerificationActionPlan, ...] = Field(min_length=1)
    run_after_call_id: str


class TerminationPlan(HarnessContractModel):
    final_actor_call_id: str
    final_output_kind: Literal["unified_diff"] = "unified_diff"
    max_patch_bytes: int = Field(gt=0)
    success_condition: Literal[
        "patch_emitted_and_public_verification_completed"
    ] = "patch_emitted_and_public_verification_completed"


class ActorBudgetSharePlan(HarnessContractModel):
    actor_id: str
    budget_share_bps: int = Field(gt=0, le=10_000)


class BudgetLedgerPlan(HarnessContractModel):
    aggregate_ceiling: TaskCeilings
    actor_shares: tuple[ActorBudgetSharePlan, ...] = Field(min_length=1)
    scheduled_model_calls: int = Field(gt=0)
    scheduled_revision_calls: int = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_ledger(self) -> "BudgetLedgerPlan":
        if sum(item.budget_share_bps for item in self.actor_shares) != 10_000:
            raise ValueError("compiled actor budget shares must sum to 10000")
        if self.scheduled_model_calls > self.aggregate_ceiling.max_model_calls:
            raise ValueError("compiled actor calls exceed max_model_calls")
        return self


class ConsumedFieldBinding(HarnessContractModel):
    source_path: str
    source_value_digest: str
    plan_consumer_paths: tuple[str, ...] = Field(min_length=1)
    runtime_owners: tuple[
        Literal[
            "actor_context",
            "artifact_store",
            "tool_authority",
            "budget_ledger",
            "scheduler",
            "revision_controller",
            "termination_controller",
        ],
        ...,
    ] = Field(min_length=1)

    @field_validator("source_value_digest")
    @classmethod
    def validate_source_value_digest(cls, value: str) -> str:
        return _require_digest(value, "source_value_digest")


class ConsumedFieldLivenessManifest(HarnessContractModel):
    schema_version: Literal[CONSUMED_FIELD_LIVENESS_SCHEMA_VERSION] = (
        CONSUMED_FIELD_LIVENESS_SCHEMA_VERSION
    )
    source_protocol_digest: str
    bindings: tuple[ConsumedFieldBinding, ...] = Field(min_length=1)

    @field_validator("source_protocol_digest")
    @classmethod
    def validate_source_protocol_digest(cls, value: str) -> str:
        return _require_digest(value, "source_protocol_digest")

    @model_validator(mode="after")
    def validate_bindings(self) -> "ConsumedFieldLivenessManifest":
        paths = [binding.source_path for binding in self.bindings]
        if len(paths) != len(set(paths)):
            raise ValueError("liveness bindings must have unique source paths")
        return self

    def consumer_paths_for(self, source_path: str) -> tuple[str, ...]:
        for binding in self.bindings:
            if binding.source_path == source_path:
                return binding.plan_consumer_paths
        raise KeyError(source_path)


class CompositeRunPlan(HarnessContractModel):
    schema_version: Literal[COMPOSITE_RUN_PLAN_SCHEMA_VERSION] = (
        COMPOSITE_RUN_PLAN_SCHEMA_VERSION
    )
    task_envelope_digest: str
    source_protocol_digest: str
    compiled_semantic_digest: str
    dependency_manifest: RuntimeDependencyManifest
    dependency_manifest_digest: str
    actor_calls: tuple[ActorCallPlan, ...] = Field(min_length=1)
    stages: tuple[CompositePlanStage, ...] = Field(min_length=1)
    artifact_deliveries: tuple[ArtifactDeliveryPlan, ...]
    public_verification: PublicVerificationPlan
    termination: TerminationPlan
    budget_ledger: BudgetLedgerPlan
    liveness_manifest: ConsumedFieldLivenessManifest

    @field_validator(
        "task_envelope_digest",
        "source_protocol_digest",
        "compiled_semantic_digest",
        "dependency_manifest_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @model_validator(mode="after")
    def validate_plan_references(self) -> "CompositeRunPlan":
        call_ids = [call.call_id for call in self.actor_calls]
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("compiled call_id values must be unique")
        call_id_set = set(call_ids)
        staged_call_ids = [call_id for stage in self.stages for call_id in stage.call_ids]
        if len(staged_call_ids) != len(set(staged_call_ids)):
            raise ValueError("every actor call must appear in exactly one stage")
        if set(staged_call_ids) != call_id_set:
            raise ValueError("stages must schedule every actor call exactly once")
        if self.termination.final_actor_call_id not in call_id_set:
            raise ValueError("termination references an unknown final actor call")
        if self.public_verification.run_after_call_id != self.termination.final_actor_call_id:
            raise ValueError("public verification must run after the final actor call")
        if self.source_protocol_digest != self.liveness_manifest.source_protocol_digest:
            raise ValueError("liveness manifest is bound to another source protocol")
        if self.dependency_manifest_digest != self.dependency_manifest.manifest_digest():
            raise ValueError("dependency_manifest_digest does not match its manifest")
        if self.compiled_semantic_digest != composite_plan_digest(
            self.semantic_payload()
        ):
            raise ValueError("compiled_semantic_digest does not match the normalized plan")
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_envelope_digest": self.task_envelope_digest,
            "dependency_manifest": self.dependency_manifest.model_dump(mode="json"),
            "dependency_manifest_digest": self.dependency_manifest_digest,
            "actor_calls": [call.model_dump(mode="json") for call in self.actor_calls],
            "stages": [stage.model_dump(mode="json") for stage in self.stages],
            "artifact_deliveries": [
                delivery.model_dump(mode="json")
                for delivery in self.artifact_deliveries
            ],
            "public_verification": self.public_verification.model_dump(mode="json"),
            "termination": self.termination.model_dump(mode="json"),
            "budget_ledger": self.budget_ledger.model_dump(mode="json"),
        }


def source_field_digest(value: Any) -> str:
    return canonical_identity_digest(value, domain="harness-source-field")


__all__ = [
    "ActorBudgetSharePlan",
    "ActorCallPlan",
    "ArtifactDeliveryPlan",
    "ArtifactWritePlan",
    "BudgetLedgerPlan",
    "CanonicalHarnessSeedReference",
    "CompositeCompilerMetadata",
    "CompositePlanStage",
    "CompositeRunPlan",
    "ConsumedFieldBinding",
    "ConsumedFieldLivenessManifest",
    "ContextReadPlan",
    "DependencyRef",
    "HarnessActor",
    "HarnessArtifactChannel",
    "HarnessPublicCarryoverRef",
    "HarnessPublicSessionContext",
    "HarnessPublicSessionLimits",
    "HarnessProtocol",
    "HarnessRevision",
    "HarnessSeedDocument",
    "HarnessTermination",
    "NO_PUBLIC_SESSION_CONTEXT_DIGEST",
    "PUBLIC_SESSION_CONTEXT_SCHEMA_VERSION",
    "PublicVerificationActionPlan",
    "PublicVerificationPlan",
    "RuntimeDependencyManifest",
    "TerminationPlan",
    "TrustedToolDependency",
    "harness_public_carryover_ref_digest",
    "public_session_context_digest",
    "source_field_digest",
]
