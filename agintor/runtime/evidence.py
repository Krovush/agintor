from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..contracts.epochs import (
    DeploymentIdentity,
    REPO_REPAIR_CAPABILITY_EPOCH,
    ResearchEpochManifest,
    TaskEnvelope,
    TaskCeilings,
    TrustedToolAuthority,
    assert_task_bound_to_epoch,
)
from ..contracts.harness import CompositeRunPlan
from ..contracts.outcomes import OutcomeCost, OutcomeReceipt, PairKey
from ..contracts.run_evidence import (
    ArtifactDeliveryEvidence,
    ArtifactEvidence,
    ArtifactReadEvidence,
    ContextEntry,
    CostLedgerEvidence,
    EnvironmentEvidence,
    ObservedValue,
    PatchEvidence,
    PreCallContextEvidence,
    ProviderCallEvidence,
    RetryEvidence,
    RouteEvidence,
    RunEvidence,
    RunHealth,
    RunProofRecord,
    TerminationEvidence,
    ToolReceiptEvidence,
    assert_no_resolved_credentials,
)
from ..core.identity import canonical_identity_digest, evidence_digest
from .kernel.composite_artifacts import (
    ArtifactDeliveryEvidence as RuntimeArtifactDeliveryEvidence,
    ArtifactEvidence as RuntimeArtifactEvidence,
    artifact_payload_digest,
)
from .kernel.composite_budget import AggregateBudgetSnapshot
from .kernel.composite_runtime import (
    ActorCallEvidence as RuntimeActorCallEvidence,
    CompositeRunResult,
    PreCallContextManifest,
    ScratchWorkspaceBinding,
    StageExecutionEvidence,
)

if TYPE_CHECKING:
    from ..storage.proof_records import ImmutableProofRecordStore


class EvidenceAssemblyError(ValueError):
    """Raised when explicit execution facts do not reconcile exactly."""


class AssemblyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)


class RuntimeEvidenceEpoch(AssemblyModel):
    """Public epoch authority sufficient to bind runtime-side evidence.

    Immutable releases intentionally omit sealed split and evaluator authority.
    Runtime evidence therefore consumes only the public development authority it
    can actually observe instead of reconstructing or importing sealed fields.
    """

    runtime_contract_version: str
    epoch_id: str
    epoch_manifest_digest: str
    capability_epoch: Literal["repo-repair-v1"] = REPO_REPAIR_CAPABILITY_EPOCH
    promotion_capable: Literal[True] = True
    task_manifest_digest: str
    development_split_digest: str
    deployment: DeploymentIdentity
    per_run_ceilings: TaskCeilings
    trusted_tools: tuple[TrustedToolAuthority, ...]

    @classmethod
    def from_epoch(
        cls,
        epoch: ResearchEpochManifest | Mapping[str, Any] | Any,
    ) -> "RuntimeEvidenceEpoch":
        if isinstance(epoch, Mapping):
            source = dict(epoch)
        elif hasattr(epoch, "model_dump"):
            source = epoch.model_dump(mode="python")
        else:
            raise TypeError("runtime evidence epoch must be a typed contract or mapping")
        return cls.model_validate(
            {
                field_name: source[field_name]
                for field_name in cls.model_fields
            }
        )


class ProviderCallDetail(AssemblyModel):
    provider_call_id: str
    sequence_no: int = Field(ge=0)
    call_id: str
    actor_id: str
    turn_index: int = Field(ge=0)
    attempt_index: int = Field(ge=0)
    runtime_context_manifest_digest: str
    reservation_id: str | None = None
    deployment_id: str
    provider: str
    model: str
    provider_config_digest: str
    request_digest: str
    status: Literal["succeeded", "failed_pre_send", "failed_post_send"]
    request_sent: bool
    response_id: str | None = None
    response_digest: str | None = None
    response_kind: Literal["tool_request", "terminal"] | None = None
    tool_request_id: str | None = None
    started_at_ms: int = Field(ge=0)
    finished_at_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_shape(self) -> "ProviderCallDetail":
        assert_no_resolved_credentials(self.model_dump(mode="python"))
        if self.finished_at_ms < self.started_at_ms:
            raise ValueError("provider detail finish precedes start")
        if self.status == "failed_pre_send":
            if self.request_sent or self.response_id:
                raise ValueError("pre-send provider failure cannot claim sent-side identities")
        elif not self.request_sent:
            raise ValueError("successful/post-send provider detail requires request_sent")
        if self.status == "succeeded" and (
            not self.response_id
            or not self.response_digest
            or not self.reservation_id
            or self.response_kind is None
        ):
            raise ValueError(
                "successful provider detail requires response, kind, and reservation identities"
            )
        if self.status != "succeeded" and (
            self.response_digest is not None
            or self.response_kind is not None
            or self.tool_request_id is not None
        ):
            raise ValueError("failed provider detail may not claim response content")
        if (self.response_kind == "tool_request") != bool(self.tool_request_id):
            raise ValueError("tool-request provider detail requires exactly one request id")
        return self


class PublicVerificationEvidence(AssemblyModel):
    status: Literal["passed", "failed", "not_run"]
    plan_digest: str
    patch_digest: str | None = None
    action_receipt_digests: tuple[str, ...] = ()
    completed_at_ms: int | None = Field(default=None, ge=0)
    verification_digest: str = ""

    @model_validator(mode="after")
    def validate_verification(self) -> "PublicVerificationEvidence":
        if self.status == "not_run":
            if self.patch_digest or self.action_receipt_digests or self.completed_at_ms is not None:
                raise ValueError("not-run public verification cannot claim receipts or patch")
        elif not self.patch_digest or not self.action_receipt_digests or self.completed_at_ms is None:
            raise ValueError("completed public verification requires patch, receipts, and time")
        payload = self.model_dump(mode="python", exclude={"verification_digest"})
        computed = evidence_digest({"kind": "public-verification-v1", **payload})
        if self.verification_digest and self.verification_digest != computed:
            raise ValueError("public verification digest mismatch")
        if not self.verification_digest:
            object.__setattr__(self, "verification_digest", computed)
        return self


class PartialCompositeRunObservation(AssemblyModel):
    run_id: str
    status: Literal["failed"] = "failed"
    failure_kind: str
    failed_call_id: str | None = None
    task_envelope_digest: str
    compiled_semantic_digest: str
    scratch_workspace: ScratchWorkspaceBinding
    actor_calls: tuple[RuntimeActorCallEvidence, ...] = ()
    stages: tuple[StageExecutionEvidence, ...] = ()
    context_manifests: tuple[PreCallContextManifest, ...] = ()
    artifacts: tuple[RuntimeArtifactEvidence, ...] = ()
    artifact_deliveries: tuple[RuntimeArtifactDeliveryEvidence, ...] = ()
    budget: AggregateBudgetSnapshot

    @field_validator("failure_kind")
    @classmethod
    def validate_failure_kind(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("partial failure_kind may not be empty")
        return normalized


def public_verification_plan_digest(plan: CompositeRunPlan) -> str:
    return evidence_digest(
        {
            "kind": "public-verification-plan-v1",
            "plan": plan.public_verification.model_dump(mode="python"),
        }
    )


def public_verification_action_digest(action: Any) -> str:
    return canonical_identity_digest(
        action.model_dump(mode="python"),
        domain="public-verification-invocation",
    )


def tool_manifest_digest(plan: CompositeRunPlan) -> str:
    return canonical_identity_digest(
        [tool.model_dump(mode="python") for tool in plan.dependency_manifest.trusted_tools],
        domain="trusted-tool-manifest",
    )


def _runtime_value_digest(value: Any) -> str:
    return canonical_identity_digest(value, domain="runtime-context-value")


def _validated_inputs(
    plan: CompositeRunPlan,
    task: TaskEnvelope,
    epoch: ResearchEpochManifest | RuntimeEvidenceEpoch | Mapping[str, Any],
    result: CompositeRunResult | PartialCompositeRunObservation,
    pair_key: PairKey,
    environment: EnvironmentEvidence,
) -> tuple[CompositeRunPlan, TaskEnvelope, RuntimeEvidenceEpoch]:
    try:
        plan = CompositeRunPlan.model_validate(plan.model_dump(mode="python"))
        task = TaskEnvelope.model_validate(task.model_dump(mode="python"))
        runtime_epoch = RuntimeEvidenceEpoch.from_epoch(epoch)
    except Exception as exc:
        raise EvidenceAssemblyError("plan/task/epoch boundary validation failed") from exc
    if isinstance(epoch, ResearchEpochManifest):
        assert_task_bound_to_epoch(task, epoch)
    elif (
        task.runtime_contract_version != runtime_epoch.runtime_contract_version
        or task.epoch_id != runtime_epoch.epoch_id
        or task.epoch_manifest_digest != runtime_epoch.epoch_manifest_digest
        or task.capability_epoch != runtime_epoch.capability_epoch
        or task.data_state != "development"
        or task.split_manifest_digest != runtime_epoch.development_split_digest
        or not task.ceilings.is_within(runtime_epoch.per_run_ceilings)
    ):
        raise EvidenceAssemblyError("task crossed public runtime epoch authority")
    if plan.task_envelope_digest != task.task_manifest_digest:
        raise EvidenceAssemblyError("plan task identity crossed TaskEnvelope")
    if result.task_envelope_digest != task.task_manifest_digest:
        raise EvidenceAssemblyError("result task identity crossed TaskEnvelope")
    if result.compiled_semantic_digest != plan.compiled_semantic_digest:
        raise EvidenceAssemblyError("result compiled identity crossed CompositeRunPlan")
    if result.scratch_workspace.workspace_digest != task.workspace_snapshot.digest:
        raise EvidenceAssemblyError("result scratch identity crossed TaskEnvelope snapshot")
    if plan.budget_ledger.aggregate_ceiling != task.ceilings:
        raise EvidenceAssemblyError("plan budget crossed TaskEnvelope ceilings")
    if pair_key.task_manifest_id != task.task_manifest_id:
        raise EvidenceAssemblyError("PairKey task crossed TaskEnvelope")
    if pair_key.provider_config_digest != runtime_epoch.deployment.provider_config_digest:
        raise EvidenceAssemblyError("PairKey provider configuration crossed epoch")
    if environment.environment_id != pair_key.environment_id:
        raise EvidenceAssemblyError("environment identity crossed PairKey")
    if environment.workspace_snapshot_digest != task.workspace_snapshot.digest:
        raise EvidenceAssemblyError("environment workspace identity crossed TaskEnvelope")
    expected_tools = {tool.tool_id: tool for tool in runtime_epoch.trusted_tools}
    actual_tools = {tool.tool_id: tool for tool in plan.dependency_manifest.trusted_tools}
    if set(expected_tools) != set(actual_tools):
        raise EvidenceAssemblyError("plan trusted tool set crossed epoch")
    for tool_id, expected in expected_tools.items():
        actual = actual_tools[tool_id]
        if (
            actual.implementation_digest != expected.implementation_digest
            or actual.policy_digest != expected.policy_digest
        ):
            raise EvidenceAssemblyError(f"plan trusted tool identity crossed epoch for {tool_id}")
    return plan, task, runtime_epoch


def _execution_order(plan: CompositeRunPlan) -> tuple[str, ...]:
    return tuple(call_id for stage in plan.stages for call_id in stage.call_ids)


def _validate_result_structure(
    plan: CompositeRunPlan,
    result: CompositeRunResult | PartialCompositeRunObservation,
) -> tuple[str, ...]:
    plan_calls = {call.call_id: call for call in plan.actor_calls}
    plan_stages = {stage.stage_id: stage for stage in plan.stages}
    actual_ids = tuple(call.call_id for call in result.actor_calls)
    expected_order = _execution_order(plan)
    if isinstance(result, CompositeRunResult):
        if actual_ids != expected_order:
            raise EvidenceAssemblyError("successful result actor calls do not exactly cover plan")
    elif actual_ids != expected_order[: len(actual_ids)]:
        raise EvidenceAssemblyError("partial result completed calls are not an execution prefix")
    if len(actual_ids) != len(set(actual_ids)):
        raise EvidenceAssemblyError("result contains duplicate actor call evidence")
    for actual in result.actor_calls:
        planned = plan_calls.get(actual.call_id)
        if planned is None:
            raise EvidenceAssemblyError("result contains an extra actor call")
        expected_stage = next(stage.stage_id for stage in plan.stages if actual.call_id in stage.call_ids)
        if (
            actual.actor_id != planned.actor_id
            or actual.call_kind != planned.call_kind
            or actual.stage_id != expected_stage
            or tuple(actual.artifact_ids_written)
            != tuple(write.artifact_id for write in planned.artifact_writes)
            or tuple(actual.actual_read_ids)
            != tuple(read.read_id for read in planned.context_reads)
        ):
            raise EvidenceAssemblyError(f"result actor call crossed plan for {actual.call_id}")
        if bool(actual.final_patch_digest) != planned.emits_final_patch:
            raise EvidenceAssemblyError("result final patch ownership crossed plan")
    stage_ids = tuple(stage.stage_id for stage in result.stages)
    expected_stage_ids = tuple(stage.stage_id for stage in plan.stages)
    if isinstance(result, CompositeRunResult):
        if stage_ids != expected_stage_ids:
            raise EvidenceAssemblyError("successful result stages do not exactly cover plan")
    elif stage_ids != expected_stage_ids[: len(stage_ids)]:
        raise EvidenceAssemblyError("partial result stages are not an execution prefix")
    for actual in result.stages:
        planned = plan_stages.get(actual.stage_id)
        if planned is None or (
            actual.stage_index != planned.stage_index
            or actual.logical_fork != planned.fork
            or actual.logical_join != planned.join
            or actual.call_execution_order != planned.call_ids
        ):
            raise EvidenceAssemblyError(f"result stage crossed plan for {actual.stage_id}")
    return actual_ids


def _context_evidence(
    plan: CompositeRunPlan,
    task: TaskEnvelope,
    result: CompositeRunResult | PartialCompositeRunObservation,
) -> tuple[tuple[PreCallContextEvidence, ...], dict[str, str]]:
    plan_calls = {call.call_id: call for call in plan.actor_calls}
    runtime_calls = {call.call_id: call for call in result.actor_calls}
    artifact_producers = {
        write.artifact_id: write.producer_call_id
        for call in plan.actor_calls
        for write in call.artifact_writes
    }
    manifests = tuple(result.context_manifests)
    call_ids = [manifest.call_id for manifest in manifests]
    expected_order = _execution_order(plan)
    if tuple(call_ids) != expected_order[: len(call_ids)] or len(call_ids) != len(set(call_ids)):
        raise EvidenceAssemblyError("context manifests are not a unique execution prefix")
    contexts: list[PreCallContextEvidence] = []
    context_ids: dict[str, str] = {}
    for sequence_no, manifest in enumerate(manifests, start=1):
        call = plan_calls[manifest.call_id]
        if (
            manifest.actor_id != call.actor_id
            or manifest.task_envelope_digest != task.task_manifest_digest
            or not manifest.assembled_before_provider
        ):
            raise EvidenceAssemblyError(f"pre-call manifest crossed plan/task for {call.call_id}")
        if tuple(read.read_id for read in manifest.reads) != tuple(
            read.read_id for read in call.context_reads
        ):
            raise EvidenceAssemblyError(f"pre-call reads crossed plan for {call.call_id}")
        entries: list[ContextEntry] = [
            ContextEntry(
                entry_id=f"{call.call_id}.instruction",
                source_kind="instruction",
                source_ref=f"actors[{call.actor_id}].instruction",
                observed=ObservedValue(
                    value={
                        "instruction": call.instruction,
                        "revision_instruction": call.revision_instruction,
                    }
                ),
            )
        ]
        for planned, actual in zip(call.context_reads, manifest.reads):
            if (
                actual.source_kind != planned.source_kind
                or actual.source_ref != planned.source_ref
            ):
                raise EvidenceAssemblyError(f"actual context read crossed plan for {call.call_id}")
            assert_no_resolved_credentials(actual.value)
            expected_digest = (
                artifact_payload_digest(str(actual.value))
                if actual.source_kind == "artifact"
                else _runtime_value_digest(actual.value)
            )
            if actual.value_digest != expected_digest:
                raise EvidenceAssemblyError(f"actual context value digest mismatch for {actual.read_id}")
            if actual.source_kind == "task":
                expected_value = (
                    task.issue
                    if actual.source_ref == "issue"
                    else [step.model_dump(mode="json") for step in task.public_reproduction]
                )
                if actual.value != expected_value or actual.provenance_ref != task.task_manifest_digest:
                    raise EvidenceAssemblyError("task context value/provenance crossed TaskEnvelope")
            elif actual.source_kind == "workspace":
                expected_value = {
                    "workspace_id": result.scratch_workspace.workspace_id,
                    "workspace_digest": result.scratch_workspace.workspace_digest,
                }
                if actual.value != expected_value or actual.provenance_ref != result.scratch_workspace.workspace_id:
                    raise EvidenceAssemblyError("workspace context crossed scratch binding")
            elif actual.source_kind == "artifact":
                if actual.provenance_ref != artifact_producers.get(actual.source_ref):
                    raise EvidenceAssemblyError("artifact context provenance crossed plan producer")
            elif actual.source_kind == "prior_actor_output":
                prior = runtime_calls.get(actual.source_ref)
                if (
                    prior is None
                    or actual.provenance_ref != actual.source_ref
                    or actual.value_digest != prior.output_text_digest
                ):
                    raise EvidenceAssemblyError("prior actor output crossed completed call evidence")
            elif actual.source_kind == "session":
                if (
                    actual.source_ref != "public_carryover"
                    or not isinstance(actual.value, dict)
                    or actual.value.get("context_digest") != actual.provenance_ref
                ):
                    raise EvidenceAssemblyError("public session context crossed its declared context digest")
            entries.append(
                ContextEntry(
                    entry_id=f"{call.call_id}.{actual.read_id}",
                    source_kind=actual.source_kind,
                    source_ref=actual.source_ref,
                    observed=ObservedValue(value=actual.value),
                )
            )
        context_id = f"context.{call.call_id}"
        context = PreCallContextEvidence(
            context_id=context_id,
            sequence_no=sequence_no,
            call_id=call.call_id,
            actor_id=call.actor_id,
            entries=tuple(entries),
        )
        contexts.append(context)
        context_ids[call.call_id] = context_id
    if isinstance(result, CompositeRunResult) and len(contexts) != len(plan.actor_calls):
        raise EvidenceAssemblyError("successful result lacks exact pre-call contexts")
    return tuple(contexts), context_ids


def _artifact_evidence(
    plan: CompositeRunPlan,
    result: CompositeRunResult | PartialCompositeRunObservation,
    contexts: tuple[PreCallContextEvidence, ...],
) -> tuple[
    tuple[ArtifactEvidence, ...],
    tuple[ArtifactDeliveryEvidence, ...],
    tuple[ArtifactReadEvidence, ...],
]:
    write_plans = {
        write.artifact_id: write
        for call in plan.actor_calls
        for write in call.artifact_writes
    }
    delivery_plans = {
        (delivery.artifact_id, delivery.consumer_call_id): delivery
        for delivery in plan.artifact_deliveries
    }
    runtime_artifacts: dict[str, Any] = {}
    artifacts: list[ArtifactEvidence] = []
    for record in result.artifacts:
        artifact = record.artifact
        planned = write_plans.get(artifact.artifact_id)
        if planned is None or (
            artifact.channel_id != planned.channel_id
            or artifact.producer_call_id != planned.producer_call_id
            or artifact.payload_kind != planned.payload_kind
            or artifact.max_bytes != planned.max_bytes
            or not artifact.immutable
            or artifact.payload_digest != artifact_payload_digest(artifact.payload)
            or artifact.byte_size != len(artifact.payload.encode("utf-8"))
        ):
            raise EvidenceAssemblyError(f"runtime artifact crossed plan/value for {artifact.artifact_id}")
        expected_direct_consumers = tuple(
            delivery.consumer_call_id
            for delivery in plan.artifact_deliveries
            if delivery.artifact_id == artifact.artifact_id
        )
        if artifact.intended_consumer_call_ids != expected_direct_consumers:
            raise EvidenceAssemblyError("runtime artifact intended consumers crossed plan")
        expected_all_consumers = tuple(
            sorted(
                call.call_id
                for call in plan.actor_calls
                if any(
                    read.source_kind == "artifact"
                    and read.source_ref == artifact.artifact_id
                    for read in call.context_reads
                )
            )
        )
        actual_consumers = tuple(sorted(record.actual_consumer_call_ids))
        if not set(actual_consumers).issubset(expected_all_consumers):
            raise EvidenceAssemblyError("runtime artifact has undeclared consumers")
        if isinstance(result, CompositeRunResult) and actual_consumers != expected_all_consumers:
            raise EvidenceAssemblyError("successful runtime artifact consumers do not cover plan reads")
        observed = ObservedValue(value=artifact.payload)
        artifacts.append(
            ArtifactEvidence(
                artifact_id=artifact.artifact_id,
                channel_id=artifact.channel_id,
                producer_call_id=artifact.producer_call_id,
                artifact_schema="text",
                observed=observed,
                payload_bytes=artifact.byte_size,
                intended_consumer_call_ids=expected_all_consumers,
                actual_consumer_call_ids=actual_consumers,
            )
        )
        runtime_artifacts[artifact.artifact_id] = artifact
    if isinstance(result, CompositeRunResult) and set(runtime_artifacts) != set(write_plans):
        raise EvidenceAssemblyError("successful result artifact set does not exactly cover plan")

    deliveries: list[ArtifactDeliveryEvidence] = []
    delivery_ids: dict[tuple[str, str], str] = {}
    for sequence_no, runtime_delivery in enumerate(result.artifact_deliveries, start=1):
        key = (runtime_delivery.artifact_id, runtime_delivery.consumer_call_id)
        planned = delivery_plans.get(key)
        artifact = runtime_artifacts.get(runtime_delivery.artifact_id)
        if planned is None or artifact is None or (
            runtime_delivery.channel_id != planned.channel_id
            or runtime_delivery.producer_call_id != planned.producer_call_id
            or runtime_delivery.payload != artifact.payload
            or runtime_delivery.payload_digest != artifact.payload_digest
            or runtime_delivery.byte_size != artifact.byte_size
        ):
            raise EvidenceAssemblyError("runtime artifact delivery crossed plan/produced value")
        delivery_id = f"delivery.{runtime_delivery.artifact_id}.{runtime_delivery.consumer_call_id}"
        delivery_ids[key] = delivery_id
        deliveries.append(
            ArtifactDeliveryEvidence(
                delivery_id=delivery_id,
                sequence_no=sequence_no,
                artifact_id=runtime_delivery.artifact_id,
                channel_id=runtime_delivery.channel_id,
                producer_call_id=runtime_delivery.producer_call_id,
                consumer_call_id=runtime_delivery.consumer_call_id,
                observed=ObservedValue(value=runtime_delivery.payload),
                payload_bytes=runtime_delivery.byte_size,
            )
        )
    if isinstance(result, CompositeRunResult) and set(delivery_ids) != set(delivery_plans):
        raise EvidenceAssemblyError("successful result deliveries do not exactly cover plan")

    contexts_by_call = {context.call_id: context for context in contexts}
    reads: list[ArtifactReadEvidence] = []
    sequence_no = 0
    runtime_contexts = {item.call_id: item for item in result.context_manifests}
    for call_id in _execution_order(plan):
        manifest = runtime_contexts.get(call_id)
        if manifest is None:
            continue
        context = contexts_by_call[call_id]
        for actual in manifest.reads:
            if actual.source_kind != "artifact":
                continue
            artifact = runtime_artifacts.get(actual.source_ref)
            if artifact is None or actual.value != artifact.payload or actual.value_digest != artifact.payload_digest:
                raise EvidenceAssemblyError("artifact context read crossed produced value")
            sequence_no += 1
            key = (artifact.artifact_id, call_id)
            direct = key in delivery_ids
            reads.append(
                ArtifactReadEvidence(
                    read_id=f"read.{call_id}.{actual.read_id}",
                    sequence_no=sequence_no,
                    artifact_id=artifact.artifact_id,
                    channel_id=artifact.channel_id,
                    consumer_call_id=call_id,
                    context_id=context.context_id,
                    context_entry_id=f"{call_id}.{actual.read_id}",
                    read_kind="direct_delivery" if direct else "retained_read",
                    delivery_id=delivery_ids.get(key),
                    observed=ObservedValue(value=actual.value),
                    payload_bytes=artifact.byte_size,
                )
            )
    return tuple(artifacts), tuple(deliveries), tuple(reads)


def _provider_evidence(
    plan: CompositeRunPlan,
    result: CompositeRunResult | PartialCompositeRunObservation,
    details: Sequence[ProviderCallDetail],
    contexts: tuple[PreCallContextEvidence, ...],
) -> tuple[ProviderCallEvidence, ...]:
    plan_calls = {call.call_id: call for call in plan.actor_calls}
    runtime_contexts = {context.call_id: context for context in result.context_manifests}
    o1_contexts = {context.call_id: context for context in contexts}
    completed_calls = {call.call_id: call for call in result.actor_calls}
    grouped: dict[str, list[ProviderCallDetail]] = {}
    seen_ids: set[str] = set()
    for detail in details:
        if detail.provider_call_id in seen_ids:
            raise EvidenceAssemblyError("duplicate explicit provider call detail")
        seen_ids.add(detail.provider_call_id)
        if detail.call_id not in plan_calls or detail.call_id not in runtime_contexts:
            raise EvidenceAssemblyError("provider detail has extra/missing context or plan call")
        if detail.actor_id != plan_calls[detail.call_id].actor_id:
            raise EvidenceAssemblyError("provider detail actor crossed plan")
        if detail.runtime_context_manifest_digest != runtime_contexts[detail.call_id].manifest_digest:
            raise EvidenceAssemblyError("provider detail context crossed runtime manifest")
        grouped.setdefault(detail.call_id, []).append(detail)
    if set(grouped) != set(runtime_contexts):
        raise EvidenceAssemblyError("missing explicit provider detail for attempted call")
    for call_id, details_for_call in grouped.items():
        details_for_call.sort(key=lambda item: (item.turn_index, item.attempt_index))
        turns: dict[int, list[ProviderCallDetail]] = {}
        for detail in details_for_call:
            turns.setdefault(detail.turn_index, []).append(detail)
        if sorted(turns) != list(range(len(turns))):
            raise EvidenceAssemblyError("provider turn indexes must be contiguous from zero")
        for attempts in turns.values():
            if [item.attempt_index for item in attempts] != list(range(len(attempts))):
                raise EvidenceAssemblyError(
                    "provider retry attempt indexes must be contiguous from zero per turn"
                )
            succeeded = [item for item in attempts if item.status == "succeeded"]
            if len(succeeded) > 1 or (succeeded and succeeded[0] != attempts[-1]):
                raise EvidenceAssemblyError(
                    "each provider turn may have only one final successful attempt"
                )
        completed = completed_calls.get(call_id)
        if completed is not None:
            runtime_rounds = tuple(completed.provider_rounds)
            successful = tuple(
                detail for detail in details_for_call if detail.status == "succeeded"
            )
            if len(successful) != len(runtime_rounds):
                raise EvidenceAssemblyError(
                    "completed actor call provider details do not cover every runtime round"
                )
            for detail, runtime_round in zip(successful, runtime_rounds):
                if (
                    detail.turn_index != runtime_round.turn_index
                    or detail.request_digest != runtime_round.request_digest
                    or detail.reservation_id != runtime_round.reservation_id
                    or detail.status != runtime_round.status
                    or detail.response_id != runtime_round.response_id
                    or detail.response_digest != runtime_round.response_digest
                    or detail.response_kind != runtime_round.response_kind
                    or detail.tool_request_id != runtime_round.tool_request_id
                    or detail.started_at_ms != runtime_round.started_at_ms
                    or detail.finished_at_ms != runtime_round.finished_at_ms
                ):
                    raise EvidenceAssemblyError(
                        "provider detail crossed exact runtime provider-round evidence"
                    )
            if not successful or successful[-1].response_kind != "terminal":
                raise EvidenceAssemblyError("completed actor call lacks a terminal provider round")
            final = successful[-1]
            if (
                final.request_digest != completed.request_digest
                or final.reservation_id != completed.provider_reservation_id
                or completed.context_manifest_digest
                != runtime_contexts[call_id].manifest_digest
                or completed.provider_status != "succeeded"
            ):
                raise EvidenceAssemblyError("provider detail crossed actor-call result")
        elif any(
            detail.status == "succeeded" and detail.response_kind == "terminal"
            for detail in details_for_call
        ):
            raise EvidenceAssemblyError("partial failed call cannot claim a terminal provider detail")
    converted: list[ProviderCallEvidence] = []
    for sequence_no, detail in enumerate(
        sorted(details, key=lambda item: item.sequence_no), start=1
    ):
        if detail.sequence_no != sequence_no:
            raise EvidenceAssemblyError("provider detail sequence numbers must be contiguous")
        context = o1_contexts[detail.call_id]
        converted.append(
            ProviderCallEvidence(
                provider_call_id=detail.provider_call_id,
                sequence_no=sequence_no,
                call_id=detail.call_id,
                actor_id=detail.actor_id,
                turn_index=detail.turn_index,
                attempt_index=detail.attempt_index,
                context_id=context.context_id,
                context_digest=context.context_digest,
                deployment_id=detail.deployment_id,
                provider=detail.provider,
                model=detail.model,
                provider_config_digest=detail.provider_config_digest,
                request_digest=detail.request_digest,
                status=detail.status,
                request_sent=detail.request_sent,
                response_id=detail.response_id,
                response_digest=detail.response_digest,
                response_kind=detail.response_kind,
                tool_request_id=detail.tool_request_id,
                started_at_ms=detail.started_at_ms,
                finished_at_ms=detail.finished_at_ms,
            )
        )
    return tuple(converted)


def _route_evidence(
    plan: CompositeRunPlan,
    attempted_call_ids: Sequence[str],
) -> tuple[RouteEvidence, ...]:
    stage_by_call = {
        call_id: stage for stage in plan.stages for call_id in stage.call_ids
    }
    routes: list[RouteEvidence] = []
    for sequence_no, call_id in enumerate(attempted_call_ids, start=1):
        stage = stage_by_call[call_id]
        dependencies = [
            dependency_call
            for dependency_id in stage.depends_on_stage_ids
            for dependency_call in next(
                item.call_ids for item in plan.stages if item.stage_id == dependency_id
            )
        ]
        route_kind = (
            "revision"
            if stage.revision
            else "join"
            if stage.join
            else "fork"
            if stage.fork
            else "sequential"
        )
        trigger = (
            "public_verification_failed"
            if stage.revision
            else "join_completed"
            if stage.join
            else "producer_completed"
            if dependencies
            else "plan_start"
        )
        routes.append(
            RouteEvidence(
                route_id=f"route.{stage.stage_id}.{call_id}",
                sequence_no=sequence_no,
                route_kind=route_kind,
                from_call_id=dependencies[-1] if dependencies else None,
                to_call_id=call_id,
                stage_id=stage.stage_id,
                trigger=trigger,
            )
        )
    return tuple(routes)


def _validate_public_verification(
    plan: CompositeRunPlan,
    verification: PublicVerificationEvidence,
    tool_receipts: Sequence[ToolReceiptEvidence],
    patch_digest: str | None,
) -> None:
    if verification.plan_digest != public_verification_plan_digest(plan):
        raise EvidenceAssemblyError("public verification plan identity crossed CompositeRunPlan")
    if verification.status != "not_run" and verification.patch_digest != patch_digest:
        raise EvidenceAssemblyError("public verification patch identity crossed runtime output")
    public_receipts = tuple(
        receipt
        for receipt in tool_receipts
        if receipt.phase == "terminal_public_verification"
    )
    if verification.status == "not_run":
        if public_receipts:
            raise EvidenceAssemblyError("public-test tool receipts exist for not-run verification")
    elif (
        verification.action_receipt_digests
        != tuple(receipt.receipt_digest for receipt in public_receipts)
        or len(public_receipts) != len(plan.public_verification.actions)
    ):
        raise EvidenceAssemblyError("public verification receipts do not exactly cover plan actions")
    if verification.status != "not_run":
        expected_invocations = tuple(
            public_verification_action_digest(action)
            for action in plan.public_verification.actions
        )
        actual_invocations = tuple(receipt.invocation_digest for receipt in public_receipts)
        if actual_invocations != expected_invocations:
            raise EvidenceAssemblyError("public verification invocation crossed plan actions")
        expected_step_ids = tuple(action.step_id for action in plan.public_verification.actions)
        actual_step_ids = tuple(receipt.verification_step_id for receipt in public_receipts)
        if actual_step_ids != expected_step_ids:
            raise EvidenceAssemblyError("public verification receipt order crossed plan actions")
        statuses = tuple(receipt.status for receipt in public_receipts)
        if verification.status == "passed" and any(status != "succeeded" for status in statuses):
            raise EvidenceAssemblyError("passed public verification contains a failed receipt")
        if verification.status == "failed" and all(status == "succeeded" for status in statuses):
            raise EvidenceAssemblyError("failed public verification lacks a failing receipt")


def _validate_budget(
    prefix: AggregateBudgetSnapshot,
    final: AggregateBudgetSnapshot,
    provider_calls: Sequence[ProviderCallEvidence],
    tool_receipts: Sequence[ToolReceiptEvidence],
    retries: Sequence[RetryEvidence],
) -> None:
    monotone_fields = (
        "model_calls",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "cache_write_tokens",
        "known_cost_usd",
        "estimated_cost_usd",
        "unknown_cost_events",
        "unknown_usage_events",
        "tool_calls",
        "tool_output_bytes",
        "retries",
        "latency_ms",
        "elapsed_wall_time_ms",
    )
    if any(getattr(final, field) < getattr(prefix, field) for field in monotone_fields):
        raise EvidenceAssemblyError("final aggregate budget regressed below runtime prefix")
    if final.model_calls != sum(call.request_sent for call in provider_calls):
        raise EvidenceAssemblyError("aggregate model calls do not match explicit provider records")
    if final.tool_calls != len(tool_receipts):
        raise EvidenceAssemblyError("aggregate tool calls do not match explicit receipts")
    if final.tool_output_bytes != sum(receipt.output_bytes for receipt in tool_receipts):
        raise EvidenceAssemblyError("aggregate tool bytes do not match explicit receipts")
    if final.retries != len(retries):
        raise EvidenceAssemblyError("aggregate retries do not match explicit retry records")


def assemble_run_evidence(
    *,
    plan: CompositeRunPlan,
    task: TaskEnvelope,
    epoch: ResearchEpochManifest | RuntimeEvidenceEpoch | Mapping[str, Any],
    release_digest: str,
    release_manifest_digest: str,
    profile_digest: str,
    execution_mode: Literal["deterministic_replay", "live_provider"],
    live_inference_status: Literal["not_run", "completed", "failed"],
    real_inference_requests_sent: int,
    result: CompositeRunResult | PartialCompositeRunObservation,
    pair_key: PairKey,
    provider_calls: Sequence[ProviderCallDetail],
    tool_receipts: Sequence[ToolReceiptEvidence],
    retries: Sequence[RetryEvidence],
    public_verification: PublicVerificationEvidence,
    environment: EnvironmentEvidence,
    final_budget: AggregateBudgetSnapshot,
    no_leakage: bool,
    environment_healthy: bool,
    completed_at_ms: int,
) -> RunEvidence:
    """Reconcile explicit R1/R2 facts into one strict, immutable RunEvidence."""

    try:
        if isinstance(result, CompositeRunResult):
            result = CompositeRunResult.model_validate(result.model_dump(mode="python"))
        else:
            result = PartialCompositeRunObservation.model_validate(
                result.model_dump(mode="python")
            )
        pair_key = PairKey.model_validate(pair_key.model_dump(mode="python"))
        provider_calls = tuple(
            ProviderCallDetail.model_validate(item.model_dump(mode="python"))
            for item in provider_calls
        )
        tool_receipts = tuple(
            ToolReceiptEvidence.model_validate(item.model_dump(mode="python"))
            for item in tool_receipts
        )
        retries = tuple(
            RetryEvidence.model_validate(item.model_dump(mode="python"))
            for item in retries
        )
        public_verification = PublicVerificationEvidence.model_validate(
            public_verification.model_dump(mode="python")
        )
        environment = EnvironmentEvidence.model_validate(
            environment.model_dump(mode="python")
        )
        final_budget = AggregateBudgetSnapshot.model_validate(
            final_budget.model_dump(mode="python")
        )
    except Exception as exc:
        raise EvidenceAssemblyError("explicit runtime evidence failed boundary validation") from exc
    if not isinstance(no_leakage, bool) or not isinstance(environment_healthy, bool):
        raise EvidenceAssemblyError("health assertions must be explicit booleans")
    if not isinstance(completed_at_ms, int) or completed_at_ms < 0:
        raise EvidenceAssemblyError("completed_at_ms must be a nonnegative integer")
    plan, task, epoch = _validated_inputs(
        plan, task, epoch, result, pair_key, environment
    )
    _validate_result_structure(plan, result)
    contexts, _ = _context_evidence(plan, task, result)
    artifacts, deliveries, reads = _artifact_evidence(plan, result, contexts)
    calls = _provider_evidence(plan, result, provider_calls, contexts)
    if sum(call.attempt_index > 0 for call in calls) != len(retries):
        raise EvidenceAssemblyError("provider retry attempts do not match explicit retry evidence")
    attempted_call_ids = tuple(context.call_id for context in contexts)
    routes = _route_evidence(plan, attempted_call_ids)
    for receipt in tool_receipts:
        planned_call = next(
            (call for call in plan.actor_calls if call.call_id == receipt.call_id),
            None,
        )
        if planned_call is None:
            raise EvidenceAssemblyError("tool receipt crossed call authority")
        if receipt.phase == "actor_tool" and receipt.tool_id not in planned_call.tool_ids:
            raise EvidenceAssemblyError("actor tool receipt crossed call authority")
        if receipt.phase == "terminal_public_verification" and (
            receipt.call_id != plan.termination.final_actor_call_id
            or receipt.tool_id != "repo.public_test"
        ):
            raise EvidenceAssemblyError("terminal verification receipt crossed final-call authority")
    runtime_receipts = tuple(getattr(result, "tool_receipts", ()))
    if runtime_receipts:
        external_by_id = {receipt.receipt_id: receipt for receipt in tool_receipts}
        if tuple(receipt.receipt_id for receipt in runtime_receipts) != tuple(
            receipt.receipt_id for receipt in tool_receipts
        ):
            raise EvidenceAssemblyError("explicit tool receipts crossed runtime receipt order")
        status_map = {
            "succeeded": "succeeded",
            "failed": "failed",
            "timed_out": "timed_out",
            "output_limit": "blocked",
            "launch_failed": "blocked",
            "budget_rejected": "blocked",
        }
        for runtime_receipt in runtime_receipts:
            external = external_by_id[runtime_receipt.receipt_id]
            if (
                external.call_id != runtime_receipt.call_id
                or external.tool_id != runtime_receipt.tool_id
                or external.phase != runtime_receipt.phase
                or external.tool_request_id != runtime_receipt.tool_request_id
                or external.verification_step_id != runtime_receipt.verification_step_id
                or external.status != status_map[runtime_receipt.status.value]
                or external.output_digest != runtime_receipt.output_digest
                or external.output_bytes != runtime_receipt.output_bytes
                or external.started_at_ms != runtime_receipt.started_at_ms
                or external.finished_at_ms != runtime_receipt.finished_at_ms
            ):
                raise EvidenceAssemblyError("explicit tool receipt crossed runtime source evidence")
    _validate_budget(result.budget, final_budget, calls, tool_receipts, retries)

    is_complete_result = isinstance(result, CompositeRunResult)
    patch_text = result.final_patch if is_complete_result else None
    patch_digest = result.final_patch_digest if is_complete_result else None
    if patch_text is not None:
        expected_patch_digest = canonical_identity_digest(
            patch_text, domain="final-unified-diff"
        )
        if patch_digest != expected_patch_digest:
            raise EvidenceAssemblyError("runtime final patch digest does not match exact patch")
        terminal_call = next(
            (
                call
                for call in result.actor_calls
                if call.call_id == plan.termination.final_actor_call_id
            ),
            None,
        )
        if terminal_call is None or terminal_call.final_patch_digest != patch_digest:
            raise EvidenceAssemblyError("terminal actor-call patch identity crossed runtime result")
        assert_no_resolved_credentials(patch_text)
    _validate_public_verification(
        plan, public_verification, tool_receipts, patch_digest
    )

    patch_bytes = len(patch_text.encode("utf-8")) if patch_text is not None else 0
    within_envelope = bool(
        not final_budget.violations
        and not final_budget.deadline_exceeded
        and final_budget.reconciled
        and not final_budget.unknown_cost_events
        and not final_budget.unknown_usage_events
        and final_budget.model_calls <= task.ceilings.max_model_calls
        and final_budget.input_tokens <= task.ceilings.max_input_tokens
        and final_budget.output_tokens <= task.ceilings.max_output_tokens
        and final_budget.cached_tokens <= task.ceilings.max_cached_tokens
        and final_budget.cache_write_tokens <= task.ceilings.max_cache_write_tokens
        and final_budget.tool_calls <= task.ceilings.max_tool_calls
        and final_budget.tool_output_bytes <= task.ceilings.max_tool_output_bytes
        and final_budget.retries <= task.ceilings.max_retries
        and patch_bytes <= task.ceilings.max_patch_bytes
        and final_budget.known_cost_usd <= task.ceilings.max_known_cost_usd
        and final_budget.known_cost_usd + final_budget.estimated_cost_usd
        <= task.ceilings.max_estimated_cost_usd
    )
    outcome_cost = OutcomeCost(
        model_calls=final_budget.model_calls,
        input_tokens=final_budget.input_tokens,
        output_tokens=final_budget.output_tokens,
        cached_tokens=final_budget.cached_tokens,
        cache_write_tokens=final_budget.cache_write_tokens,
        tool_calls=final_budget.tool_calls,
        tool_output_bytes=final_budget.tool_output_bytes,
        artifact_bytes=sum(artifact.payload_bytes for artifact in artifacts),
        patch_bytes=patch_bytes,
        retries=final_budget.retries,
        wall_time_ms=final_budget.elapsed_wall_time_ms,
        known_cost_usd=final_budget.known_cost_usd,
        estimated_cost_usd=final_budget.estimated_cost_usd,
        unknown_dollars=bool(final_budget.unknown_cost_events),
        within_epoch_envelope=within_envelope,
    )
    accounting_complete = bool(
        final_budget.reconciled
        and final_budget.active_reservations == 0
        and not final_budget.unknown_cost_events
        and not final_budget.unknown_usage_events
        and not final_budget.violations
    )
    verification_passed = public_verification.status == "passed"
    process_integrity = bool(is_complete_result)
    if is_complete_result:
        termination_reason = (
            "success"
            if verification_passed
            else "public_verification_failed"
            if public_verification.status == "failed"
            else "hard_failure"
        )
        final_call_id = plan.termination.final_actor_call_id
    else:
        termination_reason = (
            "budget_exhausted"
            if "budget" in result.failure_kind
            else "hard_failure"
        )
        final_call_id = result.failed_call_id

    cost_ledger = CostLedgerEvidence(
        cost=outcome_cost,
        provider_deadline_ms=task.ceilings.provider_deadline_ms,
        deadline_exceeded=final_budget.deadline_exceeded,
        active_reservations=final_budget.active_reservations,
        reconciled=accounting_complete,
    )
    evidence_id = "run-evidence." + evidence_digest(
        {
            "run": result.run_id,
            "pair": pair_key.model_dump(mode="python"),
            "protocol": plan.source_protocol_digest,
            "compiled": plan.compiled_semantic_digest,
        }
    )[:24]
    return RunEvidence(
        evidence_id=evidence_id,
        run_id=result.run_id,
        execution_mode=execution_mode,
        live_inference_status=live_inference_status,
        real_inference_requests_sent=real_inference_requests_sent,
        data_state=task.data_state,
        epoch_id=epoch.epoch_id,
        epoch_manifest_digest=epoch.epoch_manifest_digest,
        release_digest=release_digest,
        release_manifest_digest=release_manifest_digest,
        profile_digest=profile_digest,
        split_manifest_digest=task.split_manifest_digest,
        pair_key=pair_key,
        task_manifest_digest=task.task_manifest_digest,
        protocol_digest=plan.source_protocol_digest,
        compiled_semantic_digest=plan.compiled_semantic_digest,
        dependency_manifest_digest=plan.dependency_manifest_digest,
        compiler_digest=plan.dependency_manifest.compiler.implementation_digest,
        kernel_digest=plan.dependency_manifest.kernel.implementation_digest,
        tool_manifest_digest=tool_manifest_digest(plan),
        provider_config_digest=epoch.deployment.provider_config_digest,
        decoding_policy_digest=epoch.deployment.decoding_policy_digest,
        price_schedule_digest=epoch.deployment.price_schedule_digest,
        command_container_policy_digest=(
            epoch.deployment.command_container_policy_digest
        ),
        deployment_id=epoch.deployment.deployment_id,
        provider=epoch.deployment.provider,
        model=epoch.deployment.model,
        contexts=contexts,
        artifacts=artifacts,
        deliveries=deliveries,
        reads=reads,
        routes=routes,
        provider_calls=calls,
        tool_receipts=tuple(tool_receipts),
        retries=tuple(retries),
        cost_ledger=cost_ledger,
        environment=environment,
        patch=PatchEvidence(
            status="emitted" if patch_digest else "not_emitted",
            observed=(ObservedValue(value=patch_text) if patch_text is not None else None),
            patch_digest=patch_digest,
            patch_bytes=patch_bytes,
            public_verification_passed=(verification_passed if patch_digest else None),
        ),
        termination=TerminationEvidence(
            reason=termination_reason,
            final_call_id=final_call_id,
            final_patch_digest=patch_digest,
            completed_at_ms=completed_at_ms,
            success=termination_reason == "success",
        ),
        health=RunHealth(
            process_integrity=process_integrity,
            no_leakage=bool(no_leakage),
            context_integrity=True,
            artifact_integrity=True,
            tool_integrity=True,
            accounting_complete=accounting_complete,
            environment_integrity=bool(environment_healthy),
        ),
    )


def bind_and_append_evaluator_proof(
    *,
    epoch: ResearchEpochManifest,
    task: TaskEnvelope,
    run_evidence: RunEvidence,
    outcome_receipt: OutcomeReceipt,
    store: "ImmutableProofRecordStore",
) -> tuple[RunProofRecord, Path]:
    """Cross-bind evaluator authority to runtime evidence and append immutably."""

    from ..search.promotion import assert_authoritative_outcome_receipt

    try:
        epoch = ResearchEpochManifest.model_validate(epoch.model_dump(mode="python"))
        task = TaskEnvelope.model_validate(task.model_dump(mode="python"))
        run_evidence = RunEvidence.model_validate(
            run_evidence.model_dump(mode="python")
        )
        outcome_receipt = OutcomeReceipt.model_validate(
            outcome_receipt.model_dump(mode="python")
        )
    except Exception as exc:
        raise EvidenceAssemblyError("proof binding inputs failed boundary validation") from exc
    assert_task_bound_to_epoch(task, epoch)
    assert_authoritative_outcome_receipt(outcome_receipt, epoch)
    if not run_evidence.health.healthy:
        raise EvidenceAssemblyError(
            "partial or unhealthy RunEvidence cannot bind evaluator outcome authority"
        )
    if (
        run_evidence.patch.status != "emitted"
        or run_evidence.termination.reason
        not in {"success", "public_verification_failed"}
    ):
        raise EvidenceAssemblyError(
            "RunEvidence lacks a complete emitted candidate for evaluator authority"
        )
    if (
        run_evidence.termination.reason == "success"
        and run_evidence.patch.public_verification_passed is not True
    ) or (
        run_evidence.termination.reason == "public_verification_failed"
        and run_evidence.patch.public_verification_passed is not False
    ):
        raise EvidenceAssemblyError(
            "RunEvidence public-verification quality crossed termination evidence"
        )
    expected = {
        "capability_epoch": epoch.capability_epoch,
        "data_state": task.data_state,
        "epoch_id": epoch.epoch_id,
        "epoch_manifest_digest": epoch.epoch_manifest_digest,
        "split_manifest_digest": task.split_manifest_digest,
        "task_manifest_digest": task.task_manifest_digest,
        "provider_config_digest": epoch.deployment.provider_config_digest,
        "deployment_id": epoch.deployment.deployment_id,
        "provider": epoch.deployment.provider,
        "model": epoch.deployment.model,
    }
    crossed = [
        field_name
        for field_name, expected_value in expected.items()
        if getattr(run_evidence, field_name) != expected_value
    ]
    if crossed:
        raise EvidenceAssemblyError("run evidence crossed bound epoch/task/deployment: " + ", ".join(crossed))
    if run_evidence.pair_key != outcome_receipt.pair_key:
        raise EvidenceAssemblyError("outcome receipt PairKey crossed RunEvidence")
    record_id = "proof." + evidence_digest(
        {
            "run_evidence": run_evidence.evidence_digest,
            "outcome_receipt": outcome_receipt.receipt_digest,
        }
    )[:24]
    try:
        record = RunProofRecord(
            proof_record_id=record_id,
            run_evidence=run_evidence,
            outcome_receipt=outcome_receipt,
        )
    except Exception as exc:
        raise EvidenceAssemblyError("outcome receipt crossed exact runtime proof identities") from exc
    path = store.append(record)
    return record, path


__all__ = [
    "EvidenceAssemblyError",
    "PartialCompositeRunObservation",
    "ProviderCallDetail",
    "PublicVerificationEvidence",
    "RuntimeEvidenceEpoch",
    "assemble_run_evidence",
    "bind_and_append_evaluator_proof",
    "public_verification_action_digest",
    "public_verification_plan_digest",
    "tool_manifest_digest",
]
