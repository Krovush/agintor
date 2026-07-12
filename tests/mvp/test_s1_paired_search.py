from __future__ import annotations

from collections.abc import Callable

import pytest

from agintor.contracts.epochs import (
    DeploymentIdentity,
    EvaluatorAuthority,
    PublicReproductionStep,
    ResearchEpochManifest,
    SearchEnvelope,
    StopRule,
    TaskCeilings,
    TaskEnvelope,
    TrustedToolAuthority,
    WorkspaceSnapshotRef,
    REPO_REPAIR_TRUSTED_TOOL_IDS,
)
from agintor.contracts.harness import (
    DependencyRef,
    HarnessActor,
    HarnessArtifactChannel,
    HarnessProtocol,
    RuntimeDependencyManifest,
    TrustedToolDependency,
)
from agintor.contracts.harness_actions import (
    BudgetEffect,
    ChannelAddPatch,
    InstructionRewritePatch,
    PredictedTraceEffect,
    SemanticTransactionProposal,
    TransactionApplicability,
)
from agintor.contracts.outcomes import (
    DiagnosticScore,
    OutcomeCost,
    OutcomeHealth,
    OutcomeReceipt,
    PairKey,
    pair_key_digest,
)
from agintor.contracts.promotion_proof import (
    EvaluatorOutcomeProofBinding,
    PromotionRunEvidenceProjection,
)
from agintor.core.identity import canonical_identity_digest
from agintor.runtime.api.composite_compiler import (
    compile_composite_run_plan,
    load_canonical_harness_seed,
)
from agintor.search.harness_mutator import apply_semantic_transaction
from agintor.search.paired_harness import (
    FrozenControlArm,
    HarnessEvaluationRequest,
    LiveSearchAuthorization,
    PairedHarnessSearchConfig,
    PairedSearchIntegrityError,
    ProposalBatchRequest,
    canonical_pair_keys,
    load_paired_harness_search_result,
    paired_task_panel_digest,
    run_paired_harness_search,
    write_paired_harness_search_result,
)


def _digest(label: str) -> str:
    return canonical_identity_digest(label, domain="test-s1")


def _ceilings() -> TaskCeilings:
    return TaskCeilings(
        max_model_calls=8,
        max_input_tokens=20_000,
        max_output_tokens=8_000,
        max_cached_tokens=10_000,
        max_tool_calls=30,
        max_tool_output_bytes=100_000,
        max_artifact_bytes=100_000,
        max_patch_bytes=30_000,
        max_retries=2,
        max_wall_time_ms=120_000,
        provider_deadline_ms=30_000,
        max_known_cost_usd=5.0,
        max_estimated_cost_usd=6.0,
    )


def _pair_keys(provider_config_digest: str) -> tuple[PairKey, ...]:
    return canonical_pair_keys(
        (
            PairKey(
                task_manifest_id="task.search.1",
                environment_id="environment.search.1",
                sampling_replicate=0,
                provider_config_digest=provider_config_digest,
            ),
            PairKey(
                task_manifest_id="task.search.1",
                environment_id="environment.search.1",
                sampling_replicate=1,
                provider_config_digest=provider_config_digest,
            ),
        )
    )


def _epoch(
    *,
    max_candidate_evaluations: int = 2,
    max_steps: int = 2,
    offspring_per_step: int = 1,
    max_nonimproving: int = 4,
) -> tuple[ResearchEpochManifest, tuple[PairKey, ...]]:
    provider_digest = _digest("provider")
    pair_keys = _pair_keys(provider_digest)
    epoch = ResearchEpochManifest(
        epoch_id="epoch.search.1",
        task_manifest_digest=_digest("task-distribution"),
        development_split_digest=_digest("development-split"),
        sealed_confirmation_split_digest=_digest("sealed-split"),
        deployment=DeploymentIdentity(
            deployment_id="scripted.fixed.repair",
            provider="scripted",
            model="fixed-offline-model",
            provider_config_digest=provider_digest,
            decoding_policy_digest=_digest("decoding"),
            price_schedule_digest=_digest("prices"),
            command_container_policy_digest=_digest("command-container-policy"),
        ),
        per_run_ceilings=_ceilings(),
        search_envelope=SearchEnvelope(
            max_steps=max_steps,
            offspring_per_step=offspring_per_step,
            sampling_replicates=2,
            task_panel_digest=paired_task_panel_digest(pair_keys),
        ),
        trusted_tools=tuple(
            TrustedToolAuthority(
                tool_id=tool_id,
                implementation_digest=_digest(f"tool:{tool_id}"),
                policy_digest=_digest(f"policy:{tool_id}"),
            )
            for tool_id in REPO_REPAIR_TRUSTED_TOOL_IDS
        ),
        stop_rule=StopRule(
            max_candidate_evaluations=max_candidate_evaluations,
            max_consecutive_non_improving_steps=max_nonimproving,
        ),
        evaluator_authority=EvaluatorAuthority(
            evaluator_id="evaluator.search.v1",
            evaluator_identity_digest=_digest("evaluator"),
            evaluation_policy_digest=_digest("evaluation-policy"),
        ),
    )
    return epoch, pair_keys


def _task(epoch: ResearchEpochManifest) -> TaskEnvelope:
    return TaskEnvelope(
        task_manifest_id="task.search.1",
        epoch_id=epoch.epoch_id,
        epoch_manifest_digest=epoch.epoch_manifest_digest,
        data_state="development",
        split_manifest_digest=epoch.development_split_digest,
        issue="Repair the public parser regression without target-file hints.",
        workspace_snapshot=WorkspaceSnapshotRef(
            snapshot_id="snapshot.search.1",
            uri="fixtures/public/search-clean",
            digest=_digest("workspace"),
        ),
        public_reproduction=(
            PublicReproductionStep(
                step_id="public-test",
                argv=("python", "-m", "pytest", "tests/test_parser.py"),
                timeout_ms=30_000,
            ),
        ),
        ceilings=_ceilings(),
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
                policy_digest=_digest(f"runtime-policy:{tool_id}"),
            )
            for tool_id in sorted(task.allowed_capabilities)
        ),
    )


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
    proposal_source: str = "manual",
) -> SemanticTransactionProposal:
    return SemanticTransactionProposal(
        transaction_id=transaction_id,
        operator=patch.operator,
        treatment_class=(
            "prompt_only_control"
            if patch.operator == "instruction_rewrite"
            else "structural"
        ),
        proposal_source=proposal_source,
        parent_source_protocol_digest=parent.source_digest(),
        parent_compiled_semantic_digest=parent_plan.compiled_semantic_digest,
        task_envelope_digest=task.task_manifest_digest,
        dependency_manifest_digest=dependencies.manifest_digest(),
        mechanism_hypothesis=f"Exercise {patch.operator} through its live consumer.",
        applicability=applicability,
        normalized_patch=patch,
        touched_source_paths=touched_source_paths,
        budget_effect=BudgetEffect(mode="unchanged"),
        predicted_trace_effect=PredictedTraceEffect(
            runtime_owner=(
                "actor_context"
                if patch.operator == "instruction_rewrite"
                else "artifact_store"
            ),
            trace_observation=(
                "instruction_blocks"
                if patch.operator == "instruction_rewrite"
                else "artifact_deliveries"
            ),
            expected_effect="The named runtime consumer changes in evidence.",
        ),
    )


def _channel_proposal(
    request: ProposalBatchRequest,
    *,
    name: str,
    index: int,
) -> SemanticTransactionProposal:
    channel_id = f"{name}-{request.step_index}-{index}"
    channel = HarnessArtifactChannel(
        channel_id=channel_id,
        producer_actor_id="investigator",
        consumer_actor_id="implementer",
    )
    return _proposal(
        transaction_id=f"txn.{channel_id}",
        parent=request.incumbent_protocol,
        parent_plan=request.incumbent_anchor_plan,
        task=request.anchor_task,
        dependencies=request.dependency_manifest,
        patch=ChannelAddPatch(channel=channel),
        applicability=TransactionApplicability(
            required_actor_ids=("investigator", "implementer"),
            absent_channel_ids=(channel_id,),
        ),
        touched_source_paths=(f"artifact_channels[{channel_id}]",),
        proposal_source="matched_random",
    )


def _controls(
    parent: HarnessProtocol,
    task: TaskEnvelope,
    dependencies: RuntimeDependencyManifest,
) -> tuple[FrozenControlArm, ...]:
    parent_plan = compile_composite_run_plan(task, parent, dependencies)
    actor = parent.actors[0]
    prompt_proposal = _proposal(
        transaction_id="txn.control-prompt",
        parent=parent,
        parent_plan=parent_plan,
        task=task,
        dependencies=dependencies,
        patch=InstructionRewritePatch(
            actor_id=actor.actor_id,
            before_instruction=actor.instruction,
            after_instruction="Use a frozen prompt-only evidence localization instruction.",
        ),
        applicability=TransactionApplicability(required_actor_ids=(actor.actor_id,)),
        touched_source_paths=(f"actors[{actor.actor_id}].instruction",),
    )
    prompt = apply_semantic_transaction(
        parent, parent_plan, task, dependencies, prompt_proposal
    )

    channel = HarnessArtifactChannel(
        channel_id="matched-random-control",
        producer_actor_id="investigator",
        consumer_actor_id="implementer",
    )
    random_proposal = _proposal(
        transaction_id="txn.control-random",
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
        proposal_source="matched_random",
    )
    random = apply_semantic_transaction(
        parent, parent_plan, task, dependencies, random_proposal
    )

    base_actor = next(actor for actor in parent.actors if actor.actor_id == "implementer")
    single_actor = base_actor.model_copy(update={"budget_share_bps": 10_000})
    single = HarnessProtocol(
        actors=(single_actor,),
        termination={"final_actor_id": single_actor.actor_id},
    )
    return (
        FrozenControlArm(
            control_id="control.single",
            control_kind="equal_envelope_single_actor",
            protocol=single,
        ),
        FrozenControlArm(
            control_id="control.repeated-single",
            control_kind="repeated_single_actor_fixed_selector",
            protocol=single,
            fixed_selector_id="selector.public-complete-repair-v1",
        ),
        FrozenControlArm(
            control_id="control.static",
            control_kind="static_localization_repair_validation",
            protocol=parent,
        ),
        FrozenControlArm(
            control_id="control.founding",
            control_kind="founding_parent",
            protocol=parent,
        ),
        FrozenControlArm(
            control_id="control.prompt",
            control_kind="prompt_only",
            protocol=prompt.child_protocol,
            origin_transaction=prompt.transaction,
        ),
        FrozenControlArm(
            control_id="control.random",
            control_kind="matched_random_semantic",
            protocol=random.child_protocol,
            origin_transaction=random.transaction,
        ),
    )


def _config(
    *,
    pair_keys: tuple[PairKey, ...],
    controls: tuple[FrozenControlArm, ...],
    mode: str = "offline_scripted",
    opportunities: int = 1,
    epoch: ResearchEpochManifest | None = None,
) -> PairedHarnessSearchConfig:
    live_authorization = None
    if mode == "live_provider":
        if epoch is None:
            raise ValueError("live test config requires epoch")
        live_authorization = LiveSearchAuthorization(
            authorization_id="live-search.test",
            search_id="search.scripted.1",
            epoch_id=epoch.epoch_id,
            epoch_manifest_digest=epoch.epoch_manifest_digest,
            deployment_profile_digest=_digest("candidate-profile"),
            provider_config_digest=epoch.deployment.provider_config_digest,
            authorized_by="test-suite",
        )
    return PairedHarnessSearchConfig(
        search_id="search.scripted.1",
        execution_mode=mode,
        expected_pair_keys=pair_keys,
        deployment_profile_digest=_digest("candidate-profile"),
        live_authorization=live_authorization,
        controls=controls,
        control_opportunities_per_arm=opportunities,
    )


def _health(*, healthy: bool = True) -> OutcomeHealth:
    return OutcomeHealth(
        process_integrity=healthy,
        no_leakage=healthy,
        environment_integrity=healthy,
        evaluator_integrity=healthy,
        accounting_complete=healthy,
    )


def _cost(*, known_cost: float = 0.2, wall_time_ms: int = 5_000) -> OutcomeCost:
    return OutcomeCost(
        model_calls=2,
        input_tokens=1_000,
        output_tokens=500,
        cached_tokens=0,
        tool_calls=4,
        tool_output_bytes=2_000,
        artifact_bytes=1_000,
        patch_bytes=500,
        retries=0,
        wall_time_ms=wall_time_ms,
        known_cost_usd=known_cost,
        estimated_cost_usd=0.0,
        unknown_dollars=False,
        within_epoch_envelope=True,
    )


def _receipt(
    *,
    request: HarnessEvaluationRequest,
    pair_key: PairKey,
    epoch: ResearchEpochManifest,
    task: TaskEnvelope,
    dependencies: RuntimeDependencyManifest,
    complete_repair: bool,
    known_cost: float = 0.2,
    environment_digest: str | None = None,
    healthy: bool = True,
    live: bool = False,
) -> OutcomeReceipt:
    return OutcomeReceipt(
        receipt_id=(
            f"receipt.{request.evaluation_id}.{pair_key.sampling_replicate}"
        ),
        execution_mode="live_provider" if live else "deterministic_replay",
        live_inference_status="completed" if live else "not_run",
        real_inference_requests_sent=2 if live else 0,
        data_state="development",
        epoch_id=epoch.epoch_id,
        epoch_manifest_digest=epoch.epoch_manifest_digest,
        release_digest=_digest("candidate-release"),
        release_manifest_digest=_digest("candidate-release-manifest"),
        profile_digest=request.deployment_profile_digest,
        split_manifest_digest=epoch.development_split_digest,
        task_manifest_id=task.task_manifest_id,
        task_manifest_digest=task.task_manifest_digest,
        evaluation_contract_id="evaluation.search.1",
        evaluation_contract_digest=_digest("evaluation-contract"),
        evaluator_id=epoch.evaluator_authority.evaluator_id,
        evaluator_identity_digest=epoch.evaluator_authority.evaluator_identity_digest,
        evaluation_policy_digest=epoch.evaluator_authority.evaluation_policy_digest,
        pair_key=pair_key,
        protocol_digest=request.protocol.source_digest(),
        compiler_digest=dependencies.compiler.implementation_digest,
        kernel_digest=dependencies.kernel.implementation_digest,
        tool_manifest_digest=dependencies.manifest_digest(),
        provider_config_digest=epoch.deployment.provider_config_digest,
        decoding_policy_digest=epoch.deployment.decoding_policy_digest,
        price_schedule_digest=epoch.deployment.price_schedule_digest,
        command_container_policy_digest=(
            epoch.deployment.command_container_policy_digest
        ),
        evaluator_environment_digest=(
            environment_digest or _digest(pair_key.environment_id)
        ),
        patch_digest=_digest(
            f"patch:{request.evaluation_id}:{pair_key.sampling_replicate}"
        ),
        complete_repair=complete_repair,
        health=_health(healthy=healthy),
        cost=_cost(known_cost=known_cost),
        diagnostics=(DiagnosticScore(name="ignored_trace_score", value=999.0),),
        issued_at_ms=1,
    )


def _proof_binding(
    *,
    request: HarnessEvaluationRequest,
    receipt: OutcomeReceipt,
    epoch: ResearchEpochManifest,
    dependencies: RuntimeDependencyManifest,
) -> EvaluatorOutcomeProofBinding:
    plan = next(
        item.plan
        for item in request.compiled_plans
        if item.task_manifest_id == receipt.task_manifest_id
    )
    evidence_digest = _digest(f"run-evidence:{receipt.receipt_id}")
    run = PromotionRunEvidenceProjection(
        evidence_id=f"evidence.{receipt.receipt_id}",
        evidence_digest=evidence_digest,
        run_id=f"run.{receipt.receipt_id}",
        execution_mode=receipt.execution_mode,
        live_inference_status=receipt.live_inference_status,
        real_inference_requests_sent=receipt.real_inference_requests_sent,
        arm="intact",
        capability_epoch=receipt.capability_epoch,
        data_state=receipt.data_state,
        epoch_id=receipt.epoch_id,
        epoch_manifest_digest=receipt.epoch_manifest_digest,
        release_digest=receipt.release_digest,
        release_manifest_digest=receipt.release_manifest_digest,
        profile_digest=receipt.profile_digest,
        split_manifest_digest=receipt.split_manifest_digest,
        pair_key=receipt.pair_key,
        task_manifest_digest=receipt.task_manifest_digest,
        protocol_digest=receipt.protocol_digest,
        compiled_semantic_digest=plan.compiled_semantic_digest,
        dependency_manifest_digest=plan.dependency_manifest_digest,
        compiler_digest=receipt.compiler_digest,
        kernel_digest=receipt.kernel_digest,
        tool_manifest_digest=receipt.tool_manifest_digest,
        provider_config_digest=receipt.provider_config_digest,
        decoding_policy_digest=receipt.decoding_policy_digest,
        price_schedule_digest=receipt.price_schedule_digest,
        command_container_policy_digest=receipt.command_container_policy_digest,
        deployment_id=epoch.deployment.deployment_id,
        provider=epoch.deployment.provider,
        model=epoch.deployment.model,
        cost_ledger_digest=_digest(f"cost-ledger:{receipt.receipt_id}"),
        runtime_environment_digest=_digest(
            f"runtime-environment:{receipt.pair_key.environment_id}"
        ),
        patch_digest=receipt.patch_digest,
        healthy=receipt.health.passes_promotion_floor,
    )
    return EvaluatorOutcomeProofBinding(
        outcome_receipt=receipt,
        proof_record_id=f"proof.{receipt.receipt_id}",
        proof_record_digest=_digest(f"proof-record:{receipt.receipt_id}"),
        run_evidence=run,
        run_evidence_digest=evidence_digest,
        proof_record_ref=(
            f"runs/{pair_key_digest(receipt.pair_key)}/"
            f"{receipt.protocol_digest}/{evidence_digest}.json"
        ),
        outcome_link_ref=f"outcome_links/{receipt.receipt_digest}.json",
    )


def _scripted_evaluator(
    *,
    epoch: ResearchEpochManifest,
    task: TaskEnvelope,
    dependencies: RuntimeDependencyManifest,
    child_mode: str = "improve",
    founding_mode: str = "mixed",
    calls: list[HarnessEvaluationRequest] | None = None,
    live: bool = False,
) -> Callable[[HarnessEvaluationRequest], tuple[EvaluatorOutcomeProofBinding, ...]]:
    def evaluate(
        request: HarnessEvaluationRequest,
    ) -> tuple[EvaluatorOutcomeProofBinding, ...]:
        if calls is not None:
            calls.append(request)
        proof_bindings = []
        for pair_key in request.expected_pair_keys:
            if request.arm_kind == "search_parent":
                complete = (
                    True
                    if founding_mode == "saturated"
                    else False
                    if founding_mode == "uniform_failure"
                    else pair_key.sampling_replicate == 0
                )
            elif request.arm_kind == "control":
                complete = pair_key.sampling_replicate == 0
            elif child_mode == "improve":
                complete = any(
                    channel.channel_id.startswith("gain-")
                    for channel in request.protocol.artifact_channels
                ) or pair_key.sampling_replicate == 0
            elif child_mode == "fail_bad_promote_gain":
                if any(
                    channel.channel_id.startswith("bad-")
                    for channel in request.protocol.artifact_channels
                ):
                    complete = False
                else:
                    complete = True
            else:
                complete = pair_key.sampling_replicate == 0
            receipt = _receipt(
                    request=request,
                    pair_key=pair_key,
                    epoch=epoch,
                    task=task,
                    dependencies=dependencies,
                    complete_repair=complete,
                    live=live,
                )
            proof_bindings.append(
                _proof_binding(
                    request=request,
                    receipt=receipt,
                    epoch=epoch,
                    dependencies=dependencies,
                )
            )
        return tuple(proof_bindings)

    return evaluate


def test_improving_non_prompt_child_is_promoted_only_with_paired_authority() -> None:
    epoch, pair_keys = _epoch(max_candidate_evaluations=1, max_steps=1)
    task = _task(epoch)
    dependencies = _dependencies(task)
    parent = load_canonical_harness_seed().protocol
    controls = _controls(parent, task, dependencies)

    def proposals(request: ProposalBatchRequest):
        return (_channel_proposal(request, name="gain", index=0),)

    result = run_paired_harness_search(
        epoch=epoch,
        tasks=(task,),
        dependency_manifest=dependencies,
        founding_protocol=parent,
        config=_config(pair_keys=pair_keys, controls=controls),
        proposal_callback=proposals,
        evaluator_callback=_scripted_evaluator(
            epoch=epoch,
            task=task,
            dependencies=dependencies,
        ),
    )

    assert result.retained_children == 1
    assert result.final_protocol.source_digest() != parent.source_digest()
    promoted = next(record for record in result.candidate_lineage if record.status == "promoted")
    assert promoted.transaction is not None
    assert promoted.transaction.treatment_class == "structural"
    assert promoted.joined_panel is not None
    assert promoted.promotion_authorization is not None
    assert result.selection_decisions[0].diagnostics_used_for_selection is False


def test_failed_child_is_preserved_and_cannot_contaminate_batch_leader() -> None:
    epoch, pair_keys = _epoch(
        max_candidate_evaluations=2,
        max_steps=1,
        offspring_per_step=2,
    )
    task = _task(epoch)
    dependencies = _dependencies(task)
    parent = load_canonical_harness_seed().protocol
    controls = _controls(parent, task, dependencies)

    def proposals(request: ProposalBatchRequest):
        return (
            _channel_proposal(request, name="bad", index=0),
            _channel_proposal(request, name="gain", index=1),
        )

    result = run_paired_harness_search(
        epoch=epoch,
        tasks=(task,),
        dependency_manifest=dependencies,
        founding_protocol=parent,
        config=_config(pair_keys=pair_keys, controls=controls),
        proposal_callback=proposals,
        evaluator_callback=_scripted_evaluator(
            epoch=epoch,
            task=task,
            dependencies=dependencies,
            child_mode="fail_bad_promote_gain",
        ),
    )

    rejected = next(
        record
        for record in result.candidate_lineage
        if record.proposal.transaction_id.startswith("txn.bad-")
    )
    assert rejected.status == "outcome_rejected"
    assert rejected.protocol is not None
    assert rejected.child_proof_bindings
    assert result.final_candidate_id != rejected.candidate_id
    assert all(
        transaction.transaction_id != rejected.proposal.transaction_id
        for transaction in result.retained_transactions
    )


@pytest.mark.parametrize("failure_mode", ["missing", "duplicate", "mismatched"])
def test_missing_duplicate_and_mismatched_pairs_fail_closed(failure_mode: str) -> None:
    epoch, pair_keys = _epoch(max_candidate_evaluations=1, max_steps=1)
    task = _task(epoch)
    dependencies = _dependencies(task)
    parent = load_canonical_harness_seed().protocol
    controls = _controls(parent, task, dependencies)
    healthy = _scripted_evaluator(
        epoch=epoch,
        task=task,
        dependencies=dependencies,
    )

    def evaluator(request: HarnessEvaluationRequest):
        proof_bindings = list(healthy(request))
        if request.arm_kind != "search_child":
            return tuple(proof_bindings)
        if failure_mode == "missing":
            return tuple(proof_bindings[:-1])
        if failure_mode == "duplicate":
            return (proof_bindings[0], proof_bindings[0])
        crossed_run = proof_bindings[0].run_evidence.model_copy(
            update={"compiled_semantic_digest": _digest("crossed-compiled-plan")}
        )
        proof_bindings[0] = proof_bindings[0].model_copy(
            update={"run_evidence": crossed_run}
        )
        return tuple(proof_bindings)

    with pytest.raises(PairedSearchIntegrityError):
        run_paired_harness_search(
            epoch=epoch,
            tasks=(task,),
            dependency_manifest=dependencies,
            founding_protocol=parent,
            config=_config(pair_keys=pair_keys, controls=controls),
            proposal_callback=lambda request: (
                _channel_proposal(request, name="gain", index=0),
            ),
            evaluator_callback=evaluator,
        )


def test_every_frozen_control_receives_equal_serial_opportunities() -> None:
    epoch, pair_keys = _epoch(max_candidate_evaluations=1, max_steps=1)
    task = _task(epoch)
    dependencies = _dependencies(task)
    parent = load_canonical_harness_seed().protocol
    controls = _controls(parent, task, dependencies)
    calls: list[HarnessEvaluationRequest] = []
    result = run_paired_harness_search(
        epoch=epoch,
        tasks=(task,),
        dependency_manifest=dependencies,
        founding_protocol=parent,
        config=_config(
            pair_keys=pair_keys,
            controls=controls,
            opportunities=2,
        ),
        proposal_callback=lambda request: (
            _channel_proposal(request, name="gain", index=0),
        ),
        evaluator_callback=_scripted_evaluator(
            epoch=epoch,
            task=task,
            dependencies=dependencies,
            calls=calls,
        ),
    )
    assert len(result.control_opportunities) == 12
    assert all(record.status == "completed" for record in result.control_opportunities)
    counts = {
        control.control_id: sum(
            record.control_id == control.control_id
            for record in result.control_opportunities
        )
        for control in controls
    }
    assert set(counts.values()) == {2}
    control_calls = [call for call in calls if call.arm_kind == "control"]
    assert len(control_calls) == 12
    assert all(call.execution_mode == "offline_scripted" for call in control_calls)
    assert all(call.live_authorization_digest is None for call in control_calls)


def test_search_stops_at_exact_frozen_candidate_budget() -> None:
    epoch, pair_keys = _epoch(
        max_candidate_evaluations=4,
        max_steps=2,
        offspring_per_step=2,
        max_nonimproving=4,
    )
    task = _task(epoch)
    dependencies = _dependencies(task)
    parent = load_canonical_harness_seed().protocol
    controls = _controls(parent, task, dependencies)

    def proposals(request: ProposalBatchRequest):
        return tuple(
            _channel_proposal(request, name="neutral", index=index)
            for index in range(request.requested_offspring)
        )

    result = run_paired_harness_search(
        epoch=epoch,
        tasks=(task,),
        dependency_manifest=dependencies,
        founding_protocol=parent,
        config=_config(pair_keys=pair_keys, controls=controls),
        proposal_callback=proposals,
        evaluator_callback=_scripted_evaluator(
            epoch=epoch,
            task=task,
            dependencies=dependencies,
            child_mode="equivalent",
        ),
    )
    assert result.candidate_opportunities_used == 4
    assert result.evaluated_children == 4
    assert len(result.candidate_lineage) == 4
    assert result.retained_children == 0
    assert result.final_status.feasibility_status == "no_outcome_improving_descendant"


def test_live_search_requires_authorization_and_live_proof_provenance() -> None:
    epoch, pair_keys = _epoch(max_candidate_evaluations=1, max_steps=1)
    task = _task(epoch)
    dependencies = _dependencies(task)
    parent = load_canonical_harness_seed().protocol
    controls = _controls(parent, task, dependencies)

    with pytest.raises(ValueError, match="explicit live search authorization"):
        PairedHarnessSearchConfig(
            search_id="search.scripted.1",
            execution_mode="live_provider",
            expected_pair_keys=pair_keys,
            deployment_profile_digest=_digest("candidate-profile"),
            controls=controls,
            control_opportunities_per_arm=1,
        )

    requests: list[HarnessEvaluationRequest] = []
    result = run_paired_harness_search(
        epoch=epoch,
        tasks=(task,),
        dependency_manifest=dependencies,
        founding_protocol=parent,
        config=_config(
            pair_keys=pair_keys,
            controls=controls,
            mode="live_provider",
            epoch=epoch,
        ),
        proposal_callback=lambda request: (
            _channel_proposal(request, name="gain", index=0),
        ),
        evaluator_callback=_scripted_evaluator(
            epoch=epoch,
            task=task,
            dependencies=dependencies,
            calls=requests,
            live=True,
        ),
    )
    assert result.capability_promotion_authorized is True
    assert result.final_status.live_inference_status == "completed"
    assert result.final_status.inference_requests_sent > 0
    assert all(request.execution_mode == "live_provider" for request in requests)
    assert all(request.live_authorization_digest for request in requests)

    with pytest.raises(PairedSearchIntegrityError, match="completed live-provider proof"):
        run_paired_harness_search(
            epoch=epoch,
            tasks=(task,),
            dependency_manifest=dependencies,
            founding_protocol=parent,
            config=_config(
                pair_keys=pair_keys,
                controls=controls,
                mode="live_provider",
                epoch=epoch,
            ),
            proposal_callback=lambda request: (
                _channel_proposal(request, name="gain", index=0),
            ),
            evaluator_callback=_scripted_evaluator(
                epoch=epoch,
                task=task,
                dependencies=dependencies,
                live=False,
            ),
        )


@pytest.mark.parametrize(
    ("founding_mode", "expected_status"),
    [
        ("saturated", "no_headroom_saturated"),
        ("uniform_failure", "no_headroom_uniform_failure"),
    ],
)
def test_no_headroom_stops_before_controls_or_candidate_proposals(
    founding_mode: str,
    expected_status: str,
) -> None:
    epoch, pair_keys = _epoch(max_candidate_evaluations=1, max_steps=1)
    task = _task(epoch)
    dependencies = _dependencies(task)
    parent = load_canonical_harness_seed().protocol
    controls = _controls(parent, task, dependencies)
    proposal_called = False

    def proposals(request: ProposalBatchRequest):
        nonlocal proposal_called
        proposal_called = True
        return (_channel_proposal(request, name="gain", index=0),)

    result = run_paired_harness_search(
        epoch=epoch,
        tasks=(task,),
        dependency_manifest=dependencies,
        founding_protocol=parent,
        config=_config(pair_keys=pair_keys, controls=controls),
        proposal_callback=proposals,
        evaluator_callback=_scripted_evaluator(
            epoch=epoch,
            task=task,
            dependencies=dependencies,
            founding_mode=founding_mode,
        ),
    )
    assert proposal_called is False
    assert result.candidate_opportunities_used == 0
    assert result.final_status.execution_status == "feasibility_stop"
    assert result.final_status.feasibility_status == expected_status
    assert all(record.status == "not_run" for record in result.control_opportunities)


def test_dry_run_validates_full_plan_without_evaluator_or_live_status(tmp_path) -> None:
    epoch, pair_keys = _epoch(max_candidate_evaluations=1, max_steps=1)
    task = _task(epoch)
    dependencies = _dependencies(task)
    parent = load_canonical_harness_seed().protocol
    controls = _controls(parent, task, dependencies)
    result = run_paired_harness_search(
        epoch=epoch,
        tasks=(task,),
        dependency_manifest=dependencies,
        founding_protocol=parent,
        config=_config(
            pair_keys=pair_keys,
            controls=controls,
            mode="dry_run",
        ),
    )
    assert result.final_status.execution_status == "dry_run"
    assert result.final_status.live_inference_status == "not_run"
    assert result.final_status.inference_requests_sent == 0
    assert result.founding_proof_bindings == ()
    assert all(record.status == "not_run" for record in result.control_opportunities)
    path = write_paired_harness_search_result(tmp_path / "search-result.json", result)
    loaded = load_paired_harness_search_result(path)
    assert loaded == result
    assert write_paired_harness_search_result(path, result) == path
