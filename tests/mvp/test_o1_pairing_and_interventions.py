from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from agintor.contracts.epochs import (
    DeploymentIdentity,
    EvaluatorAuthority,
    REPO_REPAIR_TRUSTED_TOOL_IDS,
    ResearchEpochManifest,
    SearchEnvelope,
    StopRule,
    TaskCeilings,
    TrustedToolAuthority,
)
from agintor.contracts.outcomes import (
    OutcomeCost,
    OutcomeHealth,
    OutcomeReceipt,
    PairKey,
    outcome_receipt_digest,
)
from agintor.contracts.run_evidence import (
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
    RouteEvidence,
    RunEvidence,
    RunHealth,
    RunProofRecord,
    TerminationEvidence,
    runtime_environment_evidence_digest,
)
from agintor.core.identity import canonical_identity_digest
from agintor.evaluation.interventions import (
    InterventionError,
    NeutralArtifactIntervention,
    build_neutral_artifact_intervention,
    join_matched_intervention_runs,
    length_matched_neutral_text,
)
from agintor.evaluation.pairing import PairingError, join_outcome_receipts
from agintor.storage.proof_records import (
    ImmutableProofRecordStore,
    ProofStoreError,
    proof_record_public_projection,
)


def _digest(label: str) -> str:
    return canonical_identity_digest(label, domain="test-o1")


def _ceilings() -> TaskCeilings:
    return TaskCeilings(
        max_model_calls=8,
        max_input_tokens=20_000,
        max_output_tokens=8_000,
        max_cached_tokens=10_000,
        max_tool_calls=20,
        max_tool_output_bytes=100_000,
        max_artifact_bytes=100_000,
        max_patch_bytes=30_000,
        max_retries=2,
        max_wall_time_ms=120_000,
        provider_deadline_ms=30_000,
        max_known_cost_usd=5.0,
        max_estimated_cost_usd=6.0,
    )


def _epoch() -> ResearchEpochManifest:
    tools = tuple(
        TrustedToolAuthority(
            tool_id=tool_id,
            implementation_digest=_digest(f"tool:{tool_id}"),
            policy_digest=_digest(f"policy:{tool_id}"),
        )
        for tool_id in REPO_REPAIR_TRUSTED_TOOL_IDS
    )
    return ResearchEpochManifest(
        epoch_id="epoch.o1",
        task_manifest_digest=_digest("tasks"),
        development_split_digest=_digest("development"),
        sealed_confirmation_split_digest=_digest("confirmation"),
        deployment=DeploymentIdentity(
            deployment_id="fixed.deployment",
            provider="openai",
            model="fixed-model",
            provider_config_digest=_digest("provider-config"),
            decoding_policy_digest=_digest("decoding"),
            price_schedule_digest=_digest("prices"),
            command_container_policy_digest=_digest("command-container-policy"),
        ),
        per_run_ceilings=_ceilings(),
        search_envelope=SearchEnvelope(
            max_steps=2,
            offspring_per_step=1,
            sampling_replicates=2,
            task_panel_digest=_digest("panel"),
        ),
        trusted_tools=tools,
        stop_rule=StopRule(
            max_candidate_evaluations=4,
            max_consecutive_non_improving_steps=2,
        ),
        evaluator_authority=EvaluatorAuthority(
            evaluator_id="repo-evaluator.v1",
            evaluator_identity_digest=_digest("evaluator"),
            evaluation_policy_digest=_digest("evaluation-policy"),
        ),
    )


def _pair(epoch: ResearchEpochManifest, replicate: int = 0) -> PairKey:
    return PairKey(
        task_manifest_id=f"task.o1.{replicate}",
        environment_id=f"environment.o1.{replicate}",
        sampling_replicate=replicate,
        provider_config_digest=epoch.deployment.provider_config_digest,
    )


def _cost(*, patch_bytes: int, retries: int = 0) -> OutcomeCost:
    return OutcomeCost(
        model_calls=2 + retries,
        input_tokens=100,
        output_tokens=50,
        cached_tokens=0,
        tool_calls=0,
        tool_output_bytes=0,
        artifact_bytes=200,
        patch_bytes=patch_bytes,
        retries=retries,
        wall_time_ms=1_000,
        known_cost_usd=0.1,
        estimated_cost_usd=0.0,
        unknown_dollars=False,
        within_epoch_envelope=True,
    )


def _outcome_health(*, healthy: bool = True) -> OutcomeHealth:
    return OutcomeHealth(
        process_integrity=healthy,
        no_leakage=True,
        environment_integrity=True,
        evaluator_integrity=True,
        accounting_complete=True,
    )


def _receipt(
    epoch: ResearchEpochManifest,
    pair_key: PairKey,
    *,
    protocol: str,
    complete_repair: bool,
    healthy: bool = True,
    cost: OutcomeCost | None = None,
    patch_digest: str | None = None,
) -> OutcomeReceipt:
    return OutcomeReceipt(
        receipt_id=f"receipt.{pair_key.sampling_replicate}.{protocol}",
        execution_mode="deterministic_replay",
        live_inference_status="not_run",
        real_inference_requests_sent=0,
        data_state="development",
        epoch_id=epoch.epoch_id,
        epoch_manifest_digest=epoch.epoch_manifest_digest,
        release_digest=_digest("release"),
        release_manifest_digest=_digest("release-manifest"),
        profile_digest=_digest("profile"),
        split_manifest_digest=epoch.development_split_digest,
        task_manifest_id=pair_key.task_manifest_id,
        task_manifest_digest=_digest(f"task:{pair_key.task_manifest_id}"),
        evaluation_contract_id=f"evaluation.{pair_key.task_manifest_id}",
        evaluation_contract_digest=_digest(f"evaluation:{pair_key.task_manifest_id}"),
        evaluator_id=epoch.evaluator_authority.evaluator_id,
        evaluator_identity_digest=epoch.evaluator_authority.evaluator_identity_digest,
        evaluation_policy_digest=epoch.evaluator_authority.evaluation_policy_digest,
        pair_key=pair_key,
        protocol_digest=_digest(protocol),
        compiler_digest=_digest("compiler"),
        kernel_digest=_digest("kernel"),
        tool_manifest_digest=_digest("tools"),
        provider_config_digest=epoch.deployment.provider_config_digest,
        decoding_policy_digest=epoch.deployment.decoding_policy_digest,
        price_schedule_digest=epoch.deployment.price_schedule_digest,
        command_container_policy_digest=(
            epoch.deployment.command_container_policy_digest
        ),
        evaluator_environment_digest=_digest(f"environment:{pair_key.environment_id}"),
        patch_digest=patch_digest or _digest(f"patch:{pair_key.sampling_replicate}:{protocol}"),
        complete_repair=complete_repair,
        health=_outcome_health(healthy=healthy),
        cost=cost or _cost(patch_bytes=30),
        issued_at_ms=1,
    )


def _run_evidence(
    epoch: ResearchEpochManifest,
    pair_key: PairKey,
    *,
    protocol: str = "parent",
    intervention: NeutralArtifactIntervention | None = None,
) -> RunEvidence:
    original_findings = ObservedValue(value="ORIGINAL_FINDINGS_ABC")
    delivered_findings = intervention.neutral if intervention is not None else original_findings
    patch_text = "diff --git a/a.py b/a.py\n+fixed\n"
    patch_value = ObservedValue(value=patch_text)
    patch_digest = canonical_identity_digest(
        patch_text,
        domain="final-unified-diff",
    )
    patch_bytes = len(patch_text.encode("utf-8"))

    context_investigate = PreCallContextEvidence(
        context_id="context.investigate",
        sequence_no=1,
        call_id="call.investigate",
        actor_id="investigator",
        entries=(
            ContextEntry(
                entry_id="instruction.investigate",
                source_kind="instruction",
                source_ref="actors[investigator].instruction",
                observed=ObservedValue(value="Investigate the public failure."),
            ),
            ContextEntry(
                entry_id="task.issue",
                source_kind="task",
                source_ref="issue",
                observed=ObservedValue(value="Repair the parser regression."),
            ),
        ),
    )
    context_implement = PreCallContextEvidence(
        context_id="context.implement",
        sequence_no=2,
        call_id="call.implement",
        actor_id="implementer",
        entries=(
            ContextEntry(
                entry_id="instruction.implement",
                source_kind="instruction",
                source_ref="actors[implementer].instruction",
                observed=ObservedValue(value="Implement a minimal verified patch."),
            ),
            ContextEntry(
                entry_id="artifact.findings",
                source_kind="artifact",
                source_ref="artifact.findings",
                observed=delivered_findings,
            ),
        ),
    )
    artifacts = (
        ArtifactEvidence(
            artifact_id="artifact.findings",
            channel_id="channel.findings",
            producer_call_id="call.investigate",
            artifact_schema="text",
            observed=original_findings,
            payload_bytes=len(str(original_findings.value).encode("utf-8")),
            intended_consumer_call_ids=("call.implement",),
            actual_consumer_call_ids=("call.implement",),
        ),
        ArtifactEvidence(
            artifact_id="artifact.patch",
            channel_id="channel.patch",
            producer_call_id="call.implement",
            artifact_schema="text",
            observed=patch_value,
            payload_bytes=len(str(patch_value.value).encode("utf-8")),
            intended_consumer_call_ids=(),
            actual_consumer_call_ids=(),
        ),
    )
    delivery = ArtifactDeliveryEvidence(
        delivery_id="delivery.findings",
        sequence_no=1,
        artifact_id="artifact.findings",
        channel_id="channel.findings",
        producer_call_id="call.investigate",
        consumer_call_id="call.implement",
        delivery_kind=("neutral_replacement" if intervention else "intact"),
        intervention_digest=(intervention.intervention_digest if intervention else None),
        observed=delivered_findings,
        payload_bytes=len(str(delivered_findings.value).encode("utf-8")),
    )
    read = ArtifactReadEvidence(
        read_id="read.findings",
        sequence_no=1,
        artifact_id="artifact.findings",
        channel_id="channel.findings",
        consumer_call_id="call.implement",
        context_id="context.implement",
        context_entry_id="artifact.findings",
        delivery_id="delivery.findings",
        observed=delivered_findings,
        payload_bytes=len(str(delivered_findings.value).encode("utf-8")),
    )
    calls = (
        ProviderCallEvidence(
            provider_call_id="provider.investigate",
            sequence_no=1,
            call_id="call.investigate",
            actor_id="investigator",
            turn_index=0,
            attempt_index=0,
            context_id=context_investigate.context_id,
            context_digest=context_investigate.context_digest,
            deployment_id=epoch.deployment.deployment_id,
            provider=epoch.deployment.provider,
            model=epoch.deployment.model,
            provider_config_digest=epoch.deployment.provider_config_digest,
            request_digest=_digest("request.investigate"),
            status="succeeded",
            request_sent=True,
            response_id=("resp-neutral-1" if intervention else "resp-intact-1"),
            response_digest=_digest("response.investigate"),
            response_kind="terminal",
            started_at_ms=1,
            finished_at_ms=2,
        ),
        ProviderCallEvidence(
            provider_call_id="provider.implement",
            sequence_no=2,
            call_id="call.implement",
            actor_id="implementer",
            turn_index=0,
            attempt_index=0,
            context_id=context_implement.context_id,
            context_digest=context_implement.context_digest,
            deployment_id=epoch.deployment.deployment_id,
            provider=epoch.deployment.provider,
            model=epoch.deployment.model,
            provider_config_digest=epoch.deployment.provider_config_digest,
            request_digest=_digest("request.implement.neutral" if intervention else "request.implement.intact"),
            status="succeeded",
            request_sent=True,
            response_id=("resp-neutral-2" if intervention else "resp-intact-2"),
            response_digest=_digest("response.implement"),
            response_kind="terminal",
            started_at_ms=3,
            finished_at_ms=4,
        ),
    )
    cost = _cost(patch_bytes=patch_bytes)
    return RunEvidence(
        evidence_id=("evidence.neutral" if intervention else "evidence.intact"),
        run_id=("run.neutral" if intervention else "run.intact"),
        execution_mode="deterministic_replay",
        live_inference_status="not_run",
        real_inference_requests_sent=0,
        arm=("neutral_artifact" if intervention else "intact"),
        intervention_digest=(intervention.intervention_digest if intervention else None),
        data_state="development",
        epoch_id=epoch.epoch_id,
        epoch_manifest_digest=epoch.epoch_manifest_digest,
        release_digest=_digest("release"),
        release_manifest_digest=_digest("release-manifest"),
        profile_digest=_digest("profile"),
        split_manifest_digest=epoch.development_split_digest,
        pair_key=pair_key,
        task_manifest_digest=_digest(f"task:{pair_key.task_manifest_id}"),
        protocol_digest=_digest(protocol),
        compiled_semantic_digest=_digest("compiled-semantic"),
        dependency_manifest_digest=_digest("dependency-manifest"),
        compiler_digest=_digest("compiler"),
        kernel_digest=_digest("kernel"),
        tool_manifest_digest=_digest("tools"),
        provider_config_digest=epoch.deployment.provider_config_digest,
        decoding_policy_digest=epoch.deployment.decoding_policy_digest,
        price_schedule_digest=epoch.deployment.price_schedule_digest,
        command_container_policy_digest=(
            epoch.deployment.command_container_policy_digest
        ),
        deployment_id=epoch.deployment.deployment_id,
        provider=epoch.deployment.provider,
        model=epoch.deployment.model,
        contexts=(context_investigate, context_implement),
        artifacts=artifacts,
        deliveries=(delivery,),
        reads=(read,),
        routes=(
            RouteEvidence(
                route_id="route.start",
                sequence_no=1,
                route_kind="sequential",
                to_call_id="call.investigate",
                stage_id="stage.1",
                trigger="plan_start",
            ),
            RouteEvidence(
                route_id="route.findings",
                sequence_no=2,
                route_kind="sequential",
                from_call_id="call.investigate",
                to_call_id="call.implement",
                stage_id="stage.2",
                trigger="producer_completed",
            ),
        ),
        provider_calls=calls,
        tool_receipts=(),
        retries=(),
        cost_ledger=CostLedgerEvidence(
            cost=cost,
            provider_deadline_ms=epoch.per_run_ceilings.provider_deadline_ms,
            deadline_exceeded=False,
            active_reservations=0,
            reconciled=True,
        ),
        environment=EnvironmentEvidence(
            **(
                runtime_environment := {
                    "environment_id": pair_key.environment_id,
                    "command_container_policy_digest": (
                        epoch.deployment.command_container_policy_digest
                    ),
                    "python_identity": sys.version,
                    "platform_identity": sys.platform,
                    "workspace_snapshot_digest": _digest("workspace"),
                    "container_image_digest": None,
                    "network_policy": "none",
                    "filesystem_policy": "scratch-workspace-only",
                }
            ),
            runtime_environment_digest=runtime_environment_evidence_digest(
                runtime_environment
            ),
        ),
        patch=PatchEvidence(
            status="emitted",
            observed=patch_value,
            patch_digest=patch_digest,
            patch_bytes=patch_bytes,
            artifact_id="artifact.patch",
            public_verification_passed=True,
        ),
        termination=TerminationEvidence(
            reason="success",
            final_call_id="call.implement",
            final_patch_digest=patch_digest,
            completed_at_ms=5,
            success=True,
        ),
        health=RunHealth(
            process_integrity=True,
            no_leakage=True,
            context_integrity=True,
            artifact_integrity=True,
            tool_integrity=True,
            accounting_complete=True,
            environment_integrity=True,
        ),
    )


def _proof_record(epoch: ResearchEpochManifest, evidence: RunEvidence) -> RunProofRecord:
    receipt = _receipt(
        epoch,
        evidence.pair_key,
        protocol="parent",
        complete_repair=True,
        cost=evidence.cost_ledger.cost,
        patch_digest=evidence.patch.patch_digest,
    )
    return RunProofRecord(
        proof_record_id="proof.intact",
        run_evidence=evidence,
        outcome_receipt=receipt,
    )


def test_run_evidence_records_exact_context_delivery_read_and_provider_ids() -> None:
    epoch = _epoch()
    evidence = _run_evidence(epoch, _pair(epoch))

    produced = evidence.artifacts[0].observed
    delivered = evidence.deliveries[0].observed
    read = evidence.reads[0].observed
    context_value = evidence.contexts[1].entries[1].observed
    assert produced == delivered == read == context_value
    assert produced.value == "ORIGINAL_FINDINGS_ABC"
    assert evidence.provider_calls[0].response_id == "resp-intact-1"
    assert evidence.cost_ledger.cost.model_calls == len(evidence.provider_calls)
    assert evidence.health.healthy
    assert len(evidence.evidence_digest) == 64


def test_declared_but_undelivered_or_unread_artifact_fails_closed() -> None:
    epoch = _epoch()
    evidence = _run_evidence(epoch, _pair(epoch))
    payload = evidence.model_dump(mode="python")
    payload["deliveries"] = []
    payload["evidence_digest"] = ""
    with pytest.raises(ValidationError, match="undelivered artifact"):
        RunEvidence.model_validate(payload)

    payload = evidence.model_dump(mode="python")
    payload["reads"] = []
    payload["evidence_digest"] = ""
    with pytest.raises(ValidationError, match="every delivered artifact"):
        RunEvidence.model_validate(payload)


def test_run_evidence_rejects_resolved_credentials() -> None:
    with pytest.raises(ValidationError, match="resolved credential"):
        ObservedValue(value={"api_key": "not-allowed"})
    with pytest.raises(ValidationError, match="resolved credential"):
        ObservedValue(value="Bearer abcdefghijklmnopqrstuvwxyz")


def test_pair_join_is_order_invariant_and_requires_exact_healthy_panel() -> None:
    epoch = _epoch()
    pairs = (_pair(epoch, 0), _pair(epoch, 1))
    parents = tuple(
        _receipt(epoch, pair, protocol="parent", complete_repair=False)
        for pair in pairs
    )
    children = tuple(
        _receipt(epoch, pair, protocol="child", complete_repair=True)
        for pair in pairs
    )

    first = join_outcome_receipts(
        epoch=epoch,
        expected_pair_keys=pairs,
        parent_receipts=parents,
        child_receipts=children,
    )
    shuffled = join_outcome_receipts(
        epoch=epoch,
        expected_pair_keys=tuple(reversed(pairs)),
        parent_receipts=tuple(reversed(parents)),
        child_receipts=tuple(reversed(children)),
    )
    assert first.join_digest == shuffled.join_digest
    assert first.expected_pair_key_digests == tuple(sorted(first.expected_pair_key_digests))

    with pytest.raises(PairingError, match="coverage mismatch"):
        join_outcome_receipts(
            epoch=epoch,
            expected_pair_keys=pairs,
            parent_receipts=parents,
            child_receipts=children[:1],
        )
    with pytest.raises(PairingError, match="duplicate parent"):
        join_outcome_receipts(
            epoch=epoch,
            expected_pair_keys=pairs,
            parent_receipts=(*parents, parents[0]),
            child_receipts=children,
        )

    unhealthy = children[0].model_copy(update={"health": _outcome_health(healthy=False)})
    unhealthy = unhealthy.model_copy(
        update={"receipt_digest": outcome_receipt_digest(unhealthy)}
    )
    with pytest.raises(PairingError, match="unhealthy or unauthorized"):
        join_outcome_receipts(
            epoch=epoch,
            expected_pair_keys=pairs,
            parent_receipts=parents,
            child_receipts=(unhealthy, children[1]),
        )


def test_pair_join_rejects_crossed_configuration_even_with_recomputed_digest() -> None:
    epoch = _epoch()
    pair = _pair(epoch)
    parent = _receipt(epoch, pair, protocol="parent", complete_repair=False)
    child = _receipt(epoch, pair, protocol="child", complete_repair=True)
    crossed = child.model_copy(
        update={"evaluation_contract_digest": _digest("crossed-evaluation")}
    )
    crossed = crossed.model_copy(
        update={"receipt_digest": outcome_receipt_digest(crossed)}
    )

    with pytest.raises(PairingError, match="configuration mismatch"):
        join_outcome_receipts(
            epoch=epoch,
            expected_pair_keys=(pair,),
            parent_receipts=(parent,),
            child_receipts=(crossed,),
        )


def test_neutral_artifact_replacement_is_schema_length_route_call_and_price_matched() -> None:
    epoch = _epoch()
    pair = _pair(epoch)
    intact = _run_evidence(epoch, pair)
    original = intact.artifacts[0].observed.value
    neutral_text = length_matched_neutral_text(original)
    measure = lambda text: ObservedValue(value=text).serialized_bytes
    intervention = build_neutral_artifact_intervention(
        source_run=intact,
        artifact_id="artifact.findings",
        consumer_call_id="call.implement",
        neutral_value=neutral_text,
        priced_input_measure=measure,
    )
    neutral = _run_evidence(epoch, pair, intervention=intervention)
    matched = join_matched_intervention_runs(
        intact_run=intact,
        neutral_run=neutral,
        intervention=intervention,
    )

    assert intervention.original.value != intervention.neutral.value
    assert intervention.original.serialized_bytes == intervention.neutral.serialized_bytes
    assert intervention.original_priced_input_units == intervention.neutral_priced_input_units
    assert neutral.deliveries[0].observed == intervention.neutral
    assert neutral.contexts[1].entries[1].observed == intervention.neutral
    assert matched.provider_call_count == len(intact.provider_calls)
    assert matched.input_tokens_per_arm == intact.cost_ledger.cost.input_tokens


def test_neutral_intervention_rejects_unmatched_price_or_sealed_canary() -> None:
    epoch = _epoch()
    intact = _run_evidence(epoch, _pair(epoch))
    neutral = length_matched_neutral_text(intact.artifacts[0].observed.value)
    with pytest.raises(InterventionError, match="priced input"):
        build_neutral_artifact_intervention(
            source_run=intact,
            artifact_id="artifact.findings",
            consumer_call_id="call.implement",
            neutral_value=neutral,
            priced_input_measure=lambda text: 1 if text.startswith("ORIGINAL") else 2,
        )
    with pytest.raises(ValueError, match="sealed canary"):
        build_neutral_artifact_intervention(
            source_run=intact,
            artifact_id="artifact.findings",
            consumer_call_id="call.implement",
            neutral_value=neutral,
            priced_input_measure=lambda text: 1,
            canary_values=(neutral,),
        )


def test_proof_store_is_immutable_single_writer_and_receipt_walkable(tmp_path: Path) -> None:
    epoch = _epoch()
    evidence = _run_evidence(epoch, _pair(epoch))
    record = _proof_record(epoch, evidence)
    store = ImmutableProofRecordStore(tmp_path / "proof")

    path = store.append(record)
    assert store.append(record) == path
    loaded = store.load(path)
    walked = store.lookup_outcome(record.outcome_receipt.receipt_digest)
    assert loaded == record
    assert walked.proof_record_digest == record.proof_record_digest
    assert walked.run_evidence.contexts[1].entries[1].observed.value == "ORIGINAL_FINDINGS_ABC"
    assert not (store.root / "checkpoints").exists()
    assert not (store.root / "state_store").exists()
    assert not (store.root / "traces").exists()
    manifest = json.loads((store.root / "store_manifest.json").read_text(encoding="utf-8"))
    assert manifest["path_policy"] == {
        "checkpoint_publication": False,
        "derived_state_indexing": False,
        "trace_rematerialization": False,
    }

    path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ProofStoreError, match="different bytes"):
        store.append(record)


def test_public_proof_projection_contains_only_digests_and_no_exact_values() -> None:
    epoch = _epoch()
    evidence = _run_evidence(epoch, _pair(epoch))
    record = _proof_record(epoch, evidence)
    projection = proof_record_public_projection(record)
    serialized = json.dumps(projection, sort_keys=True)

    assert "ORIGINAL_FINDINGS_ABC" not in serialized
    assert "Repair the parser regression" not in serialized
    assert "value\"" not in serialized
    assert record.proof_record_digest in serialized
    assert evidence.evidence_digest in serialized
    assert record.outcome_receipt.receipt_digest in serialized
