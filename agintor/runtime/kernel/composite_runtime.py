from __future__ import annotations

import json
import math
import re
import time
from collections import Counter
from typing import Any, Literal, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ...contracts.epochs import TaskEnvelope, TrustedToolId
from ...contracts.harness import ActorCallPlan, CompositeRunPlan, ContextReadPlan
from ...contracts.harness import HarnessPublicSessionContext, public_session_context_digest
from ...core.identity import canonical_identity_digest
from ...utils import count_tokens_rough
from .composite_artifacts import (
    ArtifactDeliveryEvidence,
    ArtifactEvidence,
    ArtifactStoreError,
    ImmutableArtifactStore,
)
from .composite_budget import (
    AggregateBudgetLedger,
    AggregateBudgetSnapshot,
    CostStatus,
    ProviderUsageReport,
)
from .composite_provider import (
    CompositeProviderController,
    ControlledProvider,
    CredentialReference,
    ProviderCallResult,
    ProviderCallStatus,
    ProviderRequestReservation,
)
from .repair_tools import PublicVerificationResult, RepairToolReceipt


_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class CompositeRuntimeError(RuntimeError):
    def __init__(
        self,
        kind: str,
        message: str,
        *,
        call_id: str | None = None,
        provider_result: ProviderCallResult | None = None,
    ) -> None:
        self.kind = kind
        self.call_id = call_id
        self.provider_result = provider_result
        super().__init__(message)


class ScratchWorkspaceBinding(BaseModel):
    """Actor-visible identity for an already materialized clean scratch copy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: str = Field(min_length=1)
    workspace_digest: str

    @field_validator("workspace_id")
    @classmethod
    def normalize_workspace_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("workspace_id may not be empty")
        return normalized

    @field_validator("workspace_digest")
    @classmethod
    def validate_workspace_digest(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _DIGEST_RE.fullmatch(normalized):
            raise ValueError("workspace_digest must be a lowercase SHA-256 digest")
        return normalized


class ActualContextRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    read_id: str
    source_kind: Literal["task", "workspace", "artifact", "prior_actor_output", "session"]
    source_ref: str
    value: Any
    value_digest: str
    provenance_ref: str


class PreCallContextManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: str
    actor_id: str
    task_envelope_digest: str
    reads: tuple[ActualContextRead, ...]
    assembled_before_provider: Literal[True] = True
    manifest_digest: str = ""

    @model_validator(mode="after")
    def bind_manifest(self) -> "PreCallContextManifest":
        payload = self.model_dump(mode="python", exclude={"manifest_digest"})
        digest = canonical_identity_digest(payload, domain="pre-call-context-manifest")
        if self.manifest_digest and self.manifest_digest != digest:
            raise ValueError("pre-call context manifest digest mismatch")
        if not self.manifest_digest:
            object.__setattr__(self, "manifest_digest", digest)
        return self


class ActorCallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    compiled_semantic_digest: str
    call_id: str
    actor_id: str
    call_kind: Literal["initial", "revision"]
    instruction: str
    revision_instruction: str | None = None
    allowed_tool_ids: tuple[TrustedToolId, ...]
    budget_share_bps: int = Field(gt=0, le=10_000)
    context: PreCallContextManifest
    turn_index: int = Field(default=0, ge=0)
    tool_results: tuple["ActorToolResultContext", ...] = ()
    input_token_estimate: int = Field(ge=0)
    max_output_tokens: int = Field(gt=0)
    request_digest: str = ""

    @model_validator(mode="after")
    def bind_request(self) -> "ActorCallRequest":
        payload = self.model_dump(mode="python", exclude={"request_digest"})
        digest = canonical_identity_digest(payload, domain="actor-call-request")
        if self.request_digest and self.request_digest != digest:
            raise ValueError("actor call request digest mismatch")
        if not self.request_digest:
            object.__setattr__(self, "request_digest", digest)
        return self


class ActorCallOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    output_text: str
    artifact_payloads: dict[str, str] = Field(default_factory=dict)
    final_patch: str | None = None


class ActorToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    turn_kind: Literal["tool_request"] = "tool_request"
    request_id: str = Field(min_length=1)
    tool_id: TrustedToolId
    arguments: dict[str, Any]


class ActorTerminalTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    turn_kind: Literal["terminal"] = "terminal"
    output: ActorCallOutput


class ActorToolResultContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str
    tool_id: TrustedToolId
    receipt_id: str
    status: str
    output: Any
    output_digest: str


class ProviderRoundEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    turn_index: int = Field(ge=0)
    request_digest: str
    reservation_id: str
    status: str
    started_at_ms: int = Field(ge=0)
    finished_at_ms: int = Field(ge=0)
    response_id: str | None = None
    response_digest: str
    usage: "ProviderUsageReport"
    response_kind: Literal["tool_request", "terminal"]
    tool_request_id: str | None = None

    @model_validator(mode="after")
    def validate_round(self) -> "ProviderRoundEvidence":
        if self.finished_at_ms < self.started_at_ms:
            raise ValueError("provider round finish precedes start")
        if (self.response_kind == "tool_request") != bool(self.tool_request_id):
            raise ValueError("tool-request provider rounds require exactly one request id")
        return self


class ActorCallEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: str
    actor_id: str
    call_kind: Literal["initial", "revision"]
    stage_id: str
    request_digest: str
    context_manifest_digest: str
    actual_read_ids: tuple[str, ...]
    provider_status: str
    provider_reservation_id: str
    output_text_digest: str
    artifact_ids_written: tuple[str, ...]
    final_patch_digest: str | None = None
    provider_rounds: tuple[ProviderRoundEvidence, ...]


class StageExecutionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stage_id: str
    stage_index: int = Field(ge=0)
    logical_fork: bool
    logical_join: bool
    call_execution_order: tuple[str, ...]


class CompositeRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    status: Literal["patch_ready", "completed", "public_verification_failed"]
    task_envelope_digest: str
    compiled_semantic_digest: str
    scratch_workspace: ScratchWorkspaceBinding
    final_patch: str
    final_patch_digest: str
    final_workspace_digest: str
    source_snapshot_unchanged: bool
    public_verification_status: Literal["not_run", "passed", "failed"]
    public_verification: PublicVerificationResult | None = None
    tool_receipts: tuple[RepairToolReceipt, ...] = ()
    actor_calls: tuple[ActorCallEvidence, ...]
    stages: tuple[StageExecutionEvidence, ...]
    context_manifests: tuple[PreCallContextManifest, ...]
    artifacts: tuple[ArtifactEvidence, ...]
    artifact_deliveries: tuple[ArtifactDeliveryEvidence, ...]
    budget: AggregateBudgetSnapshot


class CompositeToolInterface(Protocol):
    def invoke(
        self,
        *,
        call_id: str,
        tool_id: TrustedToolId,
        arguments: Mapping[str, Any],
        ledger: AggregateBudgetLedger,
        phase: Literal["actor_tool", "terminal_public_verification"],
        tool_request_id: str | None,
        verification_step_id: str | None,
    ) -> Any:
        ...


class CompositeRuntime:
    """Deterministic sequential executor for one validated CompositeRunPlan."""

    def __init__(
        self,
        plan: CompositeRunPlan,
        task: TaskEnvelope,
        scratch_workspace: ScratchWorkspaceBinding,
        provider: ControlledProvider,
        *,
        run_id: str,
        credential_reference: CredentialReference | None = None,
        tool_interface: CompositeToolInterface | None = None,
        public_session_context: HarnessPublicSessionContext | Mapping[str, Any] | None = None,
    ) -> None:
        try:
            self.plan = CompositeRunPlan.model_validate(plan.model_dump(mode="python"))
            self.task = TaskEnvelope.model_validate(task.model_dump(mode="python"))
            self.scratch_workspace = ScratchWorkspaceBinding.model_validate(
                scratch_workspace.model_dump(mode="python")
            )
            self.public_session_context = (
                HarnessPublicSessionContext.model_validate(
                    public_session_context.model_dump(mode="python")
                    if isinstance(public_session_context, HarnessPublicSessionContext)
                    else public_session_context
                )
                if public_session_context is not None
                else None
            )
        except Exception as exc:
            raise CompositeRuntimeError(
                "invalid_runtime_contract",
                "runtime inputs failed boundary revalidation",
            ) from exc
        self.provider = provider
        self.run_id = str(run_id).strip()
        self.credential_reference = credential_reference
        self.tool_interface = tool_interface
        if not self.run_id:
            raise ValueError("run_id may not be empty")
        self._calls = {call.call_id: call for call in self.plan.actor_calls}
        self._call_stage: dict[str, str] = {}
        self._validate_inputs()
        self.ledger = AggregateBudgetLedger(self.plan.budget_ledger.aggregate_ceiling)
        self.provider_controller = CompositeProviderController(self.ledger)
        try:
            self.artifacts = ImmutableArtifactStore(
                self.plan,
                max_total_bytes=self.task.ceilings.max_artifact_bytes,
            )
        except ArtifactStoreError as exc:
            raise CompositeRuntimeError("invalid_artifact_plan", str(exc)) from exc
        self._outputs: dict[str, ActorCallOutput] = {}
        self._contexts: list[PreCallContextManifest] = []
        self._call_evidence: list[ActorCallEvidence] = []
        self._stage_evidence: list[StageExecutionEvidence] = []
        self._started = False

    def _validate_inputs(self) -> None:
        if self.plan.task_envelope_digest != self.task.task_manifest_digest:
            raise CompositeRuntimeError(
                "task_identity_mismatch",
                "CompositeRunPlan is bound to another TaskEnvelope",
            )
        if self.plan.budget_ledger.aggregate_ceiling != self.task.ceilings:
            raise CompositeRuntimeError(
                "ceiling_mismatch",
                "compiled aggregate ceiling does not match the public task",
            )
        if self.scratch_workspace.workspace_digest != self.task.workspace_snapshot.digest:
            raise CompositeRuntimeError(
                "scratch_identity_mismatch",
                "scratch workspace digest does not match the immutable public snapshot digest",
            )
        if self.plan.budget_ledger.scheduled_model_calls != len(self.plan.actor_calls):
            raise CompositeRuntimeError(
                "scheduled_call_mismatch",
                "budget ledger scheduled_model_calls does not match actor calls",
            )

        stages = tuple(self.plan.stages)
        stage_ids = [stage.stage_id for stage in stages]
        if len(stage_ids) != len(set(stage_ids)):
            raise CompositeRuntimeError(
                "duplicate_stage_id",
                "CompositeRunPlan stage ids must be unique",
            )
        completed_stage_ids: set[str] = set()
        for expected_index, stage in enumerate(stages):
            if stage.stage_index != expected_index:
                raise CompositeRuntimeError(
                    "stage_order_invalid",
                    "CompositeRunPlan stages must have contiguous deterministic indices",
                )
            if not set(stage.depends_on_stage_ids) <= completed_stage_ids:
                raise CompositeRuntimeError(
                    "stage_dependency_invalid",
                    f"stage {stage.stage_id!r} depends on an unexecuted stage",
                )
            for call_id in stage.call_ids:
                if call_id not in self._calls:
                    raise CompositeRuntimeError(
                        "unknown_staged_call",
                        f"stage {stage.stage_id!r} references unknown call {call_id!r}",
                    )
                if call_id in self._call_stage:
                    raise CompositeRuntimeError(
                        "call_scheduled_twice",
                        f"call {call_id!r} is scheduled more than once",
                    )
                self._call_stage[call_id] = stage.stage_id
            completed_stage_ids.add(stage.stage_id)

        final_calls = [call.call_id for call in self.plan.actor_calls if call.emits_final_patch]
        if final_calls != [self.plan.termination.final_actor_call_id]:
            raise CompositeRuntimeError(
                "termination_invalid",
                "exactly the termination call must emit the final patch",
            )
        if self.plan.termination.max_patch_bytes != self.task.ceilings.max_patch_bytes:
            raise CompositeRuntimeError(
                "patch_ceiling_mismatch",
                "termination patch ceiling does not match the public task",
            )
        expected_verification = tuple(
            {
                "step_id": step.step_id,
                "argv": step.argv,
                "cwd": step.cwd,
                "timeout_ms": step.timeout_ms,
                "expected_exit_codes": step.expected_exit_codes,
            }
            for step in self.task.public_reproduction
        )
        actual_verification = tuple(
            {
                "step_id": action.step_id,
                "argv": action.argv,
                "cwd": action.cwd,
                "timeout_ms": action.timeout_ms,
                "expected_exit_codes": action.expected_exit_codes,
            }
            for action in self.plan.public_verification.actions
        )
        if actual_verification != expected_verification:
            raise CompositeRuntimeError(
                "public_verification_mismatch",
                "compiled public verification differs from the TaskEnvelope",
            )
        allowed_tools = set(self.task.allowed_capabilities)
        for call in self.plan.actor_calls:
            if not set(call.tool_ids) <= allowed_tools:
                raise CompositeRuntimeError(
                    "task_denied_tool",
                    f"call {call.call_id!r} references a task-denied tool",
                )

        delivery_keys = {
            (delivery.artifact_id, delivery.consumer_call_id)
            for delivery in self.plan.artifact_deliveries
        }
        read_keys: set[tuple[str, str]] = set()
        call_positions = {
            call_id: stage.stage_index
            for stage in stages
            for call_id in stage.call_ids
        }
        for call in self.plan.actor_calls:
            read_ids = [read.read_id for read in call.context_reads]
            if len(read_ids) != len(set(read_ids)):
                raise CompositeRuntimeError(
                    "duplicate_context_read",
                    f"call {call.call_id!r} has duplicate read ids",
                )
            for read in call.context_reads:
                if read.source_kind == "task" and read.source_ref not in {
                    "issue",
                    "public_reproduction",
                }:
                    raise CompositeRuntimeError(
                        "undeclared_task_read",
                        f"unsupported public task read {read.source_ref!r}",
                    )
                if read.source_kind == "workspace" and read.source_ref != "scratch_workspace":
                    raise CompositeRuntimeError(
                        "undeclared_workspace_read",
                        f"unsupported workspace read {read.source_ref!r}",
                    )
                if read.source_kind == "session" and read.source_ref != "public_carryover":
                    raise CompositeRuntimeError(
                        "undeclared_session_read",
                        f"unsupported public session read {read.source_ref!r}",
                    )
                if read.source_kind == "artifact":
                    key = (read.source_ref, call.call_id)
                    if key in delivery_keys:
                        read_keys.add(key)
                    elif not self._retained_artifact_source(
                        artifact_id=read.source_ref,
                        consumer_call=call,
                        call_positions=call_positions,
                    ):
                        raise CompositeRuntimeError(
                            "undelivered_artifact_read",
                            f"artifact {read.source_ref!r} is not delivered to {call.call_id!r}",
                        )
                if read.source_kind == "prior_actor_output":
                    source_call = self._calls.get(read.source_ref)
                    if source_call is None:
                        raise CompositeRuntimeError(
                            "unknown_prior_output",
                            f"prior output call {read.source_ref!r} does not exist",
                        )
                    if call_positions[source_call.call_id] >= call_positions[call.call_id]:
                        raise CompositeRuntimeError(
                            "future_prior_output",
                            "prior actor output must come from an earlier stage",
                        )
        if read_keys != delivery_keys:
            raise CompositeRuntimeError(
                "inert_artifact_delivery",
                "every artifact delivery must have exactly one declared consumer read",
            )
        declares_session = any(
            read.source_kind == "session"
            for call in self.plan.actor_calls
            for read in call.context_reads
        )
        if self.public_session_context is not None and not declares_session:
            raise CompositeRuntimeError(
                "undeclared_session_context",
                "public session context was supplied but no actor declared a session read",
            )

    def _retained_artifact_source(
        self,
        *,
        artifact_id: str,
        consumer_call: ActorCallPlan,
        call_positions: Mapping[str, int],
    ) -> str | None:
        if consumer_call.call_kind != "revision":
            return None
        for delivery in self.plan.artifact_deliveries:
            prior_call = self._calls.get(delivery.consumer_call_id)
            if (
                delivery.artifact_id == artifact_id
                and prior_call is not None
                and prior_call.actor_id == consumer_call.actor_id
                and call_positions[prior_call.call_id] < call_positions[consumer_call.call_id]
            ):
                return prior_call.call_id
        return None

    @staticmethod
    def _value_digest(value: Any) -> str:
        return canonical_identity_digest(value, domain="runtime-context-value")

    def _resolve_read(
        self,
        call: ActorCallPlan,
        read: ContextReadPlan,
    ) -> ActualContextRead:
        if read.source_kind == "task":
            if read.source_ref == "issue":
                value: Any = self.task.issue
            elif read.source_ref == "public_reproduction":
                value = [
                    step.model_dump(mode="json")
                    for step in self.task.public_reproduction
                ]
            else:
                raise CompositeRuntimeError(
                    "undeclared_task_read",
                    f"unsupported public task read {read.source_ref!r}",
                    call_id=call.call_id,
                )
            return ActualContextRead(
                read_id=read.read_id,
                source_kind=read.source_kind,
                source_ref=read.source_ref,
                value=value,
                value_digest=self._value_digest(value),
                provenance_ref=self.task.task_manifest_digest,
            )
        if read.source_kind == "workspace":
            value = {
                "workspace_id": self.scratch_workspace.workspace_id,
                "workspace_digest": self.scratch_workspace.workspace_digest,
            }
            return ActualContextRead(
                read_id=read.read_id,
                source_kind=read.source_kind,
                source_ref=read.source_ref,
                value=value,
                value_digest=self._value_digest(value),
                provenance_ref=self.scratch_workspace.workspace_id,
            )
        if read.source_kind == "artifact":
            direct = next(
                (
                    delivery
                    for delivery in self.plan.artifact_deliveries
                    if delivery.artifact_id == read.source_ref
                    and delivery.consumer_call_id == call.call_id
                ),
                None,
            )
            if direct is not None:
                try:
                    delivery = self.artifacts.deliver(
                        artifact_id=read.source_ref,
                        consumer_call_id=call.call_id,
                    )
                except ArtifactStoreError as exc:
                    raise CompositeRuntimeError(
                        "artifact_delivery_failed",
                        str(exc),
                        call_id=call.call_id,
                    ) from exc
                value = delivery.payload
                value_digest = delivery.payload_digest
                provenance_ref = delivery.producer_call_id
            else:
                positions = {
                    call_id: stage.stage_index
                    for stage in self.plan.stages
                    for call_id in stage.call_ids
                }
                prior_consumer = self._retained_artifact_source(
                    artifact_id=read.source_ref,
                    consumer_call=call,
                    call_positions=positions,
                )
                if prior_consumer is None or not self.artifacts.was_delivered(
                    artifact_id=read.source_ref,
                    consumer_call_id=prior_consumer,
                ):
                    raise CompositeRuntimeError(
                        "artifact_delivery_failed",
                        f"retained artifact {read.source_ref!r} was not delivered to this actor",
                        call_id=call.call_id,
                    )
                try:
                    artifact = self.artifacts.read_retained(
                        artifact_id=read.source_ref,
                        consumer_call_id=call.call_id,
                    )
                except ArtifactStoreError as exc:
                    raise CompositeRuntimeError(
                        "artifact_delivery_failed",
                        str(exc),
                        call_id=call.call_id,
                    ) from exc
                value = artifact.payload
                value_digest = artifact.payload_digest
                provenance_ref = artifact.producer_call_id
            return ActualContextRead(
                read_id=read.read_id,
                source_kind=read.source_kind,
                source_ref=read.source_ref,
                value=value,
                value_digest=value_digest,
                provenance_ref=provenance_ref,
            )
        if read.source_kind == "prior_actor_output":
            prior = self._outputs.get(read.source_ref)
            if prior is None:
                raise CompositeRuntimeError(
                    "prior_output_missing",
                    f"required prior output {read.source_ref!r} is unavailable",
                    call_id=call.call_id,
                )
            return ActualContextRead(
                read_id=read.read_id,
                source_kind=read.source_kind,
                source_ref=read.source_ref,
                value=prior.output_text,
                value_digest=self._value_digest(prior.output_text),
                provenance_ref=read.source_ref,
            )
        if read.source_kind == "session":
            if read.source_ref != "public_carryover":
                raise CompositeRuntimeError(
                    "undeclared_session_read",
                    f"unsupported public session read {read.source_ref!r}",
                    call_id=call.call_id,
                )
            if self.public_session_context is None:
                value = {
                    "session_id": None,
                    "parent_message_id": None,
                    "next_sequence": None,
                    "carryover": [],
                    "carryover_count": 0,
                    "context_digest": public_session_context_digest(None),
                }
                provenance_ref = public_session_context_digest(None)
            else:
                value = self.public_session_context.actor_visible_value()
                provenance_ref = self.public_session_context.context_digest
            return ActualContextRead(
                read_id=read.read_id,
                source_kind=read.source_kind,
                source_ref=read.source_ref,
                value=value,
                value_digest=self._value_digest(value),
                provenance_ref=provenance_ref,
            )
        raise CompositeRuntimeError(
            "unsupported_context_read",
            f"unsupported context source kind {read.source_kind!r}",
            call_id=call.call_id,
        )

    def _context_for(self, call: ActorCallPlan) -> PreCallContextManifest:
        reads = tuple(self._resolve_read(call, read) for read in call.context_reads)
        manifest = PreCallContextManifest(
            call_id=call.call_id,
            actor_id=call.actor_id,
            task_envelope_digest=self.task.task_manifest_digest,
            reads=reads,
        )
        self._contexts.append(manifest)
        return manifest

    def _call_share_bps(self, call: ActorCallPlan) -> float:
        count = sum(1 for item in self.plan.actor_calls if item.actor_id == call.actor_id)
        return call.budget_share_bps / count

    def _request_for(
        self,
        call: ActorCallPlan,
        context: PreCallContextManifest,
        *,
        turn_index: int,
        tool_results: tuple[ActorToolResultContext, ...],
    ) -> ActorCallRequest:
        share_bps = self._call_share_bps(call)
        output_limit = max(
            1,
            math.floor(self.task.ceilings.max_output_tokens * share_bps / 10_000),
        )
        input_limit = max(
            1,
            math.floor(self.task.ceilings.max_input_tokens * share_bps / 10_000),
        )
        estimate = 0
        request: ActorCallRequest | None = None
        for _ in range(8):
            request = ActorCallRequest(
                run_id=self.run_id,
                compiled_semantic_digest=self.plan.compiled_semantic_digest,
                call_id=call.call_id,
                actor_id=call.actor_id,
                call_kind=call.call_kind,
                instruction=call.instruction,
                revision_instruction=call.revision_instruction,
                allowed_tool_ids=call.tool_ids,
                budget_share_bps=call.budget_share_bps,
                context=context,
                turn_index=turn_index,
                tool_results=tool_results,
                input_token_estimate=estimate,
                max_output_tokens=output_limit,
            )
            serialized = json.dumps(
                request.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            )
            next_estimate = count_tokens_rough(serialized)
            if next_estimate == estimate:
                break
            estimate = next_estimate
        if request is None or request.input_token_estimate != estimate:
            request = ActorCallRequest(
                **{
                    **request.model_dump(mode="python", exclude={"request_digest"}),
                    "input_token_estimate": estimate,
                }
            )
        if request.input_token_estimate > input_limit:
            raise CompositeRuntimeError(
                "actor_input_share_exhausted",
                f"call {call.call_id!r} context exceeds its actor input-token share",
                call_id=call.call_id,
            )
        return request

    @staticmethod
    def _require_unified_diff(patch: str) -> None:
        lines = patch.splitlines()
        if not patch.strip() or not any(line.startswith("--- ") for line in lines):
            raise CompositeRuntimeError(
                "invalid_final_patch",
                "final output is not a unified diff with an original-file header",
            )
        if not any(line.startswith("+++ ") for line in lines):
            raise CompositeRuntimeError(
                "invalid_final_patch",
                "final output is not a unified diff with an updated-file header",
            )

    def _validate_output(
        self,
        call: ActorCallPlan,
        response: Any,
    ) -> ActorCallOutput:
        try:
            output = ActorCallOutput.model_validate(response)
        except Exception as exc:
            raise CompositeRuntimeError(
                "invalid_actor_output",
                f"call {call.call_id!r} returned an invalid typed output",
                call_id=call.call_id,
            ) from exc
        expected_artifacts = {write.artifact_id for write in call.artifact_writes}
        actual_artifacts = set(output.artifact_payloads)
        if actual_artifacts != expected_artifacts:
            raise CompositeRuntimeError(
                "artifact_write_mismatch",
                f"call {call.call_id!r} did not return its exact declared artifact set",
                call_id=call.call_id,
            )
        output_size = len(output.output_text.encode("utf-8"))
        if output_size > self.task.ceilings.max_artifact_bytes:
            raise CompositeRuntimeError(
                "actor_output_too_large",
                f"call {call.call_id!r} output text exceeds the run artifact ceiling",
                call_id=call.call_id,
            )
        if call.emits_final_patch:
            if output.final_patch is None:
                raise CompositeRuntimeError(
                    "final_patch_missing",
                    f"termination call {call.call_id!r} did not emit a patch",
                    call_id=call.call_id,
                )
            self._require_unified_diff(output.final_patch)
            patch_size = len(output.final_patch.encode("utf-8"))
            if patch_size > self.plan.termination.max_patch_bytes:
                raise CompositeRuntimeError(
                    "final_patch_too_large",
                    "final patch exceeds the compiled patch-byte ceiling",
                    call_id=call.call_id,
                )
        elif output.final_patch is not None:
            raise CompositeRuntimeError(
                "undeclared_final_patch",
                f"non-terminal call {call.call_id!r} attempted to emit a final patch",
                call_id=call.call_id,
            )
        return output

    @staticmethod
    def _parse_actor_turn(response: Any) -> ActorToolRequest | ActorTerminalTurn:
        if isinstance(response, ActorToolRequest):
            return response
        if isinstance(response, ActorTerminalTurn):
            return response
        if isinstance(response, ActorCallOutput):
            return ActorTerminalTurn(output=response)
        if isinstance(response, Mapping):
            payload = dict(response)
            if payload.get("turn_kind") == "tool_request":
                return ActorToolRequest.model_validate(payload)
            if payload.get("turn_kind") == "terminal":
                return ActorTerminalTurn.model_validate(payload)
            return ActorTerminalTurn(output=ActorCallOutput.model_validate(payload))
        raise CompositeRuntimeError(
            "invalid_actor_turn",
            "provider response is neither a typed tool request nor terminal actor output",
        )

    @staticmethod
    def _tool_result_context(
        request: ActorToolRequest,
        result: Any,
    ) -> ActorToolResultContext:
        receipt = getattr(result, "receipt", None)
        if receipt is None:
            raise CompositeRuntimeError(
                "invalid_tool_result",
                "injected repair tool interface returned no typed receipt",
            )
        return ActorToolResultContext(
            request_id=request.request_id,
            tool_id=request.tool_id,
            receipt_id=str(receipt.receipt_id),
            status=str(getattr(receipt.status, "value", receipt.status)),
            output=receipt.output,
            output_digest=receipt.output_digest,
        )

    def invoke_tool(
        self,
        *,
        call_id: str,
        tool_id: TrustedToolId,
        arguments: Mapping[str, Any],
        tool_request_id: str,
    ) -> Any:
        call = self._calls.get(call_id)
        if call is None:
            raise CompositeRuntimeError("unknown_tool_call_owner", f"unknown actor call {call_id!r}")
        if tool_id not in call.tool_ids:
            raise CompositeRuntimeError(
                "tool_not_authorized",
                f"tool {tool_id!r} is not authorized for call {call_id!r}",
                call_id=call_id,
            )
        if self.tool_interface is None:
            raise CompositeRuntimeError(
                "tool_interface_unavailable",
                "trusted tool execution is not installed in the R1a runtime slice",
                call_id=call_id,
            )
        return self.tool_interface.invoke(
            call_id=call_id,
            tool_id=tool_id,
            arguments=dict(arguments),
            ledger=self.ledger,
            phase="actor_tool",
            tool_request_id=tool_request_id,
            verification_step_id=None,
        )

    def _provider_request_reservation(
        self,
        request: ActorCallRequest,
    ) -> ProviderRequestReservation:
        reservation = getattr(self.provider, "provider_request_reservation", None)
        if callable(reservation):
            try:
                return ProviderRequestReservation.model_validate(reservation(request))
            except Exception as exc:
                raise CompositeRuntimeError(
                    "provider_reservation_failed",
                    "provider could not produce a bounded pre-send reservation",
                    call_id=request.call_id,
                ) from exc
        return ProviderRequestReservation(
            input_tokens=request.input_token_estimate,
            max_output_tokens=request.max_output_tokens,
        )

    def _execute_call(self, call: ActorCallPlan, stage_id: str) -> None:
        context = self._context_for(call)
        share_bps = self._call_share_bps(call)
        cost_reservation = self.task.ceilings.max_known_cost_usd * share_bps / 10_000
        tool_results: tuple[ActorToolResultContext, ...] = ()
        seen_tool_request_ids: set[str] = set()
        round_evidence: list[ProviderRoundEvidence] = []
        output: ActorCallOutput | None = None
        provider_result: ProviderCallResult | None = None
        terminal_request: ActorCallRequest | None = None
        for turn_index in range(self.task.ceilings.max_model_calls):
            request = self._request_for(
                call,
                context,
                turn_index=turn_index,
                tool_results=tool_results,
            )
            reservation = self._provider_request_reservation(request)
            request_cost_reservation = (
                reservation.max_known_cost_usd
                if reservation.max_known_cost_usd is not None
                else cost_reservation
            )
            started_at_ms = time.time_ns() // 1_000_000
            provider_result = self.provider_controller.call(
                self.provider,
                request,
                input_tokens=reservation.input_tokens,
                max_output_tokens=reservation.max_output_tokens,
                max_cached_tokens=reservation.max_cached_tokens,
                max_cache_write_tokens=reservation.max_cache_write_tokens,
                estimated_cost_usd=request_cost_reservation,
                expected_cost_status=CostStatus.KNOWN,
                is_retry=False,
                credential_reference=self.credential_reference,
            )
            finished_at_ms = time.time_ns() // 1_000_000
            if provider_result.status is not ProviderCallStatus.SUCCEEDED:
                raise CompositeRuntimeError(
                    "provider_call_failed",
                    f"provider round {turn_index} for {call.call_id!r} did not succeed",
                    call_id=call.call_id,
                    provider_result=provider_result,
                )
            if provider_result.invocation is None or provider_result.reservation_id is None:
                raise CompositeRuntimeError(
                    "provider_result_missing",
                    f"provider round {turn_index} for {call.call_id!r} returned no invocation evidence",
                    call_id=call.call_id,
                )
            try:
                turn = self._parse_actor_turn(provider_result.invocation.response)
            except Exception as exc:
                if isinstance(exc, CompositeRuntimeError):
                    raise
                raise CompositeRuntimeError(
                    "invalid_actor_turn",
                    f"provider round {turn_index} returned an invalid actor turn",
                    call_id=call.call_id,
                ) from exc
            if isinstance(turn, ActorToolRequest):
                if turn.request_id in seen_tool_request_ids:
                    raise CompositeRuntimeError(
                        "duplicate_tool_request_id",
                        f"call {call.call_id!r} repeated tool request id {turn.request_id!r}",
                        call_id=call.call_id,
                    )
                seen_tool_request_ids.add(turn.request_id)
                round_evidence.append(
                    ProviderRoundEvidence(
                        turn_index=turn_index,
                        request_digest=request.request_digest,
                        reservation_id=provider_result.reservation_id,
                        status=provider_result.status.value,
                        started_at_ms=started_at_ms,
                        finished_at_ms=finished_at_ms,
                        response_id=provider_result.invocation.usage.response_id,
                        response_digest=canonical_identity_digest(
                            turn.model_dump(mode="json"),
                            domain="provider-round-response",
                        ),
                        usage=provider_result.invocation.usage,
                        response_kind="tool_request",
                        tool_request_id=turn.request_id,
                    )
                )
                tool_result = self.invoke_tool(
                    call_id=call.call_id,
                    tool_id=turn.tool_id,
                    arguments=turn.arguments,
                    tool_request_id=turn.request_id,
                )
                tool_results = (*tool_results, self._tool_result_context(turn, tool_result))
                continue
            round_evidence.append(
                ProviderRoundEvidence(
                    turn_index=turn_index,
                    request_digest=request.request_digest,
                    reservation_id=provider_result.reservation_id,
                    status=provider_result.status.value,
                    started_at_ms=started_at_ms,
                    finished_at_ms=finished_at_ms,
                    response_id=provider_result.invocation.usage.response_id,
                    response_digest=canonical_identity_digest(
                        turn.model_dump(mode="json"),
                        domain="provider-round-response",
                    ),
                    usage=provider_result.invocation.usage,
                    response_kind="terminal",
                )
            )
            output = self._validate_output(call, turn.output)
            terminal_request = request
            break
        if output is None or provider_result is None or terminal_request is None:
            raise CompositeRuntimeError(
                "actor_turn_budget_exhausted",
                f"call {call.call_id!r} did not produce terminal output within the model-call ceiling",
                call_id=call.call_id,
                provider_result=provider_result,
            )
        written: list[str] = []
        for write in call.artifact_writes:
            try:
                artifact = self.artifacts.write(
                    write,
                    producer_call_id=call.call_id,
                    payload=output.artifact_payloads[write.artifact_id],
                )
            except ArtifactStoreError as exc:
                raise CompositeRuntimeError(
                    "artifact_write_failed",
                    str(exc),
                    call_id=call.call_id,
                ) from exc
            written.append(artifact.artifact_id)
        self._outputs[call.call_id] = output
        patch_digest = (
            canonical_identity_digest(output.final_patch, domain="final-unified-diff")
            if output.final_patch is not None
            else None
        )
        self._call_evidence.append(
            ActorCallEvidence(
                call_id=call.call_id,
                actor_id=call.actor_id,
                call_kind=call.call_kind,
                stage_id=stage_id,
                request_digest=terminal_request.request_digest,
                context_manifest_digest=context.manifest_digest,
                actual_read_ids=tuple(read.read_id for read in context.reads),
                provider_status=provider_result.status.value,
                provider_reservation_id=provider_result.reservation_id,
                output_text_digest=self._value_digest(output.output_text),
                artifact_ids_written=tuple(written),
                final_patch_digest=patch_digest,
                provider_rounds=tuple(round_evidence),
            )
        )

    def run(self) -> CompositeRunResult:
        if self._started:
            raise CompositeRuntimeError(
                "run_already_started",
                "CompositeRuntime instances execute exactly one run",
            )
        self._started = True
        completed_stage_ids: set[str] = set()
        executed_calls: list[str] = []
        for stage in self.plan.stages:
            if not set(stage.depends_on_stage_ids) <= completed_stage_ids:
                raise CompositeRuntimeError(
                    "stage_dependency_unmet",
                    f"stage {stage.stage_id!r} dependency is not complete",
                )
            stage_order: list[str] = []
            for call_id in stage.call_ids:
                call = self._calls[call_id]
                self._execute_call(call, stage.stage_id)
                stage_order.append(call_id)
                executed_calls.append(call_id)
            self._stage_evidence.append(
                StageExecutionEvidence(
                    stage_id=stage.stage_id,
                    stage_index=stage.stage_index,
                    logical_fork=stage.fork,
                    logical_join=stage.join,
                    call_execution_order=tuple(stage_order),
                )
            )
            completed_stage_ids.add(stage.stage_id)

        if executed_calls != [call_id for stage in self.plan.stages for call_id in stage.call_ids]:
            raise CompositeRuntimeError(
                "execution_order_mismatch",
                "actual actor-call order differs from the compiled sequential order",
            )
        actual_delivery_keys = {
            (delivery.artifact_id, delivery.consumer_call_id)
            for delivery in self.artifacts.deliveries()
        }
        expected_delivery_keys = {
            (delivery.artifact_id, delivery.consumer_call_id)
            for delivery in self.plan.artifact_deliveries
        }
        if actual_delivery_keys != expected_delivery_keys:
            raise CompositeRuntimeError(
                "delivery_reconciliation_failed",
                "actual artifact deliveries do not match the compiled run plan",
            )
        final_output = self._outputs.get(self.plan.termination.final_actor_call_id)
        if final_output is None or final_output.final_patch is None:
            raise CompositeRuntimeError(
                "final_patch_missing",
                "termination call did not produce a final patch",
            )
        budget = self.ledger.snapshot()
        status: Literal["patch_ready", "completed", "public_verification_failed"] = "patch_ready"
        verification_status: Literal["not_run", "passed", "failed"] = "not_run"
        public_verification: PublicVerificationResult | None = None
        tool_receipts: tuple[RepairToolReceipt, ...] = ()
        final_workspace_digest = self.scratch_workspace.workspace_digest
        source_snapshot_unchanged = True
        if self.tool_interface is not None:
            required_methods = (
                "workspace_diff",
                "current_workspace_digest",
                "source_snapshot_unchanged",
                "immutable_base_unchanged",
                "run_public_verification",
                "receipts",
            )
            if not all(callable(getattr(self.tool_interface, name, None)) for name in required_methods):
                raise CompositeRuntimeError(
                    "repair_control_incomplete",
                    "injected tool interface lacks the R2 final-control surface",
                )
            actual_patch = self.tool_interface.workspace_diff(
                max_patch_bytes=self.plan.termination.max_patch_bytes
            )
            if actual_patch != final_output.final_patch:
                raise CompositeRuntimeError(
                    "final_patch_workspace_mismatch",
                    "submitted final patch does not exactly match the immutable-base workspace diff",
                    call_id=self.plan.termination.final_actor_call_id,
                )
            source_snapshot_unchanged = bool(
                self.tool_interface.source_snapshot_unchanged()
            )
            if not source_snapshot_unchanged or not self.tool_interface.immutable_base_unchanged():
                raise CompositeRuntimeError(
                    "immutable_source_changed",
                    "source snapshot or immutable base changed during runtime execution",
                )
            public_verification = self.tool_interface.run_public_verification(
                call_id=self.plan.termination.final_actor_call_id,
                ledger=self.ledger,
            )
            final_workspace_digest = self.tool_interface.current_workspace_digest()
            source_snapshot_unchanged = bool(
                self.tool_interface.source_snapshot_unchanged()
            )
            if not source_snapshot_unchanged or not self.tool_interface.immutable_base_unchanged():
                raise CompositeRuntimeError(
                    "immutable_source_changed",
                    "public verification changed the source snapshot or immutable base",
                )
            tool_receipts = tuple(self.tool_interface.receipts())
            if public_verification.passed:
                status = "completed"
                verification_status = "passed"
            else:
                status = "public_verification_failed"
                verification_status = "failed"
            budget = self.ledger.snapshot()
        if not budget.reconciled or not budget.healthy:
            raise CompositeRuntimeError(
                "budget_reconciliation_failed",
                "aggregate provider accounting is incomplete or unhealthy",
            )
        final_patch_digest = canonical_identity_digest(
            final_output.final_patch,
            domain="final-unified-diff",
        )
        return CompositeRunResult(
            run_id=self.run_id,
            status=status,
            task_envelope_digest=self.task.task_manifest_digest,
            compiled_semantic_digest=self.plan.compiled_semantic_digest,
            scratch_workspace=self.scratch_workspace,
            final_patch=final_output.final_patch,
            final_patch_digest=final_patch_digest,
            final_workspace_digest=final_workspace_digest,
            source_snapshot_unchanged=source_snapshot_unchanged,
            public_verification_status=verification_status,
            public_verification=public_verification,
            tool_receipts=tool_receipts,
            actor_calls=tuple(self._call_evidence),
            stages=tuple(self._stage_evidence),
            context_manifests=tuple(self._contexts),
            artifacts=self.artifacts.evidence(),
            artifact_deliveries=self.artifacts.deliveries(),
            budget=budget,
        )


__all__ = [
    "ActorCallEvidence",
    "ActorCallOutput",
    "ActorCallRequest",
    "ActorTerminalTurn",
    "ActorToolRequest",
    "ActorToolResultContext",
    "ActualContextRead",
    "CompositeRunResult",
    "CompositeRuntime",
    "CompositeRuntimeError",
    "CompositeToolInterface",
    "PreCallContextManifest",
    "ProviderRoundEvidence",
    "ScratchWorkspaceBinding",
    "StageExecutionEvidence",
]
