from __future__ import annotations

import hashlib
from typing import Any

import pytest
from pydantic import ValidationError

from agintor.contracts.epochs import (
    PublicReproductionStep,
    TaskCeilings,
    TaskEnvelope,
    WorkspaceSnapshotRef,
)
from agintor.contracts.harness import (
    DependencyRef,
    HarnessActor,
    HarnessArtifactChannel,
    HarnessProtocol,
    HarnessRevision,
    RuntimeDependencyManifest,
    TrustedToolDependency,
    source_field_digest,
)
from agintor.contracts.harness_actions import (
    ActorSplitPatch,
    BudgetEffect,
    BudgetShareChange,
    ChannelAddPatch,
    ChannelRewirePatch,
    InstructionRewritePatch,
    PredictedTraceEffect,
    RevisionInsertPatch,
    RevisionRemovePatch,
    SemanticTransactionProposal,
    SourcePathPrecondition,
    TransactionApplicability,
)
from agintor.runtime.api.composite_compiler import (
    compile_composite_run_plan,
    load_canonical_harness_seed,
)
from agintor.search.harness_mutator import (
    BudgetExpansionError,
    DependencyInvalidTransactionError,
    SemanticMutationError,
    TransactionApplicabilityError,
    apply_semantic_transaction,
    revert_semantic_transaction,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _task(*, max_model_calls: int = 8) -> TaskEnvelope:
    return TaskEnvelope(
        task_manifest_id="m1-public-task",
        epoch_id="m1-development",
        epoch_manifest_digest=_digest("m1-epoch"),
        data_state="development",
        split_manifest_digest=_digest("m1-split"),
        issue="Repair the public repository failure without answer-shaped hints.",
        workspace_snapshot=WorkspaceSnapshotRef(
            snapshot_id="m1-snapshot",
            uri="cas://m1-snapshot",
            digest=_digest("m1-snapshot"),
        ),
        public_reproduction=(
            PublicReproductionStep(
                step_id="public-test",
                argv=("python", "-m", "pytest", "-q"),
                timeout_ms=10_000,
            ),
        ),
        ceilings=TaskCeilings(
            max_model_calls=max_model_calls,
            max_input_tokens=20_000,
            max_output_tokens=8_000,
            max_cached_tokens=0,
            max_tool_calls=30,
            max_tool_output_bytes=200_000,
            max_artifact_bytes=40_000,
            max_patch_bytes=20_000,
            max_retries=1,
            max_wall_time_ms=120_000,
            provider_deadline_ms=60_000,
            max_known_cost_usd=1.0,
            max_estimated_cost_usd=2.0,
        ),
    )


def _dependencies(task: TaskEnvelope) -> RuntimeDependencyManifest:
    return RuntimeDependencyManifest(
        compiler=DependencyRef(
            dependency_id="agintor.composite_compiler",
            interface_version="1",
            implementation_digest=_digest("compiler"),
        ),
        harness_contract=DependencyRef(
            dependency_id="agintor.harness_protocol",
            interface_version="repo-repair-harness-v1",
            implementation_digest=_digest("harness"),
        ),
        kernel=DependencyRef(
            dependency_id="agintor.runtime_kernel",
            interface_version="1",
            implementation_digest=_digest("kernel"),
        ),
        trusted_tools=tuple(
            TrustedToolDependency(
                tool_id=tool_id,
                interface_version="1",
                implementation_digest=_digest(f"implementation:{tool_id}"),
                policy_digest=_digest(f"policy:{tool_id}"),
            )
            for tool_id in sorted(task.allowed_capabilities)
        ),
    )


def _parent(
    protocol: HarnessProtocol | None = None,
    *,
    max_model_calls: int = 8,
):
    task = _task(max_model_calls=max_model_calls)
    dependencies = _dependencies(task)
    source = protocol or load_canonical_harness_seed().protocol
    plan = compile_composite_run_plan(task, source, dependencies)
    return source, plan, task, dependencies


def _proposal(
    *,
    transaction_id: str,
    parent: HarnessProtocol,
    parent_plan,
    task: TaskEnvelope,
    dependencies: RuntimeDependencyManifest,
    patch,
    applicability: TransactionApplicability,
    touched_source_paths: tuple[str, ...],
    budget_effect: BudgetEffect,
    runtime_owner: str,
    trace_observation: str,
    dependency_transaction_ids: tuple[str, ...] = (),
) -> SemanticTransactionProposal:
    operator = patch.operator
    return SemanticTransactionProposal(
        transaction_id=transaction_id,
        operator=operator,
        treatment_class=(
            "prompt_only_control"
            if operator == "instruction_rewrite"
            else "structural"
        ),
        proposal_source="manual",
        parent_source_protocol_digest=parent.source_digest(),
        parent_compiled_semantic_digest=parent_plan.compiled_semantic_digest,
        task_envelope_digest=task.task_manifest_digest,
        dependency_manifest_digest=dependencies.manifest_digest(),
        mechanism_hypothesis=f"Test the causal mechanism for {operator}.",
        applicability=applicability,
        normalized_patch=patch,
        touched_source_paths=touched_source_paths,
        budget_effect=budget_effect,
        dependency_transaction_ids=dependency_transaction_ids,
        predicted_trace_effect=PredictedTraceEffect(
            runtime_owner=runtime_owner,
            trace_observation=trace_observation,
            expected_effect=f"The {operator} consumer changes in run evidence.",
        ),
    )


def _instruction_proposal(
    parent: HarnessProtocol,
    parent_plan,
    task: TaskEnvelope,
    dependencies: RuntimeDependencyManifest,
    *,
    transaction_id: str,
    after_instruction: str,
    dependency_transaction_ids: tuple[str, ...] = (),
) -> SemanticTransactionProposal:
    actor = parent.actors[0]
    path = f"actors[{actor.actor_id}].instruction"
    return _proposal(
        transaction_id=transaction_id,
        parent=parent,
        parent_plan=parent_plan,
        task=task,
        dependencies=dependencies,
        patch=InstructionRewritePatch(
            actor_id=actor.actor_id,
            before_instruction=actor.instruction,
            after_instruction=after_instruction,
        ),
        applicability=TransactionApplicability(
            required_actor_ids=(actor.actor_id,),
            source_path_preconditions=(
                SourcePathPrecondition(
                    source_path=path,
                    expected_value_digest=source_field_digest(actor.instruction),
                ),
            ),
        ),
        touched_source_paths=(path,),
        budget_effect=BudgetEffect(mode="unchanged"),
        runtime_owner="actor_context",
        trace_observation="instruction_blocks",
        dependency_transaction_ids=dependency_transaction_ids,
    )


def _split_proposal(parent, parent_plan, task, dependencies):
    source = next(actor for actor in parent.actors if actor.actor_id == "implementer")
    source_after = source.model_copy(update={"budget_share_bps": 4000})
    helper = HarnessActor(
        actor_id="repair-planner",
        task_view=("issue", "public_reproduction", "workspace"),
        instruction="Plan the cross-file repair and deliver implementation constraints.",
        tool_ids=("repo.search", "repo.read", "repo.public_test"),
        budget_share_bps=2000,
    )
    channel = HarnessArtifactChannel(
        channel_id="repair-plan",
        producer_actor_id=helper.actor_id,
        consumer_actor_id=source.actor_id,
    )
    return _proposal(
        transaction_id="txn.actor-split",
        parent=parent,
        parent_plan=parent_plan,
        task=task,
        dependencies=dependencies,
        patch=ActorSplitPatch(
            source_actor_before=source,
            source_actor_after=source_after,
            new_actor=helper,
            new_channel=channel,
        ),
        applicability=TransactionApplicability(
            required_actor_ids=(source.actor_id,),
            absent_actor_ids=(helper.actor_id,),
            absent_channel_ids=(channel.channel_id,),
        ),
        touched_source_paths=(
            f"actors[{source.actor_id}].budget_share_bps",
            f"actors[{helper.actor_id}]",
            f"artifact_channels[{channel.channel_id}]",
        ),
        budget_effect=BudgetEffect(
            mode="shares_rebalanced",
            share_changes=(
                BudgetShareChange(
                    actor_id=source.actor_id,
                    before_bps=6000,
                    after_bps=4000,
                ),
                BudgetShareChange(
                    actor_id=helper.actor_id,
                    before_bps=0,
                    after_bps=2000,
                ),
            ),
        ),
        runtime_owner="scheduler",
        trace_observation="actor_calls",
    )


def _revision_insert_proposal(parent, parent_plan, task, dependencies):
    feedback = next(
        channel
        for channel in parent.artifact_channels
        if channel.channel_id == "investigation"
    )
    draft = HarnessArtifactChannel(
        channel_id="draft-review",
        producer_actor_id="implementer",
        consumer_actor_id="investigator",
    )
    revision = HarnessRevision(
        actor_id="implementer",
        feedback_channel_id=feedback.channel_id,
        instruction="Revise the draft exactly once using the delivered review.",
    )
    return _proposal(
        transaction_id="txn.revision-insert",
        parent=parent,
        parent_plan=parent_plan,
        task=task,
        dependencies=dependencies,
        patch=RevisionInsertPatch(
            draft_channel=draft,
            feedback_channel_before=feedback,
            revision=revision,
        ),
        applicability=TransactionApplicability(
            required_actor_ids=("implementer", "investigator"),
            required_channel_ids=(feedback.channel_id,),
            absent_channel_ids=(draft.channel_id,),
            revision_state="absent",
        ),
        touched_source_paths=(
            "revision",
            f"artifact_channels[{draft.channel_id}]",
            f"artifact_channels[{feedback.channel_id}]",
        ),
        budget_effect=BudgetEffect(mode="unchanged"),
        runtime_owner="revision_controller",
        trace_observation="revision_calls",
    )


def _three_actor_protocol() -> HarnessProtocol:
    return HarnessProtocol.model_validate(
        {
            "actors": [
                {
                    "actor_id": "investigator-a",
                    "task_view": ["issue", "workspace"],
                    "instruction": "Find the primary fault evidence.",
                    "tool_ids": ["repo.search", "repo.read"],
                    "budget_share_bps": 2500,
                },
                {
                    "actor_id": "investigator-b",
                    "task_view": ["issue", "public_reproduction", "workspace"],
                    "instruction": "Find independent reproduction evidence.",
                    "tool_ids": ["repo.read", "repo.public_test"],
                    "budget_share_bps": 2500,
                },
                {
                    "actor_id": "implementer",
                    "task_view": ["issue", "workspace"],
                    "instruction": "Integrate evidence and emit the tested repair.",
                    "tool_ids": [
                        "repo.search",
                        "repo.read",
                        "repo.edit",
                        "repo.diff",
                        "repo.public_test",
                    ],
                    "budget_share_bps": 5000,
                },
            ],
            "artifact_channels": [
                {
                    "channel_id": "finding-a",
                    "producer_actor_id": "investigator-a",
                    "consumer_actor_id": "implementer",
                },
                {
                    "channel_id": "finding-b",
                    "producer_actor_id": "investigator-b",
                    "consumer_actor_id": "implementer",
                },
            ],
            "termination": {"final_actor_id": "implementer"},
        }
    )


def test_instruction_rewrite_is_prompt_only_and_inverse_restores_parent() -> None:
    parent, parent_plan, task, dependencies = _parent()
    proposal = _instruction_proposal(
        parent,
        parent_plan,
        task,
        dependencies,
        transaction_id="txn.prompt-control",
        after_instruction="Localize the fault using public evidence before reporting findings.",
    )

    applied = apply_semantic_transaction(
        parent, parent_plan, task, dependencies, proposal
    )

    assert applied.transaction.treatment_class == "prompt_only_control"
    assert applied.child_plan.compiled_semantic_digest != parent_plan.compiled_semantic_digest
    assert applied.transaction.inverse.restored_protocol == parent
    assert applied.transaction.transaction_record_digest

    reverted = revert_semantic_transaction(
        applied.child_protocol,
        applied.child_plan,
        task,
        dependencies,
        applied.transaction,
        active_transactions=(applied.transaction,),
    )
    assert reverted.protocol == parent
    assert reverted.plan.compiled_semantic_digest == parent_plan.compiled_semantic_digest


def test_instruction_whitespace_noop_and_unknown_patch_fields_fail_schema() -> None:
    parent, _, _, _ = _parent()
    actor = parent.actors[0]
    with pytest.raises(ValidationError, match="no-op"):
        InstructionRewritePatch(
            actor_id=actor.actor_id,
            before_instruction=actor.instruction,
            after_instruction=f"  {actor.instruction}  ",
        )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ChannelAddPatch.model_validate(
            {
                "channel": {
                    "channel_id": "extra",
                    "producer_actor_id": "investigator",
                    "consumer_actor_id": "implementer",
                },
                "ignored": True,
            }
        )


def test_actor_split_rebalances_inside_same_total_and_reverts_exactly() -> None:
    parent, parent_plan, task, dependencies = _parent()
    proposal = _split_proposal(parent, parent_plan, task, dependencies)

    applied = apply_semantic_transaction(
        parent, parent_plan, task, dependencies, proposal
    )

    assert len(applied.child_protocol.actors) == len(parent.actors) + 1
    assert len(applied.child_protocol.artifact_channels) == len(parent.artifact_channels) + 1
    assert sum(actor.budget_share_bps for actor in applied.child_protocol.actors) == 10_000
    assert applied.child_plan.compiled_semantic_digest != parent_plan.compiled_semantic_digest
    reverted = revert_semantic_transaction(
        applied.child_protocol,
        applied.child_plan,
        task,
        dependencies,
        applied.transaction,
        active_transactions=(applied.transaction,),
    )
    assert reverted.protocol == parent


def test_actor_split_rejects_budget_expansion_and_false_rebalance() -> None:
    parent, parent_plan, task, dependencies = _parent()
    source = next(actor for actor in parent.actors if actor.actor_id == "implementer")
    helper = HarnessActor(
        actor_id="helper",
        task_view=("issue", "workspace"),
        instruction="Provide a bounded implementation plan.",
        tool_ids=("repo.search", "repo.read"),
        budget_share_bps=2000,
    )
    with pytest.raises(ValidationError, match="exactly replace"):
        ActorSplitPatch(
            source_actor_before=source,
            source_actor_after=source.model_copy(update={"budget_share_bps": 5000}),
            new_actor=helper,
            new_channel=HarnessArtifactChannel(
                channel_id="helper-plan",
                producer_actor_id="helper",
                consumer_actor_id="implementer",
            ),
        )

    proposal = _split_proposal(parent, parent_plan, task, dependencies)
    bad = proposal.model_copy(
        update={
            "budget_effect": BudgetEffect(
                mode="shares_rebalanced",
                share_changes=(
                    BudgetShareChange(
                        actor_id="implementer",
                        before_bps=6000,
                        after_bps=4500,
                    ),
                    BudgetShareChange(
                        actor_id="repair-planner",
                        before_bps=0,
                        after_bps=1500,
                    ),
                ),
            )
        }
    )
    with pytest.raises(BudgetExpansionError, match="does not match"):
        apply_semantic_transaction(parent, parent_plan, task, dependencies, bad)


def test_channel_add_changes_artifact_semantics_and_reverts() -> None:
    parent, parent_plan, task, dependencies = _parent()
    channel = HarnessArtifactChannel(
        channel_id="second-findings",
        producer_actor_id="investigator",
        consumer_actor_id="implementer",
    )
    proposal = _proposal(
        transaction_id="txn.channel-add",
        parent=parent,
        parent_plan=parent_plan,
        task=task,
        dependencies=dependencies,
        patch=ChannelAddPatch(channel=channel),
        applicability=TransactionApplicability(
            required_actor_ids=("investigator", "implementer"),
            absent_channel_ids=(channel.channel_id,),
        ),
        touched_source_paths=(f"artifact_channels[{channel.channel_id}]",),
        budget_effect=BudgetEffect(mode="unchanged"),
        runtime_owner="artifact_store",
        trace_observation="artifact_deliveries",
    )
    applied = apply_semantic_transaction(parent, parent_plan, task, dependencies, proposal)
    assert len(applied.child_plan.artifact_deliveries) == 2
    assert applied.child_plan.compiled_semantic_digest != parent_plan.compiled_semantic_digest
    reverted = revert_semantic_transaction(
        applied.child_protocol,
        applied.child_plan,
        task,
        dependencies,
        applied.transaction,
        active_transactions=(applied.transaction,),
    )
    assert reverted.protocol == parent


def test_channel_rewire_changes_stage_and_context_route_then_reverts() -> None:
    parent, parent_plan, task, dependencies = _parent(_three_actor_protocol())
    before = next(
        channel for channel in parent.artifact_channels if channel.channel_id == "finding-a"
    )
    after = before.model_copy(update={"consumer_actor_id": "investigator-b"})
    proposal = _proposal(
        transaction_id="txn.channel-rewire",
        parent=parent,
        parent_plan=parent_plan,
        task=task,
        dependencies=dependencies,
        patch=ChannelRewirePatch(channel_before=before, channel_after=after),
        applicability=TransactionApplicability(
            required_actor_ids=("investigator-a", "investigator-b", "implementer"),
            required_channel_ids=(before.channel_id,),
        ),
        touched_source_paths=(f"artifact_channels[{before.channel_id}]",),
        budget_effect=BudgetEffect(mode="unchanged"),
        runtime_owner="scheduler",
        trace_observation="stage_topology",
    )
    applied = apply_semantic_transaction(parent, parent_plan, task, dependencies, proposal)
    assert applied.child_plan.stages != parent_plan.stages
    assert applied.child_plan.compiled_semantic_digest != parent_plan.compiled_semantic_digest
    reverted = revert_semantic_transaction(
        applied.child_protocol,
        applied.child_plan,
        task,
        dependencies,
        applied.transaction,
        active_transactions=(applied.transaction,),
    )
    assert reverted.plan.compiled_semantic_digest == parent_plan.compiled_semantic_digest


def test_channel_mutations_reject_unknown_references_and_cycles() -> None:
    parent, parent_plan, task, dependencies = _parent()
    unknown = HarnessArtifactChannel(
        channel_id="unknown-route",
        producer_actor_id="missing-actor",
        consumer_actor_id="implementer",
    )
    proposal = _proposal(
        transaction_id="txn.unknown-channel",
        parent=parent,
        parent_plan=parent_plan,
        task=task,
        dependencies=dependencies,
        patch=ChannelAddPatch(channel=unknown),
        applicability=TransactionApplicability(
            required_actor_ids=("missing-actor", "implementer"),
            absent_channel_ids=(unknown.channel_id,),
        ),
        touched_source_paths=(f"artifact_channels[{unknown.channel_id}]",),
        budget_effect=BudgetEffect(mode="unchanged"),
        runtime_owner="artifact_store",
        trace_observation="artifact_deliveries",
    )
    with pytest.raises(TransactionApplicabilityError, match="required actors"):
        apply_semantic_transaction(parent, parent_plan, task, dependencies, proposal)

    cycle = HarnessArtifactChannel(
        channel_id="cycle",
        producer_actor_id="implementer",
        consumer_actor_id="investigator",
    )
    cycle_proposal = _proposal(
        transaction_id="txn.cycle",
        parent=parent,
        parent_plan=parent_plan,
        task=task,
        dependencies=dependencies,
        patch=ChannelAddPatch(channel=cycle),
        applicability=TransactionApplicability(
            required_actor_ids=("implementer", "investigator"),
            absent_channel_ids=(cycle.channel_id,),
        ),
        touched_source_paths=(f"artifact_channels[{cycle.channel_id}]",),
        budget_effect=BudgetEffect(mode="unchanged"),
        runtime_owner="scheduler",
        trace_observation="stage_topology",
    )
    with pytest.raises(SemanticMutationError, match="does not compile"):
        apply_semantic_transaction(parent, parent_plan, task, dependencies, cycle_proposal)


def test_revision_insert_remove_each_compile_and_have_valid_inverse() -> None:
    parent, parent_plan, task, dependencies = _parent()
    insert_proposal = _revision_insert_proposal(parent, parent_plan, task, dependencies)
    inserted = apply_semantic_transaction(
        parent, parent_plan, task, dependencies, insert_proposal
    )
    assert inserted.child_protocol.revision is not None
    assert inserted.child_plan.budget_ledger.scheduled_revision_calls == 1
    assert inserted.child_plan.compiled_semantic_digest != parent_plan.compiled_semantic_digest

    draft = next(
        channel
        for channel in inserted.child_protocol.artifact_channels
        if channel.channel_id == "draft-review"
    )
    feedback = next(
        channel
        for channel in inserted.child_protocol.artifact_channels
        if channel.channel_id == "investigation"
    )
    remove_proposal = _proposal(
        transaction_id="txn.revision-remove",
        parent=inserted.child_protocol,
        parent_plan=inserted.child_plan,
        task=task,
        dependencies=dependencies,
        patch=RevisionRemovePatch(
            draft_channel=draft,
            feedback_channel=feedback,
            revision=inserted.child_protocol.revision,
        ),
        applicability=TransactionApplicability(
            required_actor_ids=("implementer", "investigator"),
            required_channel_ids=(draft.channel_id, feedback.channel_id),
            revision_state="present",
        ),
        touched_source_paths=(
            "revision",
            f"artifact_channels[{draft.channel_id}]",
            f"artifact_channels[{feedback.channel_id}]",
        ),
        budget_effect=BudgetEffect(mode="unchanged"),
        runtime_owner="revision_controller",
        trace_observation="revision_calls",
        dependency_transaction_ids=(inserted.transaction.transaction_id,),
    )
    removed = apply_semantic_transaction(
        inserted.child_protocol,
        inserted.child_plan,
        task,
        dependencies,
        remove_proposal,
        retained_transactions=(inserted.transaction,),
    )
    assert removed.child_protocol == parent
    assert removed.child_plan.compiled_semantic_digest == parent_plan.compiled_semantic_digest
    restored_revision = revert_semantic_transaction(
        removed.child_protocol,
        removed.child_plan,
        task,
        dependencies,
        removed.transaction,
        active_transactions=(inserted.transaction, removed.transaction),
    )
    assert restored_revision.protocol == inserted.child_protocol


def test_revision_insert_respects_aggregate_model_call_ceiling() -> None:
    parent, parent_plan, task, dependencies = _parent(max_model_calls=2)
    proposal = _revision_insert_proposal(parent, parent_plan, task, dependencies)
    with pytest.raises(SemanticMutationError, match="max_model_calls"):
        apply_semantic_transaction(parent, parent_plan, task, dependencies, proposal)


def test_parent_identity_and_source_path_preconditions_fail_before_mutation() -> None:
    parent, parent_plan, task, dependencies = _parent()
    proposal = _instruction_proposal(
        parent,
        parent_plan,
        task,
        dependencies,
        transaction_id="txn.bad-precondition",
        after_instruction="Use a changed evidence collection strategy.",
    )
    wrong_parent = proposal.model_copy(
        update={"parent_compiled_semantic_digest": _digest("wrong-parent")}
    )
    with pytest.raises(TransactionApplicabilityError, match="compiled semantic"):
        apply_semantic_transaction(parent, parent_plan, task, dependencies, wrong_parent)

    preconditions = proposal.applicability.model_copy(
        update={
            "source_path_preconditions": (
                SourcePathPrecondition(
                    source_path="actors[investigator].instruction",
                    expected_value_digest=_digest("stale-value"),
                ),
            )
        }
    )
    stale = proposal.model_copy(update={"applicability": preconditions})
    with pytest.raises(TransactionApplicabilityError, match="source path digest"):
        apply_semantic_transaction(parent, parent_plan, task, dependencies, stale)


def test_dependency_invalid_rollback_requires_descendant_first() -> None:
    parent, parent_plan, task, dependencies = _parent()
    first_proposal = _instruction_proposal(
        parent,
        parent_plan,
        task,
        dependencies,
        transaction_id="txn.first-rewrite",
        after_instruction="First retained evidence-oriented instruction.",
    )
    first = apply_semantic_transaction(
        parent, parent_plan, task, dependencies, first_proposal
    )
    second_proposal = _instruction_proposal(
        first.child_protocol,
        first.child_plan,
        task,
        dependencies,
        transaction_id="txn.second-rewrite",
        after_instruction="Second dependent evidence-oriented instruction.",
        dependency_transaction_ids=(first.transaction.transaction_id,),
    )
    second = apply_semantic_transaction(
        first.child_protocol,
        first.child_plan,
        task,
        dependencies,
        second_proposal,
        retained_transactions=(first.transaction,),
    )

    with pytest.raises(DependencyInvalidTransactionError, match="descendants"):
        revert_semantic_transaction(
            second.child_protocol,
            second.child_plan,
            task,
            dependencies,
            first.transaction,
            active_transactions=(first.transaction, second.transaction),
        )

    reverted_second = revert_semantic_transaction(
        second.child_protocol,
        second.child_plan,
        task,
        dependencies,
        second.transaction,
        active_transactions=(first.transaction, second.transaction),
    )
    reverted_first = revert_semantic_transaction(
        reverted_second.protocol,
        reverted_second.plan,
        task,
        dependencies,
        first.transaction,
        active_transactions=(first.transaction,),
    )
    assert reverted_first.protocol == parent
    assert reverted_first.plan.compiled_semantic_digest == parent_plan.compiled_semantic_digest
