from __future__ import annotations

import re
from typing import Annotated, Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..core.identity import transaction_digest
from .harness import (
    HarnessActor,
    HarnessArtifactChannel,
    HarnessProtocol,
    HarnessRevision,
)


SEMANTIC_TRANSACTION_SCHEMA_VERSION = "repo-repair-semantic-transaction-v1"

SemanticOperator = Literal[
    "actor_split",
    "channel_add",
    "channel_rewire",
    "revision_insert",
    "revision_remove",
    "instruction_rewrite",
]
TreatmentClass = Literal["structural", "prompt_only_control"]
ProposalSource = Literal["manual", "matched_random", "evidence_guided"]
RuntimeOwner = Literal[
    "actor_context",
    "artifact_store",
    "budget_ledger",
    "scheduler",
    "revision_controller",
]
TraceObservation = Literal[
    "actor_calls",
    "artifact_deliveries",
    "pre_call_context",
    "revision_calls",
    "instruction_blocks",
    "budget_shares",
    "stage_topology",
]

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class SemanticActionModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        use_enum_values=True,
    )


def _require_identifier(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not _IDENTIFIER_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} is not a canonical transaction identifier")
    return normalized


def _require_digest(value: str, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _DIGEST_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return normalized


class SourcePathPrecondition(SemanticActionModel):
    source_path: str = Field(min_length=1)
    expected_value_digest: str

    @field_validator("expected_value_digest")
    @classmethod
    def validate_expected_value_digest(cls, value: str) -> str:
        return _require_digest(value, "expected_value_digest")


class TransactionApplicability(SemanticActionModel):
    required_actor_ids: tuple[str, ...] = ()
    absent_actor_ids: tuple[str, ...] = ()
    required_channel_ids: tuple[str, ...] = ()
    absent_channel_ids: tuple[str, ...] = ()
    revision_state: Literal["any", "absent", "present"] = "any"
    source_path_preconditions: tuple[SourcePathPrecondition, ...] = ()

    @field_validator(
        "required_actor_ids",
        "absent_actor_ids",
        "required_channel_ids",
        "absent_channel_ids",
    )
    @classmethod
    def normalize_identifiers(
        cls,
        values: tuple[str, ...],
        info: Any,
    ) -> tuple[str, ...]:
        normalized = tuple(
            sorted(_require_identifier(value, info.field_name) for value in values)
        )
        if len(normalized) != len(set(normalized)):
            raise ValueError(f"{info.field_name} may not contain duplicates")
        return normalized

    @model_validator(mode="after")
    def validate_disjoint_requirements(self) -> "TransactionApplicability":
        actor_conflicts = set(self.required_actor_ids) & set(self.absent_actor_ids)
        channel_conflicts = set(self.required_channel_ids) & set(
            self.absent_channel_ids
        )
        if actor_conflicts:
            raise ValueError(
                f"actors cannot be both required and absent: {sorted(actor_conflicts)!r}"
            )
        if channel_conflicts:
            raise ValueError(
                "channels cannot be both required and absent: "
                f"{sorted(channel_conflicts)!r}"
            )
        paths = [item.source_path for item in self.source_path_preconditions]
        if len(paths) != len(set(paths)):
            raise ValueError("source path preconditions must have unique paths")
        return self


class PredictedTraceEffect(SemanticActionModel):
    runtime_owner: RuntimeOwner
    trace_observation: TraceObservation
    expected_effect: str = Field(min_length=1)


class BudgetShareChange(SemanticActionModel):
    actor_id: str
    before_bps: int = Field(ge=0, le=10_000)
    after_bps: int = Field(ge=0, le=10_000)

    @field_validator("actor_id")
    @classmethod
    def validate_actor_id(cls, value: str) -> str:
        return _require_identifier(value, "actor_id")

    @model_validator(mode="after")
    def validate_actual_change(self) -> "BudgetShareChange":
        if self.before_bps == self.after_bps:
            raise ValueError("budget share change must change the actor allocation")
        return self


class BudgetEffect(SemanticActionModel):
    mode: Literal["unchanged", "shares_rebalanced"]
    total_before_bps: Literal[10_000] = 10_000
    total_after_bps: Literal[10_000] = 10_000
    share_changes: tuple[BudgetShareChange, ...] = ()

    @model_validator(mode="after")
    def validate_rebalance(self) -> "BudgetEffect":
        actor_ids = [change.actor_id for change in self.share_changes]
        if len(actor_ids) != len(set(actor_ids)):
            raise ValueError("budget share changes must have unique actor ids")
        if self.mode == "unchanged" and self.share_changes:
            raise ValueError("unchanged budget effect may not contain share changes")
        if self.mode == "shares_rebalanced":
            if not self.share_changes:
                raise ValueError("shares_rebalanced requires explicit share changes")
            delta = sum(
                change.after_bps - change.before_bps
                for change in self.share_changes
            )
            if delta != 0:
                raise ValueError("budget rebalance may not expand or shrink the total")
        return self


class ActorSplitPatch(SemanticActionModel):
    operator: Literal["actor_split"] = "actor_split"
    source_actor_before: HarnessActor
    source_actor_after: HarnessActor
    new_actor: HarnessActor
    new_channel: HarnessArtifactChannel

    @model_validator(mode="after")
    def validate_split_shape(self) -> "ActorSplitPatch":
        if self.source_actor_before.actor_id != self.source_actor_after.actor_id:
            raise ValueError("actor split may not rename the source actor")
        before = self.source_actor_before.model_dump(mode="python")
        after = self.source_actor_after.model_dump(mode="python")
        before.pop("budget_share_bps")
        after.pop("budget_share_bps")
        if before != after:
            raise ValueError(
                "actor split may change the source actor budget only; other changes "
                "belong to separate transactions"
            )
        if self.new_actor.actor_id == self.source_actor_before.actor_id:
            raise ValueError("new split actor must have a distinct actor_id")
        if self.new_channel.producer_actor_id != self.new_actor.actor_id:
            raise ValueError("split channel must be produced by the new actor")
        if (
            self.new_channel.consumer_actor_id
            != self.source_actor_before.actor_id
        ):
            raise ValueError("split channel must feed the source actor")
        if (
            self.source_actor_after.budget_share_bps
            + self.new_actor.budget_share_bps
            != self.source_actor_before.budget_share_bps
        ):
            raise ValueError(
                "split actor shares must exactly replace the source actor share"
            )
        return self


class ChannelAddPatch(SemanticActionModel):
    operator: Literal["channel_add"] = "channel_add"
    channel: HarnessArtifactChannel


class ChannelRewirePatch(SemanticActionModel):
    operator: Literal["channel_rewire"] = "channel_rewire"
    channel_before: HarnessArtifactChannel
    channel_after: HarnessArtifactChannel

    @model_validator(mode="after")
    def validate_rewire_shape(self) -> "ChannelRewirePatch":
        if self.channel_before.channel_id != self.channel_after.channel_id:
            raise ValueError("channel rewire may not rename the channel")
        if self.channel_before.payload_kind != self.channel_after.payload_kind:
            raise ValueError("channel rewire may not change payload_kind")
        before_route = (
            self.channel_before.producer_actor_id,
            self.channel_before.consumer_actor_id,
        )
        after_route = (
            self.channel_after.producer_actor_id,
            self.channel_after.consumer_actor_id,
        )
        if before_route == after_route:
            raise ValueError("channel rewire must change an endpoint")
        return self


class RevisionInsertPatch(SemanticActionModel):
    operator: Literal["revision_insert"] = "revision_insert"
    draft_channel: HarnessArtifactChannel
    feedback_channel_before: HarnessArtifactChannel
    revision: HarnessRevision

    @model_validator(mode="after")
    def validate_revision_shape(self) -> "RevisionInsertPatch":
        if self.feedback_channel_before.channel_id != self.revision.feedback_channel_id:
            raise ValueError("revision must name the existing feedback channel")
        if self.feedback_channel_before.consumer_actor_id != self.revision.actor_id:
            raise ValueError("existing feedback channel must feed the revising actor")
        reviewer_actor_id = self.feedback_channel_before.producer_actor_id
        if self.draft_channel.producer_actor_id != self.revision.actor_id:
            raise ValueError("draft channel must be produced by the revising actor")
        if self.draft_channel.consumer_actor_id != reviewer_actor_id:
            raise ValueError("draft channel must feed the feedback producer")
        if self.draft_channel.channel_id == self.feedback_channel_before.channel_id:
            raise ValueError("draft and feedback channels must be distinct")
        return self


class RevisionRemovePatch(SemanticActionModel):
    operator: Literal["revision_remove"] = "revision_remove"
    draft_channel: HarnessArtifactChannel
    feedback_channel: HarnessArtifactChannel
    revision: HarnessRevision

    @model_validator(mode="after")
    def validate_revision_shape(self) -> "RevisionRemovePatch":
        insert = RevisionInsertPatch(
            draft_channel=self.draft_channel,
            feedback_channel_before=self.feedback_channel,
            revision=self.revision,
        )
        if insert.operator != "revision_insert":
            raise AssertionError("revision removal shape validation failed")
        return self


class InstructionRewritePatch(SemanticActionModel):
    operator: Literal["instruction_rewrite"] = "instruction_rewrite"
    actor_id: str
    before_instruction: str = Field(min_length=1)
    after_instruction: str = Field(min_length=1)
    control_label: Literal["prompt_only"] = "prompt_only"

    @field_validator("actor_id")
    @classmethod
    def validate_actor_id(cls, value: str) -> str:
        return _require_identifier(value, "actor_id")

    @model_validator(mode="after")
    def validate_instruction_change(self) -> "InstructionRewritePatch":
        if self.before_instruction == self.after_instruction:
            raise ValueError("instruction rewrite may not be an exact or whitespace no-op")
        return self


SemanticPatch = Annotated[
    ActorSplitPatch
    | ChannelAddPatch
    | ChannelRewirePatch
    | RevisionInsertPatch
    | RevisionRemovePatch
    | InstructionRewritePatch,
    Field(discriminator="operator"),
]


class SemanticTransactionProposal(SemanticActionModel):
    schema_version: Literal[SEMANTIC_TRANSACTION_SCHEMA_VERSION] = (
        SEMANTIC_TRANSACTION_SCHEMA_VERSION
    )
    transaction_id: str
    operator: SemanticOperator
    treatment_class: TreatmentClass
    proposal_source: ProposalSource
    parent_source_protocol_digest: str
    parent_compiled_semantic_digest: str
    task_envelope_digest: str
    dependency_manifest_digest: str
    mechanism_hypothesis: str = Field(min_length=1)
    applicability: TransactionApplicability
    normalized_patch: SemanticPatch
    touched_source_paths: tuple[str, ...] = Field(min_length=1)
    budget_effect: BudgetEffect
    dependency_transaction_ids: tuple[str, ...] = ()
    predicted_trace_effect: PredictedTraceEffect

    @field_validator("transaction_id")
    @classmethod
    def validate_transaction_id(cls, value: str) -> str:
        return _require_identifier(value, "transaction_id")

    @field_validator(
        "parent_source_protocol_digest",
        "parent_compiled_semantic_digest",
        "task_envelope_digest",
        "dependency_manifest_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @field_validator("touched_source_paths")
    @classmethod
    def normalize_touched_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted(str(value).strip() for value in values))
        if any(not value for value in normalized):
            raise ValueError("touched_source_paths may not contain empty paths")
        if len(normalized) != len(set(normalized)):
            raise ValueError("touched_source_paths may not contain duplicates")
        return normalized

    @field_validator("dependency_transaction_ids")
    @classmethod
    def normalize_dependency_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(
            sorted(_require_identifier(value, "dependency_transaction_ids") for value in values)
        )
        if len(normalized) != len(set(normalized)):
            raise ValueError("dependency_transaction_ids may not contain duplicates")
        return normalized

    @model_validator(mode="after")
    def validate_operator_labels(self) -> "SemanticTransactionProposal":
        if self.operator != self.normalized_patch.operator:
            raise ValueError("operator must match normalized_patch.operator")
        if self.operator == "instruction_rewrite":
            if self.treatment_class != "prompt_only_control":
                raise ValueError(
                    "instruction rewrite must be labeled prompt_only_control"
                )
            if self.budget_effect.mode != "unchanged":
                raise ValueError("instruction rewrite may not change budget shares")
        elif self.treatment_class != "structural":
            raise ValueError("structural operators must be labeled structural")
        return self


class TransactionReversion(SemanticActionModel):
    expected_child_source_protocol_digest: str
    expected_child_compiled_semantic_digest: str
    task_envelope_digest: str
    dependency_manifest_digest: str
    restored_protocol: HarnessProtocol
    restored_source_protocol_digest: str
    restored_compiled_semantic_digest: str
    protected_source_paths: tuple[str, ...] = Field(min_length=1)

    @field_validator(
        "expected_child_source_protocol_digest",
        "expected_child_compiled_semantic_digest",
        "task_envelope_digest",
        "dependency_manifest_digest",
        "restored_source_protocol_digest",
        "restored_compiled_semantic_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @field_validator("protected_source_paths")
    @classmethod
    def normalize_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted(str(value).strip() for value in values))
        if any(not value for value in normalized):
            raise ValueError("protected_source_paths may not contain empty paths")
        if len(normalized) != len(set(normalized)):
            raise ValueError("protected_source_paths may not contain duplicates")
        return normalized


class SemanticTransaction(SemanticTransactionProposal):
    child_source_protocol_digest: str
    child_compiled_semantic_digest: str
    inverse: TransactionReversion
    transaction_record_digest: str = ""

    @field_validator(
        "child_source_protocol_digest",
        "child_compiled_semantic_digest",
    )
    @classmethod
    def validate_child_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @model_validator(mode="after")
    def bind_transaction(self) -> "SemanticTransaction":
        if (
            self.inverse.expected_child_source_protocol_digest
            != self.child_source_protocol_digest
            or self.inverse.expected_child_compiled_semantic_digest
            != self.child_compiled_semantic_digest
        ):
            raise ValueError("inverse is not bound to this transaction child")
        if (
            self.inverse.restored_source_protocol_digest
            != self.parent_source_protocol_digest
            or self.inverse.restored_compiled_semantic_digest
            != self.parent_compiled_semantic_digest
        ):
            raise ValueError("inverse does not restore this transaction parent")
        if self.inverse.task_envelope_digest != self.task_envelope_digest:
            raise ValueError("inverse is bound to another task")
        if self.inverse.dependency_manifest_digest != self.dependency_manifest_digest:
            raise ValueError("inverse is bound to other runtime dependencies")
        if self.inverse.protected_source_paths != self.touched_source_paths:
            raise ValueError("inverse protected paths must match transaction touched paths")
        computed = semantic_transaction_digest(self)
        if self.transaction_record_digest and self.transaction_record_digest != computed:
            raise ValueError("transaction_record_digest does not match the transaction")
        if not self.transaction_record_digest:
            object.__setattr__(self, "transaction_record_digest", computed)
        return self


def semantic_transaction_identity_payload(
    transaction: SemanticTransaction | Mapping[str, Any],
) -> dict[str, Any]:
    if isinstance(transaction, SemanticTransaction):
        payload = transaction.model_dump(mode="python", exclude_none=True)
    else:
        payload = dict(transaction)
    payload.pop("transaction_record_digest", None)
    return payload


def semantic_transaction_digest(
    transaction: SemanticTransaction | Mapping[str, Any],
) -> str:
    return transaction_digest(semantic_transaction_identity_payload(transaction))


__all__ = [
    "ActorSplitPatch",
    "BudgetEffect",
    "BudgetShareChange",
    "ChannelAddPatch",
    "ChannelRewirePatch",
    "InstructionRewritePatch",
    "PredictedTraceEffect",
    "RevisionInsertPatch",
    "RevisionRemovePatch",
    "SemanticOperator",
    "SemanticPatch",
    "SemanticTransaction",
    "SemanticTransactionProposal",
    "SourcePathPrecondition",
    "TransactionApplicability",
    "TransactionReversion",
    "semantic_transaction_digest",
]
