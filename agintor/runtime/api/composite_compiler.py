from __future__ import annotations

import json
from collections.abc import Mapping
from importlib import resources
from typing import Any

from pydantic import ValidationError

from ...contracts.epochs import TaskEnvelope
from ...contracts.harness import (
    ActorBudgetSharePlan,
    ActorCallPlan,
    ArtifactDeliveryPlan,
    ArtifactWritePlan,
    BudgetLedgerPlan,
    CanonicalHarnessSeedReference,
    CompositeCompilerMetadata,
    CompositePlanStage,
    CompositeRunPlan,
    ConsumedFieldBinding,
    ConsumedFieldLivenessManifest,
    ContextReadPlan,
    HarnessActor,
    HarnessArtifactChannel,
    HarnessProtocol,
    HarnessSeedDocument,
    PublicVerificationActionPlan,
    PublicVerificationPlan,
    RuntimeDependencyManifest,
    TerminationPlan,
    source_field_digest,
)
from ...core.identity import composite_plan_digest


CANONICAL_HARNESS_PROTOCOL_FILE = "harness_protocol.json"
COMPOSITE_COMPILER_METADATA_FILE = "composite_compiler_metadata.json"
COMPOSITE_COMPILER_METADATA_RESOURCE = (
    "templates/harness/composite_compiler_metadata.json"
)
CANONICAL_TWO_ACTOR_SEED_RESOURCE = (
    "templates/harness/repo_repair_v1_two_actor_seed.json"
)


class CompositeCompilationError(ValueError):
    """The public task and harness cannot produce one executable run plan."""


class InertProtocolError(CompositeCompilationError):
    """A candidate normalizes to the same executable semantics as its parent."""


def _resource_package(explicit: str | None = None) -> str:
    return explicit or (__package__ or "agintor").split(".", 1)[0]


def _read_json_resource(resource_path: str, *, package: str | None = None) -> Any:
    path = resources.files(_resource_package(package)).joinpath(
        *resource_path.split("/")
    )
    return json.loads(path.read_text(encoding="utf-8"))


def load_composite_compiler_metadata(
    *,
    resource_package: str | None = None,
) -> CompositeCompilerMetadata:
    payload = _read_json_resource(
        COMPOSITE_COMPILER_METADATA_RESOURCE,
        package=resource_package,
    )
    return CompositeCompilerMetadata.model_validate(payload)


def load_canonical_harness_seed(
    reference: CanonicalHarnessSeedReference | None = None,
    *,
    resource_package: str | None = None,
) -> HarnessSeedDocument:
    metadata = load_composite_compiler_metadata(
        resource_package=resource_package
    )
    expected = reference or metadata.canonical_seed
    document = HarnessSeedDocument.model_validate(
        _read_json_resource(expected.resource_path, package=resource_package)
    )
    if document.reference != expected:
        raise CompositeCompilationError(
            "canonical harness seed reference does not match compiler metadata"
        )
    actual_digest = document.protocol.source_digest()
    if actual_digest != document.reference.source_protocol_digest:
        raise CompositeCompilationError(
            "canonical harness seed source_protocol_digest does not match its protocol"
        )
    return document


def _normalize_task(task: TaskEnvelope | Mapping[str, Any]) -> TaskEnvelope:
    try:
        if isinstance(task, TaskEnvelope):
            payload = task.model_dump(mode="python")
        else:
            payload = dict(task)
        return TaskEnvelope.model_validate(payload)
    except (TypeError, ValidationError, ValueError) as exc:
        raise CompositeCompilationError(f"invalid TaskEnvelope: {exc}") from exc


def _normalize_protocol(
    protocol: HarnessProtocol | Mapping[str, Any],
) -> HarnessProtocol:
    try:
        if isinstance(protocol, HarnessProtocol):
            payload = protocol.model_dump(mode="python")
        else:
            payload = dict(protocol)
        return HarnessProtocol.model_validate(payload)
    except (TypeError, ValidationError, ValueError) as exc:
        raise CompositeCompilationError(f"invalid HarnessProtocol: {exc}") from exc


def _normalize_dependencies(
    manifest: RuntimeDependencyManifest | Mapping[str, Any],
) -> RuntimeDependencyManifest:
    try:
        if isinstance(manifest, RuntimeDependencyManifest):
            payload = manifest.model_dump(mode="python")
        else:
            payload = dict(manifest)
        return RuntimeDependencyManifest.model_validate(payload)
    except (TypeError, ValidationError, ValueError) as exc:
        raise CompositeCompilationError(
            f"invalid RuntimeDependencyManifest: {exc}"
        ) from exc


def _validate_dependency_contract(
    task: TaskEnvelope,
    protocol: HarnessProtocol,
    dependencies: RuntimeDependencyManifest,
    metadata: CompositeCompilerMetadata,
) -> None:
    if dependencies.runtime_contract_version != task.runtime_contract_version:
        raise CompositeCompilationError(
            "runtime dependency manifest and task use different contract versions"
        )
    if dependencies.compiler.dependency_id != metadata.compiler_id:
        raise CompositeCompilationError("compiler dependency identity is not supported")
    if dependencies.compiler.interface_version != metadata.compiler_version:
        raise CompositeCompilationError("compiler dependency version is not supported")
    if dependencies.harness_contract.dependency_id != metadata.harness_contract_id:
        raise CompositeCompilationError("harness contract dependency is not supported")
    if (
        dependencies.harness_contract.interface_version
        != metadata.harness_schema_version
    ):
        raise CompositeCompilationError("harness contract version is not supported")

    dependency_tool_ids = tuple(tool.tool_id for tool in dependencies.trusted_tools)
    if set(dependency_tool_ids) != set(metadata.trusted_tool_ids):
        raise CompositeCompilationError(
            "runtime dependency manifest must pin every fixed trusted tool"
        )
    allowed_tool_ids = set(task.allowed_capabilities)
    dependency_tools = set(dependency_tool_ids)
    for actor in protocol.actors:
        unsupported = set(actor.tool_ids) - dependency_tools
        if unsupported:
            raise CompositeCompilationError(
                f"actor {actor.actor_id!r} references unpinned tools {sorted(unsupported)!r}"
            )
        denied = set(actor.tool_ids) - allowed_tool_ids
        if denied:
            raise CompositeCompilationError(
                f"actor {actor.actor_id!r} references task-denied tools {sorted(denied)!r}"
            )


def _path_exists(
    start: str,
    target: str,
    adjacency: dict[str, set[str]],
) -> bool:
    pending = [start]
    visited: set[str] = set()
    while pending:
        actor_id = pending.pop()
        if actor_id == target:
            return True
        if actor_id in visited:
            continue
        visited.add(actor_id)
        pending.extend(sorted(adjacency[actor_id], reverse=True))
    return False


def _compile_topology(
    protocol: HarnessProtocol,
) -> tuple[
    tuple[tuple[str, ...], ...],
    dict[str, int],
    HarnessArtifactChannel | None,
]:
    actor_order = {actor.actor_id: index for index, actor in enumerate(protocol.actors)}
    actors = tuple(actor_order)
    feedback: HarnessArtifactChannel | None = None
    if protocol.revision is not None:
        feedback = next(
            channel
            for channel in protocol.artifact_channels
            if channel.channel_id == protocol.revision.feedback_channel_id
        )
    primary_channels = tuple(
        channel
        for channel in protocol.artifact_channels
        if feedback is None or channel.channel_id != feedback.channel_id
    )

    if len(actors) > 1 and not protocol.artifact_channels:
        raise CompositeCompilationError(
            "multi-actor protocols require consequential artifact exchange"
        )

    adjacency = {actor_id: set() for actor_id in actors}
    incoming = {actor_id: set() for actor_id in actors}
    for channel in primary_channels:
        adjacency[channel.producer_actor_id].add(channel.consumer_actor_id)
        incoming[channel.consumer_actor_id].add(channel.producer_actor_id)

    indegree = {actor_id: len(incoming[actor_id]) for actor_id in actors}
    remaining = set(actors)
    levels: list[tuple[str, ...]] = []
    while remaining:
        ready = tuple(
            sorted(
                (actor_id for actor_id in remaining if indegree[actor_id] == 0),
                key=actor_order.__getitem__,
            )
        )
        if not ready:
            raise CompositeCompilationError(
                "primary artifact channel graph must be acyclic"
            )
        levels.append(ready)
        for actor_id in ready:
            remaining.remove(actor_id)
            for consumer_id in adjacency[actor_id]:
                indegree[consumer_id] -= 1

    final_actor_id = protocol.termination.final_actor_id
    if feedback is None:
        if adjacency[final_actor_id]:
            raise CompositeCompilationError(
                "the final actor must be the sink of the primary artifact graph"
            )
        for actor_id in actors:
            if not _path_exists(actor_id, final_actor_id, adjacency):
                raise CompositeCompilationError(
                    f"actor {actor_id!r} has no artifact path to the final actor"
                )
    else:
        feedback_producer_id = feedback.producer_actor_id
        if not _path_exists(final_actor_id, feedback_producer_id, adjacency):
            raise CompositeCompilationError(
                "revision feedback must come from an actor downstream of the reviser"
            )
        for actor_id in actors:
            if not _path_exists(actor_id, feedback_producer_id, adjacency):
                raise CompositeCompilationError(
                    f"actor {actor_id!r} has no artifact path to revision feedback"
                )

    for actor in protocol.actors:
        if actor.actor_id == final_actor_id and feedback is None:
            continue
        if not any(
            channel.producer_actor_id == actor.actor_id
            for channel in protocol.artifact_channels
        ):
            raise CompositeCompilationError(
                f"non-terminal actor {actor.actor_id!r} must produce an artifact"
            )

    phase_by_actor = {
        actor_id: phase_index
        for phase_index, level in enumerate(levels)
        for actor_id in level
    }
    return tuple(levels), phase_by_actor, feedback


def _task_context_reads(actor: HarnessActor) -> tuple[ContextReadPlan, ...]:
    reads: list[ContextReadPlan] = []
    for field_name in actor.task_view:
        if field_name == "workspace":
            reads.append(
                ContextReadPlan(
                    read_id="workspace.scratch_binding",
                    source_kind="workspace",
                    source_ref="scratch_workspace",
                )
            )
        elif field_name == "session_public_carryover":
            reads.append(
                ContextReadPlan(
                    read_id="session.public_carryover",
                    source_kind="session",
                    source_ref="public_carryover",
                )
            )
        else:
            reads.append(
                ContextReadPlan(
                    read_id=f"task.{field_name}",
                    source_kind="task",
                    source_ref=field_name,
                )
            )
    return tuple(reads)


def _compile_calls_and_deliveries(
    task: TaskEnvelope,
    protocol: HarnessProtocol,
    levels: tuple[tuple[str, ...], ...],
    feedback: HarnessArtifactChannel | None,
) -> tuple[
    tuple[ActorCallPlan, ...],
    tuple[ArtifactDeliveryPlan, ...],
    str,
]:
    actors = {actor.actor_id: actor for actor in protocol.actors}
    initial_call_ids = {
        actor_id: f"actor.{actor_id}.initial" for actor_id in actors
    }
    revision_call_id = (
        f"actor.{protocol.revision.actor_id}.revision"
        if protocol.revision is not None
        else None
    )

    deliveries: list[ArtifactDeliveryPlan] = []
    for channel in protocol.artifact_channels:
        consumer_call_id = initial_call_ids[channel.consumer_actor_id]
        if feedback is not None and channel.channel_id == feedback.channel_id:
            if revision_call_id is None:
                raise AssertionError("feedback channel requires a revision call")
            consumer_call_id = revision_call_id
        deliveries.append(
            ArtifactDeliveryPlan(
                channel_id=channel.channel_id,
                artifact_id=f"artifact.{channel.channel_id}",
                producer_call_id=initial_call_ids[channel.producer_actor_id],
                consumer_call_id=consumer_call_id,
            )
        )
    delivery_by_consumer: dict[str, list[ArtifactDeliveryPlan]] = {}
    for delivery in deliveries:
        delivery_by_consumer.setdefault(delivery.consumer_call_id, []).append(delivery)

    calls: list[ActorCallPlan] = []
    for level in levels:
        for actor_id in level:
            actor = actors[actor_id]
            call_id = initial_call_ids[actor_id]
            context_reads = list(_task_context_reads(actor))
            context_reads.extend(
                ContextReadPlan(
                    read_id=f"artifact.{delivery.channel_id}",
                    source_kind="artifact",
                    source_ref=delivery.artifact_id,
                )
                for delivery in delivery_by_consumer.get(call_id, ())
            )
            writes = tuple(
                ArtifactWritePlan(
                    channel_id=channel.channel_id,
                    artifact_id=f"artifact.{channel.channel_id}",
                    producer_call_id=call_id,
                    payload_kind=channel.payload_kind,
                    max_bytes=task.ceilings.max_artifact_bytes,
                )
                for channel in protocol.artifact_channels
                if channel.producer_actor_id == actor_id
            )
            calls.append(
                ActorCallPlan(
                    call_id=call_id,
                    actor_id=actor_id,
                    call_kind="initial",
                    instruction=actor.instruction,
                    context_reads=tuple(context_reads),
                    artifact_writes=writes,
                    tool_ids=actor.tool_ids,
                    budget_share_bps=actor.budget_share_bps,
                    emits_final_patch=(
                        protocol.revision is None
                        and actor_id == protocol.termination.final_actor_id
                    ),
                )
            )

    if protocol.revision is not None:
        if revision_call_id is None:
            raise AssertionError("revision call id was not constructed")
        actor = actors[protocol.revision.actor_id]
        context_reads = list(_task_context_reads(actor))
        context_reads.extend(
            ContextReadPlan(
                read_id=f"artifact.{delivery.channel_id}",
                source_kind="artifact",
                source_ref=delivery.artifact_id,
            )
            for delivery in deliveries
            if delivery.consumer_call_id == initial_call_ids[actor.actor_id]
        )
        context_reads.append(
            ContextReadPlan(
                read_id=f"prior.{initial_call_ids[actor.actor_id]}",
                source_kind="prior_actor_output",
                source_ref=initial_call_ids[actor.actor_id],
            )
        )
        context_reads.extend(
            ContextReadPlan(
                read_id=f"artifact.{delivery.channel_id}",
                source_kind="artifact",
                source_ref=delivery.artifact_id,
            )
            for delivery in deliveries
            if delivery.consumer_call_id == revision_call_id
        )
        calls.append(
            ActorCallPlan(
                call_id=revision_call_id,
                actor_id=actor.actor_id,
                call_kind="revision",
                instruction=actor.instruction,
                revision_instruction=protocol.revision.instruction,
                context_reads=tuple(context_reads),
                artifact_writes=(),
                tool_ids=actor.tool_ids,
                budget_share_bps=actor.budget_share_bps,
                emits_final_patch=True,
            )
        )
        return tuple(calls), tuple(deliveries), revision_call_id

    return (
        tuple(calls),
        tuple(deliveries),
        initial_call_ids[protocol.termination.final_actor_id],
    )


def _compile_stages(
    protocol: HarnessProtocol,
    levels: tuple[tuple[str, ...], ...],
    phase_by_actor: dict[str, int],
    feedback: HarnessArtifactChannel | None,
) -> tuple[CompositePlanStage, ...]:
    stages: list[CompositePlanStage] = []
    for stage_index, level in enumerate(levels):
        call_ids = tuple(f"actor.{actor_id}.initial" for actor_id in level)
        inbound = tuple(
            channel.channel_id
            for channel in protocol.artifact_channels
            if (feedback is None or channel.channel_id != feedback.channel_id)
            and channel.consumer_actor_id in level
        )
        parent_indices = sorted(
            {
                phase_by_actor[channel.producer_actor_id]
                for channel in protocol.artifact_channels
                if channel.channel_id in inbound
            }
        )
        stages.append(
            CompositePlanStage(
                stage_id=f"stage.{stage_index}",
                stage_index=stage_index,
                call_ids=call_ids,
                depends_on_stage_ids=tuple(
                    f"stage.{index}" for index in parent_indices
                ),
                inbound_channel_ids=inbound,
                fork=len(call_ids) > 1,
                join=(len(parent_indices) > 1 or len(inbound) > 1),
            )
        )
    if feedback is not None and protocol.revision is not None:
        stage_index = len(stages)
        stages.append(
            CompositePlanStage(
                stage_id=f"stage.{stage_index}",
                stage_index=stage_index,
                call_ids=(f"actor.{protocol.revision.actor_id}.revision",),
                depends_on_stage_ids=(
                    f"stage.{phase_by_actor[feedback.producer_actor_id]}",
                ),
                inbound_channel_ids=(feedback.channel_id,),
                revision=True,
            )
        )
    return tuple(stages)


def _compiled_call_ids_for_actor(
    actor_id: str,
    calls: tuple[ActorCallPlan, ...],
) -> tuple[str, ...]:
    return tuple(call.call_id for call in calls if call.actor_id == actor_id)


def _compile_liveness_manifest(
    protocol: HarnessProtocol,
    source_protocol_digest: str,
    calls: tuple[ActorCallPlan, ...],
    deliveries: tuple[ArtifactDeliveryPlan, ...],
) -> ConsumedFieldLivenessManifest:
    bindings: list[ConsumedFieldBinding] = []

    def add(
        source_path: str,
        source_value: Any,
        consumers: tuple[str, ...],
        owners: tuple[str, ...],
    ) -> None:
        if not consumers:
            raise CompositeCompilationError(
                f"mutable field {source_path!r} has no normalized-plan consumer"
            )
        bindings.append(
            ConsumedFieldBinding(
                source_path=source_path,
                source_value_digest=source_field_digest(source_value),
                plan_consumer_paths=consumers,
                runtime_owners=owners,
            )
        )

    for actor in protocol.actors:
        prefix = f"actors[{actor.actor_id}]"
        call_ids = _compiled_call_ids_for_actor(actor.actor_id, calls)
        add(
            f"{prefix}.task_view",
            list(actor.task_view),
            tuple(f"actor_calls[{call_id}].context_reads" for call_id in call_ids),
            ("actor_context",),
        )
        add(
            f"{prefix}.instruction",
            actor.instruction,
            tuple(f"actor_calls[{call_id}].instruction" for call_id in call_ids),
            ("actor_context",),
        )
        add(
            f"{prefix}.tool_ids",
            list(actor.tool_ids),
            tuple(f"actor_calls[{call_id}].tool_ids" for call_id in call_ids),
            ("tool_authority",),
        )
        add(
            f"{prefix}.budget_share_bps",
            actor.budget_share_bps,
            (
                f"budget_ledger.actor_shares[{actor.actor_id}].budget_share_bps",
                *tuple(
                    f"actor_calls[{call_id}].budget_share_bps"
                    for call_id in call_ids
                ),
            ),
            ("budget_ledger",),
        )

    deliveries_by_channel = {
        delivery.channel_id: delivery for delivery in deliveries
    }
    for channel in protocol.artifact_channels:
        delivery = deliveries_by_channel[channel.channel_id]
        prefix = f"artifact_channels[{channel.channel_id}]"
        add(
            f"{prefix}.producer_actor_id",
            channel.producer_actor_id,
            (
                f"artifact_deliveries[{channel.channel_id}].producer_call_id",
                f"actor_calls[{delivery.producer_call_id}].artifact_writes",
            ),
            ("artifact_store", "scheduler"),
        )
        add(
            f"{prefix}.consumer_actor_id",
            channel.consumer_actor_id,
            (
                f"artifact_deliveries[{channel.channel_id}].consumer_call_id",
                f"actor_calls[{delivery.consumer_call_id}].context_reads",
            ),
            ("artifact_store", "actor_context", "scheduler"),
        )

    if protocol.revision is not None:
        revision_call_id = f"actor.{protocol.revision.actor_id}.revision"
        add(
            "revision.actor_id",
            protocol.revision.actor_id,
            (f"actor_calls[{revision_call_id}].actor_id",),
            ("revision_controller", "scheduler"),
        )
        add(
            "revision.feedback_channel_id",
            protocol.revision.feedback_channel_id,
            (
                f"artifact_deliveries[{protocol.revision.feedback_channel_id}].consumer_call_id",
                f"actor_calls[{revision_call_id}].context_reads",
            ),
            ("revision_controller", "artifact_store"),
        )
        add(
            "revision.instruction",
            protocol.revision.instruction,
            (f"actor_calls[{revision_call_id}].revision_instruction",),
            ("revision_controller", "actor_context"),
        )

    final_call = next(call for call in calls if call.emits_final_patch)
    add(
        "termination.final_actor_id",
        protocol.termination.final_actor_id,
        (
            "termination.final_actor_call_id",
            f"actor_calls[{final_call.call_id}].emits_final_patch",
        ),
        ("termination_controller", "scheduler"),
    )

    manifest = ConsumedFieldLivenessManifest(
        source_protocol_digest=source_protocol_digest,
        bindings=tuple(bindings),
    )
    expected_paths = set(protocol.mutable_field_paths())
    actual_paths = {binding.source_path for binding in manifest.bindings}
    if actual_paths != expected_paths:
        raise CompositeCompilationError(
            "consumed-field manifest does not cover the complete mutable surface: "
            f"missing={sorted(expected_paths - actual_paths)!r}, "
            f"extra={sorted(actual_paths - expected_paths)!r}"
        )
    return manifest


def _validate_actor_differentiation(calls: tuple[ActorCallPlan, ...]) -> None:
    initial_calls = [call for call in calls if call.call_kind == "initial"]
    signatures: set[tuple[Any, ...]] = set()
    for call in initial_calls:
        signature = (
            call.instruction,
            tuple((read.source_kind, read.source_ref) for read in call.context_reads),
            tuple(call.tool_ids),
            tuple(write.channel_id for write in call.artifact_writes),
            call.emits_final_patch,
        )
        if signature in signatures:
            raise CompositeCompilationError(
                "actors must have differentiated executable instructions, views, "
                "tools, artifact responsibilities, or termination roles"
            )
        signatures.add(signature)


def compile_composite_run_plan(
    task: TaskEnvelope | Mapping[str, Any],
    protocol: HarnessProtocol | Mapping[str, Any],
    dependency_manifest: RuntimeDependencyManifest | Mapping[str, Any],
    *,
    reject_semantic_digest: str | None = None,
    compiler_metadata: CompositeCompilerMetadata | None = None,
) -> CompositeRunPlan:
    normalized_task = _normalize_task(task)
    normalized_protocol = _normalize_protocol(protocol)
    normalized_dependencies = _normalize_dependencies(dependency_manifest)
    metadata = compiler_metadata or load_composite_compiler_metadata()
    _validate_dependency_contract(
        normalized_task,
        normalized_protocol,
        normalized_dependencies,
        metadata,
    )

    levels, phase_by_actor, feedback = _compile_topology(normalized_protocol)
    calls, deliveries, final_call_id = _compile_calls_and_deliveries(
        normalized_task,
        normalized_protocol,
        levels,
        feedback,
    )
    _validate_actor_differentiation(calls)
    stages = _compile_stages(
        normalized_protocol,
        levels,
        phase_by_actor,
        feedback,
    )
    source_digest = normalized_protocol.source_digest()
    dependency_digest = normalized_dependencies.manifest_digest()
    liveness = _compile_liveness_manifest(
        normalized_protocol,
        source_digest,
        calls,
        deliveries,
    )
    public_verification = PublicVerificationPlan(
        actions=tuple(
            PublicVerificationActionPlan(
                step_id=step.step_id,
                argv=step.argv,
                cwd=step.cwd,
                timeout_ms=step.timeout_ms,
                expected_exit_codes=step.expected_exit_codes,
            )
            for step in normalized_task.public_reproduction
        ),
        run_after_call_id=final_call_id,
    )
    termination = TerminationPlan(
        final_actor_call_id=final_call_id,
        max_patch_bytes=normalized_task.ceilings.max_patch_bytes,
    )
    budget_ledger = BudgetLedgerPlan(
        aggregate_ceiling=normalized_task.ceilings,
        actor_shares=tuple(
            ActorBudgetSharePlan(
                actor_id=actor.actor_id,
                budget_share_bps=actor.budget_share_bps,
            )
            for actor in normalized_protocol.actors
        ),
        scheduled_model_calls=len(calls),
        scheduled_revision_calls=1 if normalized_protocol.revision else 0,
    )

    semantic_payload = {
        "schema_version": "repo-repair-composite-plan-v1",
        "task_envelope_digest": normalized_task.task_manifest_digest,
        "dependency_manifest": normalized_dependencies.model_dump(mode="json"),
        "dependency_manifest_digest": dependency_digest,
        "actor_calls": [call.model_dump(mode="json") for call in calls],
        "stages": [stage.model_dump(mode="json") for stage in stages],
        "artifact_deliveries": [
            delivery.model_dump(mode="json") for delivery in deliveries
        ],
        "public_verification": public_verification.model_dump(mode="json"),
        "termination": termination.model_dump(mode="json"),
        "budget_ledger": budget_ledger.model_dump(mode="json"),
    }
    compiled_digest = composite_plan_digest(semantic_payload)
    if reject_semantic_digest is not None and compiled_digest == reject_semantic_digest:
        raise InertProtocolError(
            "candidate protocol compiles to an exact semantic no-op"
        )

    return CompositeRunPlan(
        task_envelope_digest=normalized_task.task_manifest_digest,
        source_protocol_digest=source_digest,
        compiled_semantic_digest=compiled_digest,
        dependency_manifest=normalized_dependencies,
        dependency_manifest_digest=dependency_digest,
        actor_calls=calls,
        stages=stages,
        artifact_deliveries=deliveries,
        public_verification=public_verification,
        termination=termination,
        budget_ledger=budget_ledger,
        liveness_manifest=liveness,
    )


def assert_semantic_change(
    parent: CompositeRunPlan,
    child: CompositeRunPlan,
) -> None:
    if parent.task_envelope_digest != child.task_envelope_digest:
        raise CompositeCompilationError(
            "semantic-change comparison requires the same TaskEnvelope"
        )
    if parent.dependency_manifest_digest != child.dependency_manifest_digest:
        raise CompositeCompilationError(
            "semantic-change comparison requires the same runtime dependencies"
        )
    if parent.compiled_semantic_digest == child.compiled_semantic_digest:
        raise InertProtocolError(
            "candidate protocol compiles to an exact semantic no-op"
        )


__all__ = [
    "CANONICAL_HARNESS_PROTOCOL_FILE",
    "CANONICAL_TWO_ACTOR_SEED_RESOURCE",
    "COMPOSITE_COMPILER_METADATA_FILE",
    "COMPOSITE_COMPILER_METADATA_RESOURCE",
    "CompositeCompilationError",
    "InertProtocolError",
    "assert_semantic_change",
    "compile_composite_run_plan",
    "load_canonical_harness_seed",
    "load_composite_compiler_metadata",
]
