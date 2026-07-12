from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from ..contracts.epochs import TaskEnvelope
from ..contracts.harness import (
    CompositeRunPlan,
    HarnessActor,
    HarnessArtifactChannel,
    HarnessProtocol,
    RuntimeDependencyManifest,
    source_field_digest,
)
from ..contracts.harness_actions import (
    ActorSplitPatch,
    BudgetShareChange,
    ChannelAddPatch,
    ChannelRewirePatch,
    InstructionRewritePatch,
    RevisionInsertPatch,
    RevisionRemovePatch,
    SemanticTransaction,
    SemanticTransactionProposal,
    TransactionApplicability,
    TransactionReversion,
)
from ..runtime.api.composite_compiler import (
    CompositeCompilationError,
    InertProtocolError,
    compile_composite_run_plan,
)


class SemanticMutationError(ValueError):
    """A semantic transaction is invalid before candidate evaluation."""


class TransactionApplicabilityError(SemanticMutationError):
    """The transaction preconditions do not hold on the named parent."""


class BudgetExpansionError(SemanticMutationError):
    """A transaction changes the frozen aggregate deployment budget."""


class DependencyInvalidTransactionError(SemanticMutationError):
    """A transaction or rollback violates retained lineage dependencies."""


class TransactionReversionError(SemanticMutationError):
    """A stored inverse cannot restore the exact recorded parent."""


@dataclass(frozen=True)
class AppliedSemanticMutation:
    child_protocol: HarnessProtocol
    child_plan: CompositeRunPlan
    transaction: SemanticTransaction


@dataclass(frozen=True)
class RevertedSemanticMutation:
    protocol: HarnessProtocol
    plan: CompositeRunPlan
    reverted_transaction_id: str


_ACTOR_PATH_RE = re.compile(
    r"^actors\[([a-z][a-z0-9_.-]{0,63})\](?:\.(instruction|task_view|tool_ids|budget_share_bps))?$"
)
_CHANNEL_PATH_RE = re.compile(
    r"^artifact_channels\[([a-z][a-z0-9_.-]{0,63})\](?:\.(producer_actor_id|consumer_actor_id))?$"
)


def _normalize_protocol(
    protocol: HarnessProtocol | Mapping[str, Any],
) -> HarnessProtocol:
    try:
        payload = (
            protocol.model_dump(mode="python")
            if isinstance(protocol, HarnessProtocol)
            else dict(protocol)
        )
        return HarnessProtocol.model_validate(payload)
    except (TypeError, ValidationError, ValueError) as exc:
        raise SemanticMutationError(f"invalid parent HarnessProtocol: {exc}") from exc


def _normalize_task(task: TaskEnvelope | Mapping[str, Any]) -> TaskEnvelope:
    try:
        payload = (
            task.model_dump(mode="python")
            if isinstance(task, TaskEnvelope)
            else dict(task)
        )
        return TaskEnvelope.model_validate(payload)
    except (TypeError, ValidationError, ValueError) as exc:
        raise SemanticMutationError(f"invalid TaskEnvelope: {exc}") from exc


def _normalize_dependencies(
    manifest: RuntimeDependencyManifest | Mapping[str, Any],
) -> RuntimeDependencyManifest:
    try:
        payload = (
            manifest.model_dump(mode="python")
            if isinstance(manifest, RuntimeDependencyManifest)
            else dict(manifest)
        )
        return RuntimeDependencyManifest.model_validate(payload)
    except (TypeError, ValidationError, ValueError) as exc:
        raise SemanticMutationError(
            f"invalid RuntimeDependencyManifest: {exc}"
        ) from exc


def _normalize_proposal(
    proposal: SemanticTransactionProposal | Mapping[str, Any],
) -> SemanticTransactionProposal:
    try:
        payload = (
            proposal.model_dump(mode="python")
            if isinstance(proposal, SemanticTransactionProposal)
            else dict(proposal)
        )
        return SemanticTransactionProposal.model_validate(payload)
    except (TypeError, ValidationError, ValueError) as exc:
        raise SemanticMutationError(
            f"invalid SemanticTransactionProposal: {exc}"
        ) from exc


def _source_path_value(protocol: HarnessProtocol, source_path: str) -> Any:
    actor_match = _ACTOR_PATH_RE.fullmatch(source_path)
    if actor_match:
        actor_id, field_name = actor_match.groups()
        actor = next(
            (item for item in protocol.actors if item.actor_id == actor_id),
            None,
        )
        if actor is None:
            raise TransactionApplicabilityError(
                f"source path references missing actor {actor_id!r}"
            )
        if field_name is None:
            return actor.model_dump(mode="json")
        value = getattr(actor, field_name)
        return list(value) if isinstance(value, tuple) else value

    channel_match = _CHANNEL_PATH_RE.fullmatch(source_path)
    if channel_match:
        channel_id, field_name = channel_match.groups()
        channel = next(
            (
                item
                for item in protocol.artifact_channels
                if item.channel_id == channel_id
            ),
            None,
        )
        if channel is None:
            raise TransactionApplicabilityError(
                f"source path references missing channel {channel_id!r}"
            )
        if field_name is None:
            return channel.model_dump(mode="json")
        return getattr(channel, field_name)

    if source_path == "revision":
        if protocol.revision is None:
            raise TransactionApplicabilityError(
                "source path requires a present revision"
            )
        return protocol.revision.model_dump(mode="json")
    if source_path == "termination.final_actor_id":
        return protocol.termination.final_actor_id
    raise TransactionApplicabilityError(
        f"unsupported source-path precondition {source_path!r}"
    )


def _validate_applicability(
    protocol: HarnessProtocol,
    applicability: TransactionApplicability,
) -> None:
    actor_ids = {actor.actor_id for actor in protocol.actors}
    channel_ids = {
        channel.channel_id for channel in protocol.artifact_channels
    }
    missing_actors = set(applicability.required_actor_ids) - actor_ids
    present_forbidden_actors = set(applicability.absent_actor_ids) & actor_ids
    missing_channels = set(applicability.required_channel_ids) - channel_ids
    present_forbidden_channels = (
        set(applicability.absent_channel_ids) & channel_ids
    )
    if missing_actors:
        raise TransactionApplicabilityError(
            f"required actors are absent: {sorted(missing_actors)!r}"
        )
    if present_forbidden_actors:
        raise TransactionApplicabilityError(
            "actors required to be absent are present: "
            f"{sorted(present_forbidden_actors)!r}"
        )
    if missing_channels:
        raise TransactionApplicabilityError(
            f"required channels are absent: {sorted(missing_channels)!r}"
        )
    if present_forbidden_channels:
        raise TransactionApplicabilityError(
            "channels required to be absent are present: "
            f"{sorted(present_forbidden_channels)!r}"
        )
    if applicability.revision_state == "absent" and protocol.revision is not None:
        raise TransactionApplicabilityError("transaction requires no active revision")
    if applicability.revision_state == "present" and protocol.revision is None:
        raise TransactionApplicabilityError("transaction requires an active revision")
    for precondition in applicability.source_path_preconditions:
        actual = source_field_digest(
            _source_path_value(protocol, precondition.source_path)
        )
        if actual != precondition.expected_value_digest:
            raise TransactionApplicabilityError(
                f"source path digest changed at {precondition.source_path!r}"
            )


def _required_applicability(
    proposal: SemanticTransactionProposal,
) -> tuple[set[str], set[str], set[str], set[str], str]:
    patch = proposal.normalized_patch
    if isinstance(patch, ActorSplitPatch):
        return (
            {patch.source_actor_before.actor_id},
            {patch.new_actor.actor_id},
            set(),
            {patch.new_channel.channel_id},
            "any",
        )
    if isinstance(patch, ChannelAddPatch):
        return (
            {patch.channel.producer_actor_id, patch.channel.consumer_actor_id},
            set(),
            set(),
            {patch.channel.channel_id},
            "any",
        )
    if isinstance(patch, ChannelRewirePatch):
        return (
            {
                patch.channel_before.producer_actor_id,
                patch.channel_before.consumer_actor_id,
                patch.channel_after.producer_actor_id,
                patch.channel_after.consumer_actor_id,
            },
            set(),
            {patch.channel_before.channel_id},
            set(),
            "any",
        )
    if isinstance(patch, RevisionInsertPatch):
        return (
            {
                patch.revision.actor_id,
                patch.feedback_channel_before.producer_actor_id,
            },
            set(),
            {patch.feedback_channel_before.channel_id},
            {patch.draft_channel.channel_id},
            "absent",
        )
    if isinstance(patch, RevisionRemovePatch):
        return (
            {
                patch.revision.actor_id,
                patch.feedback_channel.producer_actor_id,
            },
            set(),
            {
                patch.draft_channel.channel_id,
                patch.feedback_channel.channel_id,
            },
            set(),
            "present",
        )
    if isinstance(patch, InstructionRewritePatch):
        return ({patch.actor_id}, set(), set(), set(), "any")
    raise SemanticMutationError(f"unsupported semantic patch {type(patch)!r}")


def _validate_declared_applicability(
    proposal: SemanticTransactionProposal,
) -> None:
    (
        required_actors,
        absent_actors,
        required_channels,
        absent_channels,
        revision_state,
    ) = _required_applicability(proposal)
    declared = proposal.applicability
    if not required_actors <= set(declared.required_actor_ids):
        raise TransactionApplicabilityError(
            "applicability omits operator-required actor references"
        )
    if not absent_actors <= set(declared.absent_actor_ids):
        raise TransactionApplicabilityError(
            "applicability omits operator-required absent actors"
        )
    if not required_channels <= set(declared.required_channel_ids):
        raise TransactionApplicabilityError(
            "applicability omits operator-required channel references"
        )
    if not absent_channels <= set(declared.absent_channel_ids):
        raise TransactionApplicabilityError(
            "applicability omits operator-required absent channels"
        )
    if revision_state != "any" and declared.revision_state != revision_state:
        raise TransactionApplicabilityError(
            f"operator requires applicability.revision_state={revision_state!r}"
        )


def _expected_touched_paths(
    proposal: SemanticTransactionProposal,
) -> tuple[str, ...]:
    patch = proposal.normalized_patch
    if isinstance(patch, ActorSplitPatch):
        return tuple(
            sorted(
                (
                    f"actors[{patch.source_actor_before.actor_id}].budget_share_bps",
                    f"actors[{patch.new_actor.actor_id}]",
                    f"artifact_channels[{patch.new_channel.channel_id}]",
                )
            )
        )
    if isinstance(patch, ChannelAddPatch):
        return (f"artifact_channels[{patch.channel.channel_id}]",)
    if isinstance(patch, ChannelRewirePatch):
        return (f"artifact_channels[{patch.channel_before.channel_id}]",)
    if isinstance(patch, (RevisionInsertPatch, RevisionRemovePatch)):
        draft = patch.draft_channel
        feedback = (
            patch.feedback_channel_before
            if isinstance(patch, RevisionInsertPatch)
            else patch.feedback_channel
        )
        return tuple(
            sorted(
                (
                    "revision",
                    f"artifact_channels[{draft.channel_id}]",
                    f"artifact_channels[{feedback.channel_id}]",
                )
            )
        )
    if isinstance(patch, InstructionRewritePatch):
        return (f"actors[{patch.actor_id}].instruction",)
    raise SemanticMutationError(f"unsupported semantic patch {type(patch)!r}")


def _validate_operator_metadata(proposal: SemanticTransactionProposal) -> None:
    expected_paths = _expected_touched_paths(proposal)
    if proposal.touched_source_paths != expected_paths:
        raise SemanticMutationError(
            "touched_source_paths do not equal the typed patch surface: "
            f"expected {expected_paths!r}"
        )

    owner = proposal.predicted_trace_effect.runtime_owner
    allowed_owners = {
        "actor_split": {"scheduler", "artifact_store", "budget_ledger"},
        "channel_add": {"scheduler", "artifact_store", "actor_context"},
        "channel_rewire": {"scheduler", "artifact_store", "actor_context"},
        "revision_insert": {"revision_controller", "scheduler"},
        "revision_remove": {"revision_controller", "scheduler"},
        "instruction_rewrite": {"actor_context"},
    }[proposal.operator]
    if owner not in allowed_owners:
        raise SemanticMutationError(
            f"runtime owner {owner!r} cannot consume {proposal.operator!r}"
        )
    expected_budget_mode = (
        "shares_rebalanced" if proposal.operator == "actor_split" else "unchanged"
    )
    if proposal.budget_effect.mode != expected_budget_mode:
        raise BudgetExpansionError(
            f"{proposal.operator} requires budget mode {expected_budget_mode!r}"
        )


def _paths_overlap(left: str, right: str) -> bool:
    if left == right:
        return True
    return left.startswith(f"{right}.") or right.startswith(f"{left}.")


def _validate_lineage_dependencies(
    proposal: SemanticTransactionProposal,
    retained_transactions: Sequence[SemanticTransaction],
) -> None:
    ids = [transaction.transaction_id for transaction in retained_transactions]
    if len(ids) != len(set(ids)):
        raise DependencyInvalidTransactionError(
            "retained transaction lineage contains duplicate ids"
        )
    if proposal.transaction_id in set(ids):
        raise DependencyInvalidTransactionError(
            "transaction_id already exists in retained lineage"
        )
    missing = set(proposal.dependency_transaction_ids) - set(ids)
    if missing:
        raise DependencyInvalidTransactionError(
            f"transaction dependencies are not retained: {sorted(missing)!r}"
        )
    declared_dependencies = set(proposal.dependency_transaction_ids)
    for transaction in retained_transactions:
        overlaps = any(
            _paths_overlap(parent_path, child_path)
            for parent_path in transaction.touched_source_paths
            for child_path in proposal.touched_source_paths
        )
        if overlaps and transaction.transaction_id not in declared_dependencies:
            raise DependencyInvalidTransactionError(
                "transaction must depend on retained mutations whose source paths "
                f"it overlaps: {transaction.transaction_id!r}"
            )


def _replace_actor(
    actors: list[dict[str, Any]],
    actor: HarnessActor,
) -> None:
    for index, existing in enumerate(actors):
        if existing["actor_id"] == actor.actor_id:
            actors[index] = actor.model_dump(mode="python")
            return
    raise TransactionApplicabilityError(
        f"patch references missing actor {actor.actor_id!r}"
    )


def _apply_patch(
    parent: HarnessProtocol,
    proposal: SemanticTransactionProposal,
) -> HarnessProtocol:
    patch = proposal.normalized_patch
    payload = parent.model_dump(mode="python")
    actors = list(payload["actors"])
    channels = list(payload["artifact_channels"])

    if isinstance(patch, ActorSplitPatch):
        current = next(
            (
                actor
                for actor in parent.actors
                if actor.actor_id == patch.source_actor_before.actor_id
            ),
            None,
        )
        if current != patch.source_actor_before:
            raise TransactionApplicabilityError(
                "actor split source_actor_before does not match the parent"
            )
        _replace_actor(actors, patch.source_actor_after)
        source_index = next(
            index
            for index, actor in enumerate(actors)
            if actor["actor_id"] == patch.source_actor_after.actor_id
        )
        actors.insert(source_index, patch.new_actor.model_dump(mode="python"))
        channels.append(patch.new_channel.model_dump(mode="python"))
    elif isinstance(patch, ChannelAddPatch):
        channels.append(patch.channel.model_dump(mode="python"))
    elif isinstance(patch, ChannelRewirePatch):
        if (
            parent.revision is not None
            and patch.channel_before.channel_id
            == parent.revision.feedback_channel_id
        ):
            raise TransactionApplicabilityError(
                "revision feedback channels require revision remove/insert operators"
            )
        replaced = False
        for index, channel in enumerate(parent.artifact_channels):
            if channel.channel_id == patch.channel_before.channel_id:
                if channel != patch.channel_before:
                    raise TransactionApplicabilityError(
                        "channel_before does not match the parent route"
                    )
                channels[index] = patch.channel_after.model_dump(mode="python")
                replaced = True
                break
        if not replaced:
            raise TransactionApplicabilityError("rewire channel is absent")
    elif isinstance(patch, RevisionInsertPatch):
        if parent.revision is not None:
            raise TransactionApplicabilityError("protocol already has a revision")
        feedback = next(
            (
                channel
                for channel in parent.artifact_channels
                if channel.channel_id
                == patch.feedback_channel_before.channel_id
            ),
            None,
        )
        if feedback != patch.feedback_channel_before:
            raise TransactionApplicabilityError(
                "feedback_channel_before does not match the parent"
            )
        if patch.revision.actor_id != parent.termination.final_actor_id:
            raise TransactionApplicabilityError(
                "revision insert may not change the final actor"
            )
        channels.append(patch.draft_channel.model_dump(mode="python"))
        payload["revision"] = patch.revision.model_dump(mode="python")
    elif isinstance(patch, RevisionRemovePatch):
        if parent.revision != patch.revision:
            raise TransactionApplicabilityError(
                "revision removal does not match the active revision"
            )
        channel_map = {
            channel.channel_id: channel for channel in parent.artifact_channels
        }
        if channel_map.get(patch.draft_channel.channel_id) != patch.draft_channel:
            raise TransactionApplicabilityError(
                "revision draft channel does not match the parent"
            )
        if channel_map.get(patch.feedback_channel.channel_id) != patch.feedback_channel:
            raise TransactionApplicabilityError(
                "revision feedback channel does not match the parent"
            )
        channels = [
            channel
            for channel in channels
            if channel["channel_id"] != patch.draft_channel.channel_id
        ]
        payload["revision"] = None
    elif isinstance(patch, InstructionRewritePatch):
        current = next(
            (actor for actor in parent.actors if actor.actor_id == patch.actor_id),
            None,
        )
        if current is None:
            raise TransactionApplicabilityError("rewrite actor is absent")
        if current.instruction != patch.before_instruction:
            raise TransactionApplicabilityError(
                "before_instruction does not match the parent"
            )
        updated = current.model_copy(
            update={"instruction": patch.after_instruction},
            deep=True,
        )
        _replace_actor(actors, updated)
    else:
        raise SemanticMutationError(f"unsupported semantic patch {type(patch)!r}")

    payload["actors"] = actors
    payload["artifact_channels"] = channels
    try:
        return HarnessProtocol.model_validate(payload)
    except ValidationError as exc:
        raise SemanticMutationError(
            f"semantic transaction violates HarnessProtocol invariants: {exc}"
        ) from exc


def _actual_budget_changes(
    parent: HarnessProtocol,
    child: HarnessProtocol,
) -> tuple[BudgetShareChange, ...]:
    before = {actor.actor_id: actor.budget_share_bps for actor in parent.actors}
    after = {actor.actor_id: actor.budget_share_bps for actor in child.actors}
    changes = []
    for actor_id in sorted(set(before) | set(after)):
        before_bps = before.get(actor_id, 0)
        after_bps = after.get(actor_id, 0)
        if before_bps != after_bps:
            changes.append(
                BudgetShareChange(
                    actor_id=actor_id,
                    before_bps=before_bps,
                    after_bps=after_bps,
                )
            )
    return tuple(changes)


def _validate_budget_effect(
    parent: HarnessProtocol,
    child: HarnessProtocol,
    proposal: SemanticTransactionProposal,
) -> None:
    if sum(actor.budget_share_bps for actor in parent.actors) != 10_000:
        raise BudgetExpansionError("parent protocol budget is not aggregate-bound")
    if sum(actor.budget_share_bps for actor in child.actors) != 10_000:
        raise BudgetExpansionError("transaction expands or shrinks aggregate budget")
    actual = _actual_budget_changes(parent, child)
    declared = tuple(
        sorted(
            proposal.budget_effect.share_changes,
            key=lambda change: change.actor_id,
        )
    )
    if actual != declared:
        raise BudgetExpansionError(
            "declared budget rebalance does not match the normalized protocol patch"
        )


def _compile_plan(
    task: TaskEnvelope,
    protocol: HarnessProtocol,
    dependencies: RuntimeDependencyManifest,
    *,
    reject_semantic_digest: str | None = None,
) -> CompositeRunPlan:
    try:
        return compile_composite_run_plan(
            task,
            protocol,
            dependencies,
            reject_semantic_digest=reject_semantic_digest,
        )
    except InertProtocolError:
        raise
    except (CompositeCompilationError, ValidationError) as exc:
        raise SemanticMutationError(
            f"transaction child does not compile: {exc}"
        ) from exc


def apply_semantic_transaction(
    parent_protocol: HarnessProtocol | Mapping[str, Any],
    parent_plan: CompositeRunPlan,
    task: TaskEnvelope | Mapping[str, Any],
    dependency_manifest: RuntimeDependencyManifest | Mapping[str, Any],
    proposal: SemanticTransactionProposal | Mapping[str, Any],
    *,
    retained_transactions: Sequence[SemanticTransaction] = (),
) -> AppliedSemanticMutation:
    parent = _normalize_protocol(parent_protocol)
    normalized_task = _normalize_task(task)
    dependencies = _normalize_dependencies(dependency_manifest)
    normalized_proposal = _normalize_proposal(proposal)

    verified_parent_plan = _compile_plan(normalized_task, parent, dependencies)
    if verified_parent_plan.compiled_semantic_digest != parent_plan.compiled_semantic_digest:
        raise TransactionApplicabilityError(
            "provided parent plan is not the authoritative compilation of the parent"
        )
    if normalized_proposal.parent_source_protocol_digest != parent.source_digest():
        raise TransactionApplicabilityError(
            "parent source protocol digest precondition does not match"
        )
    if (
        normalized_proposal.parent_compiled_semantic_digest
        != parent_plan.compiled_semantic_digest
    ):
        raise TransactionApplicabilityError(
            "parent compiled semantic digest precondition does not match"
        )
    if normalized_proposal.task_envelope_digest != normalized_task.task_manifest_digest:
        raise TransactionApplicabilityError(
            "transaction is bound to another TaskEnvelope"
        )
    if (
        normalized_proposal.dependency_manifest_digest
        != dependencies.manifest_digest()
    ):
        raise TransactionApplicabilityError(
            "transaction is bound to another runtime dependency manifest"
        )

    _validate_operator_metadata(normalized_proposal)
    _validate_declared_applicability(normalized_proposal)
    _validate_applicability(parent, normalized_proposal.applicability)
    _validate_lineage_dependencies(normalized_proposal, retained_transactions)

    child = _apply_patch(parent, normalized_proposal)
    _validate_budget_effect(parent, child, normalized_proposal)
    try:
        child_plan = _compile_plan(
            normalized_task,
            child,
            dependencies,
            reject_semantic_digest=parent_plan.compiled_semantic_digest,
        )
    except InertProtocolError as exc:
        raise SemanticMutationError(
            "semantic transaction compiles to an exact no-op"
        ) from exc

    restored_plan = _compile_plan(normalized_task, parent, dependencies)
    if (
        restored_plan.compiled_semantic_digest
        != parent_plan.compiled_semantic_digest
    ):
        raise TransactionReversionError(
            "parent cannot be mechanically recompiled for inverse validation"
        )
    inverse = TransactionReversion(
        expected_child_source_protocol_digest=child.source_digest(),
        expected_child_compiled_semantic_digest=child_plan.compiled_semantic_digest,
        task_envelope_digest=normalized_task.task_manifest_digest,
        dependency_manifest_digest=dependencies.manifest_digest(),
        restored_protocol=parent,
        restored_source_protocol_digest=parent.source_digest(),
        restored_compiled_semantic_digest=parent_plan.compiled_semantic_digest,
        protected_source_paths=normalized_proposal.touched_source_paths,
    )
    transaction = SemanticTransaction.model_validate(
        {
            **normalized_proposal.model_dump(mode="python"),
            "child_source_protocol_digest": child.source_digest(),
            "child_compiled_semantic_digest": child_plan.compiled_semantic_digest,
            "inverse": inverse.model_dump(mode="python"),
        }
    )
    return AppliedSemanticMutation(
        child_protocol=child,
        child_plan=child_plan,
        transaction=transaction,
    )


def _depends_transitively(
    transaction: SemanticTransaction,
    target_id: str,
    by_id: Mapping[str, SemanticTransaction],
    visiting: set[str] | None = None,
) -> bool:
    if target_id in transaction.dependency_transaction_ids:
        return True
    active = set() if visiting is None else set(visiting)
    if transaction.transaction_id in active:
        raise DependencyInvalidTransactionError(
            "retained transaction dependency graph contains a cycle"
        )
    active.add(transaction.transaction_id)
    for dependency_id in transaction.dependency_transaction_ids:
        dependency = by_id.get(dependency_id)
        if dependency is not None and _depends_transitively(
            dependency,
            target_id,
            by_id,
            active,
        ):
            return True
    return False


def _validate_reversion_order(
    transaction: SemanticTransaction,
    active_transactions: Sequence[SemanticTransaction],
) -> None:
    ids = [item.transaction_id for item in active_transactions]
    if len(ids) != len(set(ids)):
        raise DependencyInvalidTransactionError(
            "active transaction lineage contains duplicate ids"
        )
    if transaction.transaction_id not in set(ids):
        raise DependencyInvalidTransactionError(
            "transaction to revert is not active"
        )
    target_index = ids.index(transaction.transaction_id)
    later = active_transactions[target_index + 1 :]
    by_id = {item.transaction_id: item for item in active_transactions}
    protected = transaction.inverse.protected_source_paths
    for descendant in later:
        dependent = _depends_transitively(
            descendant,
            transaction.transaction_id,
            by_id,
        )
        overlaps = any(
            _paths_overlap(parent_path, child_path)
            for parent_path in protected
            for child_path in descendant.touched_source_paths
        )
        if dependent or overlaps:
            raise DependencyInvalidTransactionError(
                "dependent or overlapping descendants must be reverted first: "
                f"{descendant.transaction_id!r}"
            )


def revert_semantic_transaction(
    current_protocol: HarnessProtocol | Mapping[str, Any],
    current_plan: CompositeRunPlan,
    task: TaskEnvelope | Mapping[str, Any],
    dependency_manifest: RuntimeDependencyManifest | Mapping[str, Any],
    transaction: SemanticTransaction,
    *,
    active_transactions: Sequence[SemanticTransaction],
) -> RevertedSemanticMutation:
    current = _normalize_protocol(current_protocol)
    normalized_task = _normalize_task(task)
    dependencies = _normalize_dependencies(dependency_manifest)
    _validate_reversion_order(transaction, active_transactions)

    authoritative_current = _compile_plan(normalized_task, current, dependencies)
    if authoritative_current.compiled_semantic_digest != current_plan.compiled_semantic_digest:
        raise TransactionReversionError(
            "provided current plan is not authoritative for the current protocol"
        )
    inverse = transaction.inverse
    if normalized_task.task_manifest_digest != inverse.task_envelope_digest:
        raise TransactionReversionError("inverse is bound to another task")
    if dependencies.manifest_digest() != inverse.dependency_manifest_digest:
        raise TransactionReversionError(
            "inverse is bound to another runtime dependency manifest"
        )
    if current.source_digest() != inverse.expected_child_source_protocol_digest:
        raise TransactionReversionError(
            "inverse requires the exact recorded child source protocol"
        )
    if (
        current_plan.compiled_semantic_digest
        != inverse.expected_child_compiled_semantic_digest
    ):
        raise TransactionReversionError(
            "inverse requires the exact recorded child compiled semantics"
        )

    restored = _normalize_protocol(inverse.restored_protocol)
    restored_plan = _compile_plan(normalized_task, restored, dependencies)
    if restored.source_digest() != inverse.restored_source_protocol_digest:
        raise TransactionReversionError(
            "stored inverse does not restore its recorded source digest"
        )
    if (
        restored_plan.compiled_semantic_digest
        != inverse.restored_compiled_semantic_digest
    ):
        raise TransactionReversionError(
            "stored inverse does not restore its recorded compiled semantics"
        )
    return RevertedSemanticMutation(
        protocol=restored,
        plan=restored_plan,
        reverted_transaction_id=transaction.transaction_id,
    )


__all__ = [
    "AppliedSemanticMutation",
    "BudgetExpansionError",
    "DependencyInvalidTransactionError",
    "RevertedSemanticMutation",
    "SemanticMutationError",
    "TransactionApplicabilityError",
    "TransactionReversionError",
    "apply_semantic_transaction",
    "revert_semantic_transaction",
]
