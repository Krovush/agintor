from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from agintor.authority.public_tasks import epoch_public_projection, task_envelope_public_projection
from agintor.contracts.epochs import (
    EvaluatorAuthority,
    PublicReproductionStep,
    REPO_REPAIR_TRUSTED_TOOL_IDS,
    ResearchEpochManifest,
    SearchEnvelope,
    StopRule,
    TaskCeilings,
    TaskEnvelope,
    TrustedToolAuthority,
    WorkspaceSnapshotRef,
)
from agintor.contracts.feasibility import (
    BaselineHeadroomAssessment,
    DevelopmentTaskFeasibilityManifest,
    FeasibilityControlResult,
    PairedSearchBudgetProjection,
    ProviderBaselineDryRun,
)
from agintor.contracts.harness import DependencyRef, RuntimeDependencyManifest, TrustedToolDependency
from agintor.contracts.outcomes import (
    OutcomeCost,
    OutcomeHealth,
    OutcomeReceipt,
    PairKey,
    pair_key_digest,
)
from agintor.contracts.run_evidence import (
    ArtifactEvidence,
    ContextEntry,
    CostLedgerEvidence,
    EnvironmentEvidence,
    ObservedValue,
    PatchEvidence,
    PreCallContextEvidence,
    ProviderCallEvidence,
    RunEvidence,
    RunHealth,
    TerminationEvidence,
    ToolReceiptEvidence,
    runtime_environment_evidence_digest,
)
from agintor.core.identity import canonical_identity_digest, evidence_digest
from agintor.evaluation.contracts import (
    EvaluationContract,
    HiddenCheck,
    SealedCanary,
    SealedFixtureRef,
)
from agintor.evaluation.gate0 import (
    build_gate0_dry_run_manifest,
    build_gate0_provider_identity,
    validate_gate0_dry_run_conformance,
)
from agintor.evaluation.pilot import (
    CONTROLLED_EVIDENCE_DIR,
    PUBLIC_RELEASE_EVIDENCE_DIR,
    D0FixtureFeasibilityEvidence,
    FactoryFollowupIdentityEvidence,
    Gate0OfflineReadiness,
    GateImplementationEvidence,
    ImmutableReleaseIdentity,
    OfflineSolveExecutionProvenance,
    PilotContentNullInterventionEvidence,
    PilotEvaluatorCall,
    PilotEvidenceError,
    PilotEvidencePath,
    PilotEvaluationContractEvidence,
    PilotGateDeterministicTestEvidence,
    PilotNotRunDevelopmentReport,
    PilotRawPairedOutcomeRecord,
    PilotToolCall,
    RetainedStructuralTransactionEvidence,
    RuntimeSessionIdentityEvidence,
    RuntimeSessionIdentitySet,
    S1OfflineRetentionEvidence,
    audit_public_development_tasks,
    build_mvp_readiness_evidence_packet,
    build_pilot_dry_run_manifest,
    consume_reserved_pilot_task,
    gate0_conformance_report_digest,
    replay_mvp_readiness_evidence_packet,
    require_pilot_live_authorization,
    reserve_audited_pilot_task,
    write_mvp_readiness_evidence_packet,
    REQUIRED_MVP_GATES,
)
from agintor.factory.harness_release_contracts import (
    ActiveReleasePointer,
    Gate0NotRunReport,
    Gate0PreregistrationPublic,
    HARNESS_RELEASE_SCHEMA_VERSION,
    HarnessReleaseManifest,
    PilotNotRunSummary,
    PublicEvidenceIndex,
    PublicSearchLineageRecord,
)
from agintor.runtime.api.composite_compiler import (
    compile_composite_run_plan,
    load_canonical_harness_seed,
)
from agintor.runtime.kernel.composite_budget import AggregateBudgetSnapshot
from agintor.runtime.harness_profile import (
    HarnessCommandContainerPolicy,
    HarnessDecodingPolicy,
    HarnessDeploymentProfile,
    HarnessProviderEndpoint,
    HarnessUsdPriceSchedule,
    harness_deployment_profile_digest,
)
from agintor.runtime.kernel.composite_provider import CredentialReference
from agintor.runtime.sdk.harness_executor import (
    CONTROLLED_RUN_EVIDENCE_REF,
    HARNESS_SOLVE_RESULT_FILE,
    HarnessCompiledExecutionIdentity,
    HarnessControlledRunEvidenceReference,
    HarnessProviderRoundReference,
    HarnessPublicVerificationSummary,
    HarnessReleaseExecutionIdentity,
    HarnessRunEvidenceIndex,
    HarnessSolveResult,
    HarnessSubmittedPatch,
    HarnessTaskExecutionIdentity,
    HarnessTerminationSummary,
    HarnessToolReceiptReference,
)
from agintor.storage.harness_factory_transaction import (
    HarnessFactoryChatManifest,
    HarnessFactoryMessage,
)
from agintor.storage.harness_session_store import HarnessSessionManifest


CANARY = "P1-SEALED-CANARY-9b85a4"


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _jsonl(*rows: object) -> bytes:
    return b"".join(_json(row) for row in rows)


def _artifact_raw(value: object) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, BaseModel):
        return _json(value.model_dump(mode="json", exclude_none=True))
    return _json(value)


def _artifact_sha(value: object) -> str:
    return hashlib.sha256(_artifact_raw(value)).hexdigest()


def _raw_paired_outcomes_digest(rows: tuple[PilotRawPairedOutcomeRecord, ...]) -> str:
    return evidence_digest(
        {
            "kind": "repo-repair-pilot-raw-paired-outcomes-v1",
            "outcomes": [row.model_dump(mode="python") for row in rows],
        }
    )


def _ceilings() -> TaskCeilings:
    return TaskCeilings(
        max_model_calls=8,
        max_input_tokens=30_000,
        max_output_tokens=12_000,
        max_cached_tokens=0,
        max_tool_calls=30,
        max_tool_output_bytes=200_000,
        max_artifact_bytes=40_000,
        max_patch_bytes=20_000,
        max_retries=2,
        max_wall_time_ms=30_000,
        provider_deadline_ms=5_000,
        max_known_cost_usd=1.0,
        max_estimated_cost_usd=2.0,
    )


def _dependencies() -> RuntimeDependencyManifest:
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
            for tool_id in sorted(REPO_REPAIR_TRUSTED_TOOL_IDS)
        ),
    )


def _deployment_profile() -> HarnessDeploymentProfile:
    return HarnessDeploymentProfile(
        deployment_id="deployment.p1",
        provider="offline-provider",
        model="offline-model",
        endpoint=HarnessProviderEndpoint(
            base_url="https://offline-provider.example/v1",
            api_key_env="PILOT_PROVIDER_API_KEY",
            api_key_file_env="PILOT_PROVIDER_KEY_FILE",
        ),
        decoding_policy=HarnessDecodingPolicy(
            temperature=0.0,
            top_p=1.0,
            max_output_tokens=2_000,
        ),
        price_schedule=HarnessUsdPriceSchedule(
            billing_mode="paid",
            input_usd_per_million_tokens=1.0,
            output_usd_per_million_tokens=2.0,
            cached_input_usd_per_million_tokens=0.25,
        ),
        command_container_policy=HarnessCommandContainerPolicy(
            image="python@sha256:" + "a" * 64,
            timeout_s=30.0,
            memory_bytes=512 * 1024 * 1024,
            cpu_count=1.0,
            pids_limit=128,
            output_bytes=1_000_000,
            tmpfs_bytes=64 * 1024 * 1024,
            nofile_limit=256,
        ),
    )


def _credential_reference(
    profile: HarnessDeploymentProfile,
    *,
    api_key_env: str | None = None,
    api_key_file_env: str | None = None,
) -> CredentialReference:
    return CredentialReference(
        provider_name=profile.provider,
        api_key_env=api_key_env if api_key_env is not None else profile.endpoint.api_key_env,
        api_key_file_env=(
            api_key_file_env
            if api_key_file_env is not None
            else profile.endpoint.api_key_file_env
        ),
    )


def _epoch(dependencies: RuntimeDependencyManifest) -> ResearchEpochManifest:
    profile = _deployment_profile()
    return ResearchEpochManifest(
        epoch_id="epoch.p1",
        task_manifest_digest=_digest("task-panel"),
        development_split_digest=_digest("development"),
        sealed_confirmation_split_digest=_digest("sealed-confirmation"),
        deployment=profile.to_deployment_identity(),
        per_run_ceilings=_ceilings(),
        search_envelope=SearchEnvelope(
            max_steps=1,
            offspring_per_step=1,
            sampling_replicates=1,
            task_panel_digest=_digest("panel"),
        ),
        trusted_tools=tuple(
            TrustedToolAuthority(
                tool_id=tool_id,
                implementation_digest=next(
                    item.implementation_digest
                    for item in dependencies.trusted_tools
                    if item.tool_id == tool_id
                ),
                policy_digest=next(
                    item.policy_digest
                    for item in dependencies.trusted_tools
                    if item.tool_id == tool_id
                ),
            )
            for tool_id in REPO_REPAIR_TRUSTED_TOOL_IDS
        ),
        stop_rule=StopRule(
            max_candidate_evaluations=1,
            max_consecutive_non_improving_steps=1,
        ),
        evaluator_authority=EvaluatorAuthority(
            evaluator_id="repo-evaluator.p1",
            evaluator_identity_digest=_digest("evaluator"),
            evaluation_policy_digest=_digest("evaluation-policy"),
        ),
    )


def _task(epoch: ResearchEpochManifest, label: str = "pilot") -> TaskEnvelope:
    return TaskEnvelope(
        task_manifest_id=f"task.p1.{label}",
        epoch_id=epoch.epoch_id,
        epoch_manifest_digest=epoch.epoch_manifest_digest,
        data_state="development",
        split_manifest_digest=epoch.development_split_digest,
        issue=f"Repair the public parser regression {label}.",
        workspace_snapshot=WorkspaceSnapshotRef(
            snapshot_id=f"snapshot.p1.{label}",
            uri=f"public/{label}-snapshot",
            digest=_digest(f"snapshot:{label}"),
        ),
        public_reproduction=(
            PublicReproductionStep(
                step_id="public-test",
                argv=("python", "-m", "pytest", "-q"),
                timeout_ms=10_000,
            ),
        ),
        ceilings=_ceilings(),
    )


def _core():
    dependencies = _dependencies()
    epoch = _epoch(dependencies)
    task = _task(epoch)
    protocol = load_canonical_harness_seed().protocol
    plan = compile_composite_run_plan(task, protocol, dependencies)
    return dependencies, epoch, task, protocol, plan


def _full_evaluation_contract(
    epoch: ResearchEpochManifest,
    task: TaskEnvelope,
) -> EvaluationContract:
    return EvaluationContract(
        evaluation_contract_id="evaluation.p1",
        epoch_id=epoch.epoch_id,
        epoch_manifest_digest=epoch.epoch_manifest_digest,
        data_state=task.data_state,
        split_manifest_digest=task.split_manifest_digest,
        task_manifest_id=task.task_manifest_id,
        task_manifest_digest=task.task_manifest_digest,
        sealed_fixture=SealedFixtureRef(
            fixture_id="fixture.p1",
            uri=task.workspace_snapshot.uri,
            fixture_digest=_digest("sealed-fixture"),
            public_snapshot_digest=task.workspace_snapshot.digest,
        ),
        protected_paths=("tests",),
        hidden_checks=(
            HiddenCheck(
                check_id="hidden.p1",
                argv=("python", "-m", "pytest", "-q", "tests/hidden"),
                timeout_ms=10_000,
            ),
        ),
        outcome_authority=epoch.evaluator_authority,
        canaries=(SealedCanary(canary_id="canary.p1", value=CANARY),),
    )


def test_public_evaluation_contract_projection_preserves_sealed_authority_digest() -> None:
    _, epoch, task, _, _ = _core()
    contract = _full_evaluation_contract(epoch, task)

    projection = PilotEvaluationContractEvidence.from_contract(contract)

    assert projection.evaluation_contract_digest == contract.evaluation_contract_digest
    assert projection.public_projection_digest != contract.evaluation_contract_digest
    assert CANARY not in projection.model_dump_json()
    with pytest.raises(ValidationError, match="evaluation_contract_digest"):
        PilotEvaluationContractEvidence(
            evaluation_contract_id=contract.evaluation_contract_id,
            evaluator_id=epoch.evaluator_authority.evaluator_id,
            evaluator_identity_digest=(
                epoch.evaluator_authority.evaluator_identity_digest
            ),
            evaluation_policy_digest=epoch.evaluator_authority.evaluation_policy_digest,
        )
    forged = projection.model_dump(mode="python")
    forged["public_projection_digest"] = "0" * 64
    with pytest.raises(ValidationError, match="public projection digest mismatch"):
        PilotEvaluationContractEvidence.model_validate(forged)


def _audit(epoch: ResearchEpochManifest, *tasks: TaskEnvelope):
    audited = audit_public_development_tasks(
        audit_id="audit.p1",
        epoch=epoch,
        tasks=tasks,
        inspected_at_ms=10,
        canary_values=(CANARY,),
    )
    return reserve_audited_pilot_task(
        audited,
        pilot_id="pilot.p1",
        task_manifest_digest=tasks[0].task_manifest_digest,
        reserved_at_ms=11,
    )


def test_public_audit_and_reservation_lifecycle_is_one_way_and_single_use() -> None:
    _, epoch, first, _, _ = _core()
    second = _task(epoch, "second")
    audit = _audit(epoch, first, second)

    assert audit.reservation_state == "reserved"
    assert all(task.permanently_development for task in audit.tasks)
    assert audit.reserved_task.task_manifest_digest == first.task_manifest_digest
    with pytest.raises(PilotEvidenceError, match="exactly one"):
        reserve_audited_pilot_task(
            audit,
            pilot_id="pilot.other",
            task_manifest_digest=second.task_manifest_digest,
            reserved_at_ms=12,
        )

    consumed = consume_reserved_pilot_task(
        audit,
        consumption_evidence_digest=_digest("pilot-consumption"),
        consumed_at_ms=20,
    )
    assert consumed.reservation_state == "consumed_reclassified_development"
    assert tuple(event.event for event in consumed.reservation_events) == (
        "reserved",
        "consumed",
        "reclassified_development",
    )
    assert consumed.reservation_events[-1].occurred_at_ms == consumed.reservation_events[-2].occurred_at_ms
    with pytest.raises(PilotEvidenceError, match="not in the one consumable"):
        consume_reserved_pilot_task(
            consumed,
            consumption_evidence_digest=_digest("second-use"),
            consumed_at_ms=21,
        )

    sealed = first.model_copy(
        update={
            "data_state": "sealed_confirmation",
            "split_manifest_digest": epoch.sealed_confirmation_split_digest,
            "task_manifest_digest": "",
        }
    )
    sealed = TaskEnvelope.model_validate(
        sealed.model_dump(mode="python", exclude={"task_manifest_digest"})
    )
    with pytest.raises(PilotEvidenceError, match="sealed-confirmation"):
        audit_public_development_tasks(
            audit_id="audit.sealed",
            epoch=epoch,
            tasks=(sealed,),
            inspected_at_ms=30,
        )
    contaminated = task_envelope_public_projection(first)
    contaminated["evaluation_contract"] = {"hidden": True}
    with pytest.raises(ValidationError, match="evaluation_contract|sealed/evaluator"):
        type(audit.tasks[0]).model_validate(
            {
                **audit.tasks[0].model_dump(mode="python"),
                "public_projection": contaminated,
                "public_projection_digest": "",
            }
        )


def _pilot_manifest(
    release_manifest,
    pointer,
    epoch,
    task,
    plan,
    audit,
    sessions,
    *,
    evaluation_contract_digest: str | None = None,
    environment_digest: str | None = None,
    run_pair_digest: str = "pair-001",
):
    actor_call = plan.actor_calls[0]
    tool_id = actor_call.tool_ids[0]
    tool_calls = (
        PilotToolCall(
            sequence=0,
            call_id="tool.inspect",
            actor_call_id=actor_call.call_id,
            tool_id=tool_id,
            action_digest=_digest("planned-tool-action"),
            max_output_bytes=4096,
        ),
    )
    evaluator_calls = (
        PilotEvaluatorCall(
            sequence=0,
            call_id="evaluator.apply-and-score",
            evaluator_id=epoch.evaluator_authority.evaluator_id,
            evaluator_identity_digest=epoch.evaluator_authority.evaluator_identity_digest,
            evaluation_contract_digest=evaluation_contract_digest
            or _digest("evaluation-contract"),
            action_digest=_digest("evaluator-action"),
        ),
    )
    run_root = f"{CONTROLLED_EVIDENCE_DIR}/runs/{run_pair_digest}"
    paths = (
        PilotEvidencePath(
            purpose="public_summary",
            scope="public",
            relative_path=f"{PUBLIC_RELEASE_EVIDENCE_DIR}/pilot_summary.json",
        ),
        PilotEvidencePath(
            purpose="task_audit",
            scope="controlled",
            relative_path=f"{CONTROLLED_EVIDENCE_DIR}/evaluator/task_audit_manifest.json",
        ),
        PilotEvidencePath(
            purpose="pilot_compiled_plan",
            scope="controlled",
            relative_path=f"{CONTROLLED_EVIDENCE_DIR}/pilot/compiled_plan.json",
        ),
        PilotEvidencePath(
            purpose="run_manifest",
            scope="controlled",
            relative_path=f"{run_root}/run_manifest.json",
        ),
        PilotEvidencePath(
            purpose="pre_call_contexts",
            scope="controlled",
            relative_path=f"{run_root}/pre_call_contexts",
        ),
        PilotEvidencePath(
            purpose="artifacts",
            scope="controlled",
            relative_path=f"{run_root}/artifacts",
        ),
        PilotEvidencePath(
            purpose="tool_receipts",
            scope="controlled",
            relative_path=f"{run_root}/tool_and_side_effect_receipts.jsonl",
        ),
        PilotEvidencePath(
            purpose="outcome_receipts",
            scope="controlled",
            relative_path=f"{CONTROLLED_EVIDENCE_DIR}/evaluator/outcome_receipts.jsonl",
        ),
        PilotEvidencePath(
            purpose="pilot_report",
            scope="controlled",
            relative_path=f"{CONTROLLED_EVIDENCE_DIR}/analysis/pilot_report.json",
        ),
    )
    continuation = next(
        item for item in sessions.sessions if item.role == "same_release_continuation"
    )
    return build_pilot_dry_run_manifest(
        pilot_id="pilot.p1",
        active_release=pointer,
        release_manifest=release_manifest,
        epoch=epoch,
        task=task,
        plan=plan,
        audit=audit,
        session_id=continuation.session_id,
        session_manifest_digest=continuation.session_manifest_digest,
        session_release_digest=continuation.active_release_digest,
        environment_id="environment.p1",
        environment_digest=environment_digest or _digest("environment"),
        tool_calls=tool_calls,
        evaluator_calls=evaluator_calls,
        evidence_paths=paths,
        created_at_ms=100,
    )


def _environment_evidence(
    epoch: ResearchEpochManifest,
    task: TaskEnvelope,
) -> EnvironmentEvidence:
    payload = {
        "environment_id": "environment.p1",
        "command_container_policy_digest": epoch.deployment.command_container_policy_digest,
        "python_identity": "python-3.12-offline-fixture",
        "platform_identity": "windows-offline-fixture",
        "workspace_snapshot_digest": task.workspace_snapshot.digest,
        "container_image_digest": None,
        "network_policy": "none",
        "filesystem_policy": "scratch-workspace-only",
    }
    return EnvironmentEvidence(
        **payload,
        runtime_environment_digest=runtime_environment_evidence_digest(payload),
    )


def _outcome_cost(
    *,
    model_calls: int,
    tool_calls: int,
    patch_bytes: int,
) -> OutcomeCost:
    return OutcomeCost(
        model_calls=model_calls,
        input_tokens=0,
        output_tokens=0,
        cached_tokens=0,
        tool_calls=tool_calls,
        tool_output_bytes=0,
        artifact_bytes=patch_bytes,
        patch_bytes=patch_bytes,
        retries=0,
        wall_time_ms=1,
        known_cost_usd=0.0,
        estimated_cost_usd=0.0,
        within_epoch_envelope=True,
    )


def _run_evidence(
    *,
    dependencies: RuntimeDependencyManifest,
    epoch: ResearchEpochManifest,
    task: TaskEnvelope,
    release: ImmutableReleaseIdentity,
    protocol_digest: str,
    plan,
    pilot,
    pair_key: PairKey,
    environment: EnvironmentEvidence,
) -> RunEvidence:
    contexts: list[PreCallContextEvidence] = []
    provider_calls: list[ProviderCallEvidence] = []
    for sequence, call in enumerate(pilot.model_calls):
        context = PreCallContextEvidence(
            context_id=f"context.{sequence}",
            sequence_no=sequence,
            call_id=call.call_id,
            actor_id=call.actor_id,
            entries=(
                ContextEntry(
                    entry_id=f"instruction.{sequence}",
                    source_kind="instruction",
                    source_ref=call.plan_call_digest,
                    observed=ObservedValue(value=f"offline request fixture for {call.call_id}"),
                ),
            ),
        )
        contexts.append(context)
        provider_calls.append(
            ProviderCallEvidence(
                provider_call_id=f"provider.{sequence}",
                sequence_no=sequence,
                call_id=call.call_id,
                actor_id=call.actor_id,
                turn_index=0,
                attempt_index=0,
                context_id=context.context_id,
                context_digest=context.context_digest,
                deployment_id=epoch.deployment.deployment_id,
                provider=epoch.deployment.provider,
                model=epoch.deployment.model,
                provider_config_digest=epoch.deployment.provider_config_digest,
                request_digest=call.request_digest,
                status="succeeded",
                request_sent=True,
                response_id=f"rsp-{sequence}",
                response_digest=_digest(f"provider-response:{sequence}:{call.call_id}"),
                response_kind="terminal",
                started_at_ms=sequence,
                finished_at_ms=sequence + 1,
            )
        )

    patch_text = "diff --git a/parser.py b/parser.py\n+fix public parser regression\n"
    patch_digest = canonical_identity_digest(patch_text, domain="final-unified-diff")
    final_call_id = pilot.model_calls[-1].call_id
    artifact = ArtifactEvidence(
        artifact_id="artifact.final_patch",
        channel_id="patch",
        producer_call_id=final_call_id,
        artifact_schema="text",
        observed=ObservedValue(value=patch_text),
        payload_bytes=len(patch_text.encode("utf-8")),
        intended_consumer_call_ids=(),
        actual_consumer_call_ids=(),
    )
    tool_receipts = tuple(
        ToolReceiptEvidence(
            tool_call_id=f"public-tool.{sequence}",
            sequence_no=sequence,
            call_id=call.call_id,
            tool_id="repo.public_test",
            phase="terminal_public_verification",
            verification_step_id=call.step_id,
            invocation_digest=call.action_digest,
            receipt_id=f"receipt.public.{sequence}",
            receipt_digest=_digest(f"public-receipt:{sequence}:{call.step_id}"),
            status="succeeded",
            output_digest=_digest(f"public-output:{sequence}:{call.step_id}"),
            output_bytes=2,
            retry_index=0,
            started_at_ms=100 + sequence,
            finished_at_ms=101 + sequence,
        )
        for sequence, call in enumerate(pilot.public_verification_calls)
    )
    cost = _outcome_cost(
        model_calls=len(provider_calls),
        tool_calls=len(tool_receipts),
        patch_bytes=len(patch_text.encode("utf-8")),
    )
    return RunEvidence(
        evidence_id="run-evidence-p1",
        run_id="run.p1",
        execution_mode="deterministic_replay",
        live_inference_status="not_run",
        real_inference_requests_sent=0,
        data_state=task.data_state,
        epoch_id=epoch.epoch_id,
        epoch_manifest_digest=epoch.epoch_manifest_digest,
        release_digest=release.release_digest,
        release_manifest_digest=release.release_manifest_digest,
        profile_digest=release.profile_digest,
        split_manifest_digest=task.split_manifest_digest,
        pair_key=pair_key,
        task_manifest_digest=task.task_manifest_digest,
        protocol_digest=protocol_digest,
        compiled_semantic_digest=plan.compiled_semantic_digest,
        dependency_manifest_digest=dependencies.manifest_digest(),
        compiler_digest=dependencies.compiler.implementation_digest,
        kernel_digest=dependencies.kernel.implementation_digest,
        tool_manifest_digest=pilot.tool_manifest_digest,
        provider_config_digest=epoch.deployment.provider_config_digest,
        decoding_policy_digest=epoch.deployment.decoding_policy_digest,
        price_schedule_digest=epoch.deployment.price_schedule_digest,
        command_container_policy_digest=epoch.deployment.command_container_policy_digest,
        deployment_id=epoch.deployment.deployment_id,
        provider=epoch.deployment.provider,
        model=epoch.deployment.model,
        contexts=tuple(contexts),
        artifacts=(artifact,),
        deliveries=(),
        reads=(),
        routes=(),
        provider_calls=tuple(provider_calls),
        tool_receipts=tool_receipts,
        retries=(),
        cost_ledger=CostLedgerEvidence(
            cost=cost,
            provider_deadline_ms=task.ceilings.provider_deadline_ms,
            deadline_exceeded=False,
            active_reservations=0,
            reconciled=True,
        ),
        environment=environment,
        patch=PatchEvidence(
            status="emitted",
            observed=ObservedValue(value=patch_text),
            patch_digest=patch_digest,
            patch_bytes=len(patch_text.encode("utf-8")),
            artifact_id=artifact.artifact_id,
            public_verification_passed=True,
        ),
        termination=TerminationEvidence(
            reason="success",
            final_call_id=final_call_id,
            final_patch_digest=patch_digest,
            completed_at_ms=200,
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


def _solve_result(
    *,
    dependencies: RuntimeDependencyManifest,
    task: TaskEnvelope,
    release: ImmutableReleaseIdentity,
    epoch: ResearchEpochManifest,
    plan,
    run_evidence: RunEvidence,
    pair_digest: str,
) -> HarnessSolveResult:
    release_evidence_index_digest = _digest("release-evidence-index")
    evidence_index = HarnessRunEvidenceIndex(
        task_envelope_digest=task.task_manifest_digest,
        compiled_semantic_digest=plan.compiled_semantic_digest,
        release_evidence_index_digest=release_evidence_index_digest,
        provider_rounds=tuple(
            HarnessProviderRoundReference(
                call_id=call.call_id,
                turn_index=call.turn_index,
                request_digest=call.request_digest,
                reservation_id=f"reservation.{call.sequence_no}",
                status=call.status,
                response_id=call.response_id,
                response_digest=call.response_digest or _digest(f"missing-response:{call.call_id}"),
                usage_digest=_digest(f"usage:{call.provider_call_id}"),
                response_kind=call.response_kind or "terminal",
                tool_request_id=call.tool_request_id,
            )
            for call in run_evidence.provider_calls
        ),
        contexts=(),
        artifacts=(),
        artifact_deliveries=(),
        tool_receipts=tuple(
            HarnessToolReceiptReference(
                receipt_id=receipt.receipt_id,
                call_id=receipt.call_id,
                tool_id=receipt.tool_id,
                phase=receipt.phase,
                tool_request_id=receipt.tool_request_id,
                verification_step_id=receipt.verification_step_id,
                status=receipt.status,
                output_digest=receipt.output_digest,
                receipt_digest=receipt.receipt_digest,
                charged=True,
            )
            for receipt in run_evidence.tool_receipts
        ),
        public_verification_receipt_ids=tuple(
            receipt.receipt_id for receipt in run_evidence.tool_receipts
        ),
        public_command_evidence_digests=tuple(
            receipt.receipt_digest for receipt in run_evidence.tool_receipts
        ),
    )
    return HarnessSolveResult(
        run_id=run_evidence.run_id,
        workspace_id="workspace.p1",
        status="completed",
        execution_mode="deterministic_replay",
        live_inference_status="not_run",
        real_inference_requests_sent=0,
        release=HarnessReleaseExecutionIdentity(
            release_digest=release.release_digest,
            release_manifest_digest=release.release_manifest_digest,
            epoch_id=release.epoch_id,
            epoch_manifest_digest=release.epoch_manifest_digest,
            deployment=epoch.deployment,
            protocol_source_digest=release.protocol_source_digest,
            dependency_manifest_digest=release.dependency_manifest_digest,
            profile_digest=release.profile_digest,
            release_evidence_index_digest=release_evidence_index_digest,
        ),
        task=HarnessTaskExecutionIdentity(
            task_manifest_id=task.task_manifest_id,
            task_envelope_digest=task.task_manifest_digest,
            data_state=task.data_state,
            split_manifest_digest=task.split_manifest_digest,
            snapshot_id=task.workspace_snapshot.snapshot_id,
            snapshot_digest=task.workspace_snapshot.digest,
        ),
        compiled=HarnessCompiledExecutionIdentity(
            compiled_semantic_digest=plan.compiled_semantic_digest,
            compiler_dependency_id=dependencies.compiler.dependency_id,
            compiler_interface_version=dependencies.compiler.interface_version,
            compiler_implementation_digest=dependencies.compiler.implementation_digest,
            harness_contract_implementation_digest=dependencies.harness_contract.implementation_digest,
            kernel_implementation_digest=dependencies.kernel.implementation_digest,
        ),
        final_workspace_digest=_digest("final-workspace"),
        submitted_patch=HarnessSubmittedPatch(
            unified_diff=run_evidence.patch.observed.value,
            patch_digest=run_evidence.patch.patch_digest,
            byte_size=run_evidence.patch.patch_bytes,
        ),
        public_verification=HarnessPublicVerificationSummary(
            status="passed",
            receipt_ids=tuple(receipt.receipt_id for receipt in run_evidence.tool_receipts),
            command_evidence_digests=tuple(
                receipt.receipt_digest for receipt in run_evidence.tool_receipts
            ),
        ),
        termination=HarnessTerminationSummary(
            final_actor_call_id=run_evidence.termination.final_call_id or "",
            status="completed",
        ),
        budget=AggregateBudgetSnapshot(
            model_calls=run_evidence.cost_ledger.cost.model_calls,
            input_tokens=0,
            output_tokens=0,
            cached_tokens=0,
            known_cost_usd=0.0,
            estimated_cost_usd=0.0,
            unknown_cost_events=0,
            unknown_usage_events=0,
            tool_calls=run_evidence.cost_ledger.cost.tool_calls,
            tool_output_bytes=0,
            retries=0,
            latency_ms=1.0,
            elapsed_wall_time_ms=1,
            remaining_wall_time_ms=task.ceilings.max_wall_time_ms - 1,
            deadline_exceeded=False,
            active_reservations=0,
            healthy=True,
            reconciled=True,
            promotion_eligible=True,
        ),
        evidence=evidence_index,
        controlled_run_evidence=HarnessControlledRunEvidenceReference(
            evidence_id=run_evidence.evidence_id,
            evidence_digest=run_evidence.evidence_digest,
            pair_key_digest=pair_digest,
            runtime_environment_digest=run_evidence.environment.runtime_environment_digest,
            release_digest=release.release_digest,
            release_manifest_digest=release.release_manifest_digest,
            task_manifest_digest=task.task_manifest_digest,
            protocol_digest=release.protocol_source_digest,
            compiled_semantic_digest=plan.compiled_semantic_digest,
        ),
        eligible_for_evaluator_submission=True,
    )


def _outcome_receipt(
    *,
    receipt_id: str,
    protocol_digest: str,
    complete_repair: bool,
    dependencies: RuntimeDependencyManifest,
    epoch: ResearchEpochManifest,
    task: TaskEnvelope,
    release: ImmutableReleaseIdentity,
    pilot,
    pair_key: PairKey,
    evaluation_contract: PilotEvaluationContractEvidence,
    patch_digest: str,
    issued_at_ms: int,
) -> OutcomeReceipt:
    return OutcomeReceipt(
        receipt_id=receipt_id,
        execution_mode="deterministic_replay",
        live_inference_status="not_run",
        real_inference_requests_sent=0,
        data_state=task.data_state,
        epoch_id=epoch.epoch_id,
        epoch_manifest_digest=epoch.epoch_manifest_digest,
        release_digest=release.release_digest,
        release_manifest_digest=release.release_manifest_digest,
        profile_digest=release.profile_digest,
        split_manifest_digest=task.split_manifest_digest,
        task_manifest_id=task.task_manifest_id,
        task_manifest_digest=task.task_manifest_digest,
        evaluation_contract_id=evaluation_contract.evaluation_contract_id,
        evaluation_contract_digest=evaluation_contract.evaluation_contract_digest,
        evaluator_id=evaluation_contract.evaluator_id,
        evaluator_identity_digest=evaluation_contract.evaluator_identity_digest,
        evaluation_policy_digest=evaluation_contract.evaluation_policy_digest,
        pair_key=pair_key,
        protocol_digest=protocol_digest,
        compiler_digest=dependencies.compiler.implementation_digest,
        kernel_digest=dependencies.kernel.implementation_digest,
        tool_manifest_digest=pilot.tool_manifest_digest,
        provider_config_digest=epoch.deployment.provider_config_digest,
        decoding_policy_digest=epoch.deployment.decoding_policy_digest,
        price_schedule_digest=epoch.deployment.price_schedule_digest,
        command_container_policy_digest=epoch.deployment.command_container_policy_digest,
        evaluator_environment_digest=pilot.environment_digest,
        patch_digest=patch_digest,
        complete_repair=complete_repair,
        health=OutcomeHealth(
            process_integrity=True,
            no_leakage=True,
            environment_integrity=True,
            evaluator_integrity=True,
            accounting_complete=True,
        ),
        cost=_outcome_cost(
            model_calls=len(pilot.model_calls),
            tool_calls=len(pilot.public_verification_calls),
            patch_bytes=1,
        ),
        issued_at_ms=issued_at_ms,
    )


def _gate_wrapper(
    *,
    gate_id: str,
    evidence_kind: str,
    backing_artifact_path: str,
    backing_artifact_schema: str,
    backing_artifact: object,
) -> GateImplementationEvidence:
    backing_digest = _artifact_sha(backing_artifact)
    return GateImplementationEvidence(
        gate_id=gate_id,
        evidence_kind=evidence_kind,
        result_digest=backing_digest,
        backing_artifact_path=backing_artifact_path,
        backing_artifact_digest=backing_digest,
        backing_artifact_schema=backing_artifact_schema,
    )


def test_dry_run_manifest_freezes_exact_calls_and_guard_has_no_executor() -> None:
    dependencies, epoch, task, protocol, plan = _core()
    profile = _deployment_profile()
    release_manifest = HarnessReleaseManifest(
        release_digest=evidence_digest(
            {"kind": HARNESS_RELEASE_SCHEMA_VERSION, "files": {"placeholder": _digest("file")}}
        ),
        epoch_id=epoch.epoch_id,
        epoch_manifest_digest=epoch.epoch_manifest_digest,
        deployment=epoch.deployment,
        protocol_source_digest=protocol.source_digest(),
        compiled_semantic_digest=plan.compiled_semantic_digest,
        dependency_manifest_digest=dependencies.manifest_digest(),
        profile_digest=harness_deployment_profile_digest(profile),
        file_digests={"placeholder": _digest("file")},
    )
    pointer = ActiveReleasePointer(
        release_digest=release_manifest.release_digest,
        release_path=f"releases/{release_manifest.release_digest}",
        manifest_digest=release_manifest.manifest_digest,
    )
    audit = _audit(epoch, task)
    sessions = RuntimeSessionIdentitySet(
        sessions=(
            RuntimeSessionIdentityEvidence(
                role="same_release_continuation",
                session_id="session.continued",
                session_manifest_digest=_digest("session-cont"),
                active_release_digest=release_manifest.release_digest,
                message_count=1,
                last_message_id="message.one",
                bounded_public_carryover_count=1,
            ),
            RuntimeSessionIdentityEvidence(
                role="independent_new_session",
                session_id="session.new",
                session_manifest_digest=_digest("session-new"),
                active_release_digest=release_manifest.release_digest,
                message_count=0,
                bounded_public_carryover_count=0,
            ),
        )
    )
    manifest = _pilot_manifest(release_manifest, pointer, epoch, task, plan, audit, sessions)

    assert tuple(call.call_id for call in manifest.model_calls) == tuple(
        call.call_id for call in plan.actor_calls
    )
    assert len(manifest.public_verification_calls) == len(plan.public_verification.actions)
    assert manifest.budget.scheduled_model_calls == len(plan.actor_calls)
    assert manifest.budget.scheduled_tool_calls == 1
    assert manifest.budget.scheduled_evaluator_calls == 1
    assert manifest.live_status == "not_run"
    assert manifest.inference_requests_sent == 0
    assert all(not call.request_sent for call in manifest.model_calls)
    assert all(not call.call_sent for call in manifest.tool_calls)
    assert all(not call.call_sent for call in manifest.public_verification_calls)
    assert all(not call.call_sent for call in manifest.evaluator_calls)

    with pytest.raises(PilotEvidenceError, match="explicit authorization"):
        require_pilot_live_authorization(
            manifest,
            audit,
            deployment_profile=profile,
            live_authorized=False,
            credential_reference=_credential_reference(profile),
        )
    with pytest.raises(PilotEvidenceError):
        require_pilot_live_authorization(
            manifest,
            audit,
            deployment_profile=profile,
            live_authorized=True,
            credential_reference="env:PILOT_PROVIDER_API_KEY",  # type: ignore[arg-type]
        )
    with pytest.raises(PilotEvidenceError, match="endpoint policy"):
        require_pilot_live_authorization(
            manifest,
            audit,
            deployment_profile=profile,
            live_authorized=True,
            credential_reference=_credential_reference(
                profile,
                api_key_env="OTHER_API_KEY",
            ),
        )
    with pytest.raises(PilotEvidenceError, match="endpoint policy"):
        require_pilot_live_authorization(
            manifest,
            audit,
            deployment_profile=profile,
            live_authorized=True,
            credential_reference=_credential_reference(
                profile,
                api_key_file_env="OTHER_KEY_FILE",
            ),
        )
    crossed_payload = profile.model_dump(mode="python")
    crossed_payload["model"] = "other-offline-model"
    for digest_field in (
        "provider_config_digest",
        "decoding_policy_digest",
        "price_schedule_digest",
        "command_container_policy_digest",
    ):
        crossed_payload.pop(digest_field, None)
    crossed_profile = HarnessDeploymentProfile.model_validate(crossed_payload)
    with pytest.raises(PilotEvidenceError, match="deployment identity"):
        require_pilot_live_authorization(
            manifest,
            audit,
            deployment_profile=crossed_profile,
            live_authorized=True,
            credential_reference=_credential_reference(crossed_profile),
        )
    credential = _credential_reference(profile)
    authorization = require_pilot_live_authorization(
        manifest,
        audit,
        deployment_profile=profile,
        live_authorized=True,
        credential_reference=credential,
    )
    assert authorization.pilot_manifest_digest == manifest.manifest_digest
    assert authorization.deployment_profile == profile
    assert authorization.profile_digest == harness_deployment_profile_digest(profile)
    assert authorization.credential_reference == credential


def test_held_out_pilot_plan_is_distinct_from_release_representative_plan() -> None:
    dependencies, epoch, pilot_task, protocol, pilot_plan = _core()
    profile = _deployment_profile()
    representative_task = _task(epoch, "representative-search-task")
    representative_plan = compile_composite_run_plan(
        representative_task,
        protocol,
        dependencies,
    )
    assert representative_plan.compiled_semantic_digest != pilot_plan.compiled_semantic_digest
    release_manifest = HarnessReleaseManifest(
        release_digest=evidence_digest(
            {"kind": HARNESS_RELEASE_SCHEMA_VERSION, "files": {"placeholder": _digest("file")}}
        ),
        epoch_id=epoch.epoch_id,
        epoch_manifest_digest=epoch.epoch_manifest_digest,
        deployment=epoch.deployment,
        protocol_source_digest=protocol.source_digest(),
        compiled_semantic_digest=representative_plan.compiled_semantic_digest,
        dependency_manifest_digest=dependencies.manifest_digest(),
        profile_digest=harness_deployment_profile_digest(profile),
        file_digests={"placeholder": _digest("file")},
    )
    pointer = ActiveReleasePointer(
        release_digest=release_manifest.release_digest,
        release_path=f"releases/{release_manifest.release_digest}",
        manifest_digest=release_manifest.manifest_digest,
    )
    audit = _audit(epoch, pilot_task)
    sessions = RuntimeSessionIdentitySet(
        sessions=(
            RuntimeSessionIdentityEvidence(
                role="same_release_continuation",
                session_id="session.held-out",
                session_manifest_digest=_digest("session-held-out"),
                active_release_digest=release_manifest.release_digest,
                message_count=1,
                last_message_id="message.held-out",
                bounded_public_carryover_count=1,
            ),
            RuntimeSessionIdentityEvidence(
                role="independent_new_session",
                session_id="session.independent",
                session_manifest_digest=_digest("session-independent"),
                active_release_digest=release_manifest.release_digest,
                message_count=0,
                bounded_public_carryover_count=0,
            ),
        )
    )

    manifest = _pilot_manifest(
        release_manifest,
        pointer,
        epoch,
        pilot_task,
        pilot_plan,
        audit,
        sessions,
    )

    assert (
        manifest.release_representative_compiled_semantic_digest
        == representative_plan.compiled_semantic_digest
    )
    assert manifest.pilot_compiled_semantic_digest == pilot_plan.compiled_semantic_digest


def _packet_fixture():
    dependencies, epoch, task, protocol, plan = _core()
    profile = _deployment_profile()
    audit = _audit(epoch, task)
    protocol_digest = protocol.source_digest()
    founding_protocol_digest = _digest("founding-protocol")
    full_evaluation_contract = _full_evaluation_contract(epoch, task)
    evaluation_contract = PilotEvaluationContractEvidence.from_contract(
        full_evaluation_contract
    )
    environment = _environment_evidence(epoch, task)
    pair_key = PairKey(
        task_manifest_id=task.task_manifest_id,
        environment_id=environment.environment_id,
        sampling_replicate=0,
        provider_config_digest=epoch.deployment.provider_config_digest,
    )
    pair_digest = pair_key_digest(pair_key)
    gate0_manifest = build_gate0_dry_run_manifest(
        provider_identity=build_gate0_provider_identity(
            deployment_profile=profile,
        ),
        evidence_destination="controlled/gate0/provider-results.jsonl",
    )
    conformance = validate_gate0_dry_run_conformance(gate0_manifest)
    conformance_digest = gate0_conformance_report_digest(conformance)
    preregistration = Gate0PreregistrationPublic(
        preregistration_id="gate0.p1",
        panel_digest=gate0_manifest.panel.panel_digest,
        deterministic_suite_digest=conformance_digest,
        planned_provider_calls=gate0_manifest.total_provider_calls,
        frozen_thresholds={
            "intact_minimum": 0.70,
            "intact_minus_null_minimum": 0.30,
            "lower_bound_minimum": 0.15,
        },
    )
    gate0_report = Gate0NotRunReport(
        preregistration_digest=preregistration.preregistration_digest
    )
    pilot_summary = PilotNotRunSummary(
        pilot_id="pilot.p1",
        planned_task_manifest_digest=task.task_manifest_digest,
    )
    limitations = (
        "Real-provider Gate 0 has not been run.",
        "The reserved non-confirmatory pilot has not been run.",
        "No capability claim is authorized by offline implementation evidence.",
    )
    retained_lineage = PublicSearchLineageRecord(
        sequence_no=0,
        transaction_id="transaction.structural",
        operator="channel_add",
        parent_protocol_digest=founding_protocol_digest,
        child_protocol_digest=protocol_digest,
        transaction_digest=_digest("structural-transaction"),
        mechanism_hypothesis_digest=_digest("mechanism-hypothesis"),
        status="accepted",
    )

    public_relative: dict[str, bytes] = {
        "capability_epoch_public.json": _json(epoch_public_projection(epoch)),
        "protocol/source.json": _json(protocol.model_dump(mode="json")),
        "protocol/compiled_plan.json": _json(plan.model_dump(mode="json")),
        "protocol/consumed_field_liveness_manifest.json": _json(
            plan.liveness_manifest.model_dump(mode="json")
        ),
        "runtime/dependency_manifest.json": _json(dependencies.model_dump(mode="json")),
        "search/transaction_lineage_public.jsonl": _jsonl(
            retained_lineage.model_dump(mode="json")
        ),
        "search/selection_decisions_public.jsonl": _jsonl(
            {"sequence_no": 0, "decision_digest": _digest("public-selection")}
        ),
        "gate0_preregistration.json": _json(preregistration.model_dump(mode="json")),
        "gate0_report.json": _json(gate0_report.model_dump(mode="json")),
        "pilot_summary.json": _json(pilot_summary.model_dump(mode="json")),
        "limitations.md": (
            "# Limitations\n\n" + "".join(f"- {item}\n" for item in limitations)
        ).encode("utf-8"),
    }
    index = PublicEvidenceIndex(
        protocol_source_digest=protocol.source_digest(),
        compiled_semantic_digest=plan.compiled_semantic_digest,
        dependency_manifest_digest=dependencies.manifest_digest(),
        epoch_manifest_digest=epoch.epoch_manifest_digest,
        profile_digest=harness_deployment_profile_digest(profile),
        artifacts={path: hashlib.sha256(raw).hexdigest() for path, raw in public_relative.items()},
    )
    public_relative["evidence_index.json"] = _json(index.model_dump(mode="json"))
    release_files = {
        f"{PUBLIC_RELEASE_EVIDENCE_DIR}/{path}": hashlib.sha256(raw).hexdigest()
        for path, raw in public_relative.items()
    }
    release_digest = evidence_digest(
        {"kind": HARNESS_RELEASE_SCHEMA_VERSION, "files": dict(sorted(release_files.items()))}
    )
    release_manifest = HarnessReleaseManifest(
        release_digest=release_digest,
        epoch_id=epoch.epoch_id,
        epoch_manifest_digest=epoch.epoch_manifest_digest,
        deployment=epoch.deployment,
        protocol_source_digest=protocol.source_digest(),
        compiled_semantic_digest=plan.compiled_semantic_digest,
        dependency_manifest_digest=dependencies.manifest_digest(),
        profile_digest=harness_deployment_profile_digest(profile),
        file_digests=release_files,
    )
    public_relative["release_manifest.json"] = _json(release_manifest.model_dump(mode="json"))
    pointer = ActiveReleasePointer(
        release_digest=release_manifest.release_digest,
        release_path=f"releases/{release_manifest.release_digest}",
        manifest_digest=release_manifest.manifest_digest,
    )
    release = ImmutableReleaseIdentity.from_records(pointer, release_manifest)

    continued_session = HarnessSessionManifest(
        session_id="session.continued",
        project_dir=".",
        active_release_digest=release.release_digest,
        created_at_ms=70,
        version=1,
        message_count=1,
        next_sequence=1,
        last_message_id="message.one",
    )
    new_session = HarnessSessionManifest(
        session_id="session.new",
        project_dir=".",
        active_release_digest=release.release_digest,
        created_at_ms=71,
    )
    sessions = RuntimeSessionIdentitySet(
        sessions=(
            RuntimeSessionIdentityEvidence.from_manifest(
                continued_session,
                role="same_release_continuation",
            ),
            RuntimeSessionIdentityEvidence.from_manifest(
                new_session,
                role="independent_new_session",
            ),
        )
    )
    pilot = _pilot_manifest(
        release_manifest,
        pointer,
        epoch,
        task,
        plan,
        audit,
        sessions,
        evaluation_contract_digest=evaluation_contract.evaluation_contract_digest,
        environment_digest=environment.runtime_environment_digest,
        run_pair_digest=pair_digest,
    )
    run_evidence = _run_evidence(
        dependencies=dependencies,
        epoch=epoch,
        task=task,
        release=release,
        protocol_digest=protocol_digest,
        plan=plan,
        pilot=pilot,
        pair_key=pair_key,
        environment=environment,
    )
    solve_result = _solve_result(
        dependencies=dependencies,
        task=task,
        release=release,
        epoch=epoch,
        plan=plan,
        run_evidence=run_evidence,
        pair_digest=pair_digest,
    )
    pilot_report = PilotNotRunDevelopmentReport(
        pilot_id=pilot.pilot_id,
        pilot_manifest_digest=pilot.manifest_digest,
        task_audit_manifest_digest=audit.audit_manifest_digest,
        reserved_task_manifest_digest=task.task_manifest_digest,
    )
    solve_execution = OfflineSolveExecutionProvenance(
        provenance_id="solve-replay.p1",
        task_manifest_digest=task.task_manifest_digest,
        protocol_source_digest=protocol_digest,
        pilot_compiled_semantic_digest=plan.compiled_semantic_digest,
        environment_digest=pilot.environment_digest,
        replay_fixture_digest=run_evidence.evidence_digest,
        solve_result_digest=solve_result.result_digest,
    )
    gate0 = Gate0OfflineReadiness.from_records(
        manifest=gate0_manifest,
        conformance=conformance,
        preregistration=preregistration,
        live_report=gate0_report,
    )
    d0_manifest = DevelopmentTaskFeasibilityManifest(
        manifest_id="d0.p1",
        epoch_id=epoch.epoch_id,
        epoch_manifest_digest=epoch.epoch_manifest_digest,
        task_manifest_id=task.task_manifest_id,
        task_manifest_digest=task.task_manifest_digest,
        evaluation_contract_id=evaluation_contract.evaluation_contract_id,
        evaluation_contract_digest=evaluation_contract.evaluation_contract_digest,
        execution_backend_id="offline-fixture-backend",
        execution_backend_digest=_digest("isolated-fixture-backend"),
        controls=(
            FeasibilityControlResult(
                control_id="known-good",
                control_kind="known_good",
                artifact_digest=_digest("known-good-artifact"),
                expected_complete_repair=True,
                observed_complete_repair=True,
                evaluator_status="passed",
                outcome_fingerprint=_digest("known-good-outcome"),
                replay_fingerprint=_digest("known-good-replay"),
                reproducible=True,
                source_snapshot_unchanged=True,
                scratch_snapshot_matched=True,
                fixture_identity_matched=True,
                protected_tamper_detected=False,
                passed=True,
            ),
        ),
        clean_replay_reproducible=True,
        protected_path_integrity=True,
        leakage_integrity=True,
        identity_integrity=True,
        offline_controls_passed=True,
        baseline_headroom=BaselineHeadroomAssessment(
            status="not_measured",
            receipt_count=0,
            complete_repairs=0,
            failures=0,
        ),
        paired_search_projection=PairedSearchBudgetProjection(
            structural_candidate_capacity=1,
            frozen_candidate_budget=1,
            projected_candidate_evaluations=1,
            sampling_replicates=1,
            projected_paired_outcome_runs=2,
            projected_max_model_calls=task.ceilings.max_model_calls,
            projected_max_known_cost_usd=task.ceilings.max_known_cost_usd,
            projected_max_estimated_cost_usd=task.ceilings.max_estimated_cost_usd,
            frozen_max_model_calls=task.ceilings.max_model_calls,
            frozen_max_known_cost_usd=task.ceilings.max_known_cost_usd,
            frozen_max_estimated_cost_usd=task.ceilings.max_estimated_cost_usd,
            fits_frozen_epoch_budget=True,
        ),
        provider_baseline_dry_run=ProviderBaselineDryRun(
            deployment_id=epoch.deployment.deployment_id,
            provider=epoch.deployment.provider,
            model=epoch.deployment.model,
            provider_config_digest=epoch.deployment.provider_config_digest,
            baseline_protocol_digest=founding_protocol_digest,
            pair_keys=(pair_key,),
            planned_provider_calls=len(pilot.model_calls),
            projected_max_known_cost_usd=task.ceilings.max_known_cost_usd,
            projected_max_estimated_cost_usd=task.ceilings.max_estimated_cost_usd,
        ),
        status="pending_real_provider_baseline",
        search_authorized=False,
        reason_codes=("provider_baseline_not_run",),
    )
    d0 = D0FixtureFeasibilityEvidence.from_manifest(d0_manifest)
    s1 = S1OfflineRetentionEvidence(
        search_result_digest=_digest("s1-search-result"),
        epoch_manifest_digest=epoch.epoch_manifest_digest,
        task_panel_digest=epoch.search_envelope.task_panel_digest,
        founding_protocol_digest=founding_protocol_digest,
        final_protocol_digest=protocol_digest,
        execution_status="completed",
        retained_children=1,
        retained_structural_transactions=(
            RetainedStructuralTransactionEvidence(
                transaction_id=retained_lineage.transaction_id,
                transaction_digest=retained_lineage.transaction_digest,
                operator=retained_lineage.operator,
                parent_protocol_digest=retained_lineage.parent_protocol_digest,
                child_protocol_digest=retained_lineage.child_protocol_digest,
            ),
        ),
    )
    factory_message = HarnessFactoryMessage(
        chat_id="chat.p1",
        message_id="message.followup",
        message_index=1,
        parent_message_id="message.initial",
        prior_active_release_digest=_digest("prior-release"),
        new_release_digest=release.release_digest,
        new_manifest_digest=release.release_manifest_digest,
        new_protocol_digest=release.protocol_source_digest,
        compiled_semantic_digest=release.representative_compiled_semantic_digest,
        dependency_manifest_digest=release.dependency_manifest_digest,
        epoch_manifest_digest=release.epoch_manifest_digest,
        prompt_text="Package the retained public protocol into an immutable MVP release.",
        search_result_digest=s1.search_result_digest,
        selection_evidence_digests=(_digest("selection-evidence"),),
        transaction_id="factory.transaction",
        created_at_ms=80,
    )
    factory_chat = HarnessFactoryChatManifest(
        chat_id=factory_message.chat_id,
        project_root=".",
        epoch_manifest_digest=release.epoch_manifest_digest,
        active_release_digest=release.release_digest,
        active_manifest_digest=release.release_manifest_digest,
        active_protocol_digest=release.protocol_source_digest,
        created_at_ms=79,
        message_count=2,
        last_message_id=factory_message.message_id,
    )
    factory = FactoryFollowupIdentityEvidence.from_records(
        chat=factory_chat,
        message=factory_message,
    )
    parent_receipt = _outcome_receipt(
        receipt_id="outcome.parent",
        protocol_digest=founding_protocol_digest,
        complete_repair=False,
        dependencies=dependencies,
        epoch=epoch,
        task=task,
        release=release,
        pilot=pilot,
        pair_key=pair_key,
        evaluation_contract=evaluation_contract,
        patch_digest=_digest("parent-patch"),
        issued_at_ms=300,
    )
    child_receipt = _outcome_receipt(
        receipt_id="outcome.child",
        protocol_digest=protocol_digest,
        complete_repair=True,
        dependencies=dependencies,
        epoch=epoch,
        task=task,
        release=release,
        pilot=pilot,
        pair_key=pair_key,
        evaluation_contract=evaluation_contract,
        patch_digest=run_evidence.patch.patch_digest,
        issued_at_ms=301,
    )
    raw_pair = PilotRawPairedOutcomeRecord(
        pair_key_digest=pair_digest,
        parent_receipt_digest=parent_receipt.receipt_digest,
        child_receipt_digest=child_receipt.receipt_digest,
        parent_complete_repair=parent_receipt.complete_repair,
        child_complete_repair=child_receipt.complete_repair,
    )
    intervention = PilotContentNullInterventionEvidence(
        intervention_id="content-null.p1",
        paired_outcome_digest=_raw_paired_outcomes_digest((raw_pair,)),
        neutral_artifact_digest=_digest("neutral-artifact"),
    )

    artifacts: dict[str, object] = {
        f"{PUBLIC_RELEASE_EVIDENCE_DIR}/{path}": raw
        for path, raw in public_relative.items()
    }
    run_root = f"{CONTROLLED_EVIDENCE_DIR}/runs/{pair_digest}"
    artifacts.update(
        {
            f"{CONTROLLED_EVIDENCE_DIR}/evaluation_contract.json": evaluation_contract,
            f"{CONTROLLED_EVIDENCE_DIR}/task_public_manifest.json": task_envelope_public_projection(task),
            f"{CONTROLLED_EVIDENCE_DIR}/evaluator/task_audit_manifest.json": audit,
            f"{CONTROLLED_EVIDENCE_DIR}/evaluator/outcome_receipts.jsonl": _jsonl(
                parent_receipt.model_dump(mode="json"),
                child_receipt.model_dump(mode="json"),
            ),
            f"{CONTROLLED_EVIDENCE_DIR}/gate0/dry_run_manifest.json": gate0_manifest,
            f"{CONTROLLED_EVIDENCE_DIR}/gate0/deterministic_conformance_report.json": conformance,
            f"{CONTROLLED_EVIDENCE_DIR}/d0/fixture_feasibility_manifest.json": d0_manifest,
            f"{CONTROLLED_EVIDENCE_DIR}/d0/fixture_feasibility.json": d0,
            f"{CONTROLLED_EVIDENCE_DIR}/search/s1_offline_retention.json": s1,
            f"{CONTROLLED_EVIDENCE_DIR}/pilot/compiled_plan.json": plan,
            f"{CONTROLLED_EVIDENCE_DIR}/pilot/dry_run_manifest.json": pilot,
            f"{CONTROLLED_EVIDENCE_DIR}/factory/chat_manifest.json": factory_chat,
            f"{CONTROLLED_EVIDENCE_DIR}/factory/followup_message.json": factory_message,
            f"{CONTROLLED_EVIDENCE_DIR}/factory/followup_transaction_identity.json": factory,
            f"{CONTROLLED_EVIDENCE_DIR}/sessions/same_release_continuation.json": continued_session,
            f"{CONTROLLED_EVIDENCE_DIR}/sessions/independent_new_session.json": new_session,
            f"{CONTROLLED_EVIDENCE_DIR}/sessions/session_identities.json": sessions,
            f"{CONTROLLED_EVIDENCE_DIR}/interventions/content_null_manifest.json": intervention,
            f"{CONTROLLED_EVIDENCE_DIR}/analysis/raw_paired_outcomes.jsonl": _jsonl(
                raw_pair.model_dump(mode="json")
            ),
            f"{CONTROLLED_EVIDENCE_DIR}/analysis/offline_solve_execution_provenance.json": solve_execution,
            f"{CONTROLLED_EVIDENCE_DIR}/analysis/pilot_report.json": pilot_report,
            f"{run_root}/run_manifest.json": {
                "run_id": run_evidence.run_id,
                "run_evidence_digest": run_evidence.evidence_digest,
                "harness_solve_result_digest": solve_result.result_digest,
            },
            f"{run_root}/{HARNESS_SOLVE_RESULT_FILE}": solve_result,
            f"{run_root}/{CONTROLLED_RUN_EVIDENCE_REF}": run_evidence,
            f"{run_root}/tool_and_side_effect_receipts.jsonl": _jsonl(
                *[receipt.model_dump(mode="json") for receipt in run_evidence.tool_receipts]
            ),
        }
    )
    artifacts.update(
        {
            f"{run_root}/pre_call_contexts/{context.context_id}.json": context
            for context in run_evidence.contexts
        }
    )
    artifacts.update(
        {
            f"{run_root}/artifacts/{artifact.artifact_id}.json": artifact
            for artifact in run_evidence.artifacts
        }
    )
    for index_no, gate_id in enumerate(REQUIRED_MVP_GATES):
        kind = (
            "immutable_identity"
            if gate_id in {"F1a", "F1b", "F1c"}
            else "offline_conformance"
            if gate_id in {"G0", "D0", "S1"}
            else "deterministic_test"
        )
        if gate_id == "G0":
            backing_path = f"{CONTROLLED_EVIDENCE_DIR}/gate0/deterministic_conformance_report.json"
            backing_schema = "gate0_conformance"
            backing = conformance
        elif gate_id == "D0":
            backing_path = f"{CONTROLLED_EVIDENCE_DIR}/d0/fixture_feasibility.json"
            backing_schema = "d0_feasibility"
            backing = d0
        elif gate_id == "S1":
            backing_path = f"{CONTROLLED_EVIDENCE_DIR}/search/s1_offline_retention.json"
            backing_schema = "s1_retention"
            backing = s1
        elif gate_id == "F1b":
            backing_path = f"{CONTROLLED_EVIDENCE_DIR}/factory/followup_message.json"
            backing_schema = "factory_followup_message"
            backing = factory_message
        elif gate_id == "F1c":
            backing_path = f"{CONTROLLED_EVIDENCE_DIR}/sessions/session_identities.json"
            backing_schema = "runtime_session_identity_set"
            backing = sessions
        else:
            backing_path = f"{CONTROLLED_EVIDENCE_DIR}/gates/backing/{gate_id}.json"
            backing_schema = "deterministic_test_evidence"
            backing = PilotGateDeterministicTestEvidence(
                gate_id=gate_id,
                test_id=f"gate.{gate_id.lower()}",
                test_command="pytest tests/mvp/test_p1_pilot_evidence.py",
                test_result_digest=_digest(f"gate-test:{index_no}:{gate_id}"),
            )
            artifacts[backing_path] = backing
        artifacts[f"{CONTROLLED_EVIDENCE_DIR}/gates/{gate_id}.json"] = _gate_wrapper(
            gate_id=gate_id,
            evidence_kind=kind,
            backing_artifact_path=backing_path,
            backing_artifact_schema=backing_schema,
            backing_artifact=backing,
        )

    packet = build_mvp_readiness_evidence_packet(
        packet_id="mvp-readiness.p1",
        release=release,
        gate0=gate0,
        d0=d0,
        s1=s1,
        solve_execution=solve_execution,
        task_audit=audit,
        pilot_dry_run=pilot,
        pilot_report=pilot_report,
        factory_followup=factory,
        runtime_sessions=sessions,
        limitations=limitations,
        artifacts=artifacts,
    )
    return packet, artifacts


def _packet_from_artifacts(
    packet,
    artifacts: dict[str, object],
    *,
    packet_id: str,
    solve_execution: OfflineSolveExecutionProvenance | None = None,
):
    return build_mvp_readiness_evidence_packet(
        packet_id=packet_id,
        release=packet.release,
        gate0=packet.gate0,
        d0=packet.d0,
        s1=packet.s1,
        solve_execution=solve_execution or packet.solve_execution,
        task_audit=artifacts[f"{CONTROLLED_EVIDENCE_DIR}/evaluator/task_audit_manifest.json"],
        pilot_dry_run=artifacts[f"{CONTROLLED_EVIDENCE_DIR}/pilot/dry_run_manifest.json"],
        pilot_report=artifacts[f"{CONTROLLED_EVIDENCE_DIR}/analysis/pilot_report.json"],
        factory_followup=packet.factory_followup,
        runtime_sessions=packet.runtime_sessions,
        limitations=packet.limitations,
        artifacts=artifacts,
    )


def test_packet_layout_atomic_replay_tamper_and_leakage_fail_closed(tmp_path: Path) -> None:
    packet, artifacts = _packet_fixture()
    generation = write_mvp_readiness_evidence_packet(
        tmp_path / "packets",
        packet=packet,
        artifacts=artifacts,
        canary_values=(CANARY,),
    )
    replayed = replay_mvp_readiness_evidence_packet(generation, canary_values=(CANARY,))
    assert replayed == packet
    assert generation.name == packet.packet_digest
    assert packet.implementation_ready is True
    assert packet.capability_claim_authorized is False
    assert packet.live_gate0_status == "not_run"
    assert packet.live_pilot_status == "not_run"
    assert packet.inference_requests_sent == 0
    assert write_mvp_readiness_evidence_packet(
        tmp_path / "packets",
        packet=packet,
        artifacts=artifacts,
        canary_values=(CANARY,),
    ) == generation

    tampered = generation / CONTROLLED_EVIDENCE_DIR / "analysis" / "pilot_report.json"
    tampered.write_text('{"status":"passed"}\n', encoding="utf-8")
    with pytest.raises(PilotEvidenceError, match="replay mismatch"):
        replay_mvp_readiness_evidence_packet(generation, canary_values=(CANARY,))

    leaked_packet, leaked_artifacts = _packet_fixture()
    leaked_artifacts[f"{CONTROLLED_EVIDENCE_DIR}/analysis/pilot_report.json"] = {
        "status": "not_run",
        "note": CANARY,
    }
    with pytest.raises(PilotEvidenceError, match="bytes do not match"):
        write_mvp_readiness_evidence_packet(
            tmp_path / "leaked",
            packet=leaked_packet,
            artifacts=leaked_artifacts,
            canary_values=(CANARY,),
        )


def test_packet_rejects_crossed_gate_backing_artifact() -> None:
    packet, artifacts = _packet_fixture()
    bad_artifacts = dict(artifacts)
    gate_path = f"{CONTROLLED_EVIDENCE_DIR}/gates/G0.json"
    gate = bad_artifacts[gate_path]
    bad_artifacts[gate_path] = gate.model_copy(
        update={
            "backing_artifact_path": f"{CONTROLLED_EVIDENCE_DIR}/pilot/dry_run_manifest.json",
        }
    )

    with pytest.raises(PilotEvidenceError, match="backing artifact digest mismatch"):
        _packet_from_artifacts(
            packet,
            bad_artifacts,
            packet_id="mvp-readiness.crossed-gate-backing",
        )


def test_packet_replay_rejects_untyped_outcome_rows(tmp_path: Path) -> None:
    packet, artifacts = _packet_fixture()
    bad_artifacts = dict(artifacts)
    bad_artifacts[f"{CONTROLLED_EVIDENCE_DIR}/evaluator/outcome_receipts.jsonl"] = _jsonl(
        {"receipt_digest": _digest("untyped-outcome"), "source": "placeholder"}
    )
    bad_packet = _packet_from_artifacts(
        packet,
        bad_artifacts,
        packet_id="mvp-readiness.bad-outcomes",
    )

    with pytest.raises(PilotEvidenceError, match="outcome receipts are not typed"):
        write_mvp_readiness_evidence_packet(
            tmp_path / "bad-outcomes",
            packet=bad_packet,
            artifacts=bad_artifacts,
            canary_values=(CANARY,),
        )


def test_packet_replay_rejects_crossed_controlled_run_reference(tmp_path: Path) -> None:
    packet, artifacts = _packet_fixture()
    bad_artifacts = dict(artifacts)
    solve_path = next(path for path in artifacts if path.endswith(HARNESS_SOLVE_RESULT_FILE))
    solve = artifacts[solve_path]
    crossed_reference = solve.controlled_run_evidence.model_copy(
        update={"runtime_environment_digest": _digest("wrong-runtime-environment")}
    )
    crossed_solve = HarnessSolveResult.model_validate(
        {
            **solve.model_dump(mode="python"),
            "controlled_run_evidence": crossed_reference.model_dump(mode="python"),
            "result_digest": "",
        }
    )
    bad_artifacts[solve_path] = crossed_solve
    bad_solve_execution = OfflineSolveExecutionProvenance.model_validate(
        {
            **packet.solve_execution.model_dump(mode="python"),
            "provenance_digest": "",
            "solve_result_digest": crossed_solve.result_digest,
        }
    )
    bad_artifacts[
        f"{CONTROLLED_EVIDENCE_DIR}/analysis/offline_solve_execution_provenance.json"
    ] = bad_solve_execution
    bad_packet = _packet_from_artifacts(
        packet,
        bad_artifacts,
        packet_id="mvp-readiness.crossed-run-reference",
        solve_execution=bad_solve_execution,
    )

    with pytest.raises(PilotEvidenceError, match="crossed environment"):
        write_mvp_readiness_evidence_packet(
            tmp_path / "crossed-run-reference",
            packet=bad_packet,
            artifacts=bad_artifacts,
            canary_values=(CANARY,),
        )


def test_packet_replay_rejects_d0_projection_not_rebuilt_from_source(tmp_path: Path) -> None:
    packet, artifacts = _packet_fixture()
    bad_artifacts = dict(artifacts)
    source_path = f"{CONTROLLED_EVIDENCE_DIR}/d0/fixture_feasibility_manifest.json"
    source = artifacts[source_path]
    crossed_source = DevelopmentTaskFeasibilityManifest.model_validate(
        {
            **source.model_dump(mode="python"),
            "manifest_digest": "",
            "execution_backend_digest": _digest("crossed-d0-backend"),
        }
    )
    bad_artifacts[source_path] = crossed_source
    bad_packet = _packet_from_artifacts(
        packet,
        bad_artifacts,
        packet_id="mvp-readiness.crossed-d0-source",
    )

    with pytest.raises(PilotEvidenceError, match="D0 fixture feasibility source crossed"):
        write_mvp_readiness_evidence_packet(
            tmp_path / "crossed-d0-source",
            packet=bad_packet,
            artifacts=bad_artifacts,
            canary_values=(CANARY,),
        )


def test_packet_refuses_missing_duplicate_and_fake_live_pass() -> None:
    packet, artifacts = _packet_fixture()
    missing = dict(artifacts)
    missing.pop(f"{CONTROLLED_EVIDENCE_DIR}/gates/R2.json")
    with pytest.raises(PilotEvidenceError, match="missing implementation evidence"):
        build_mvp_readiness_evidence_packet(
            packet_id="mvp-readiness.missing",
            release=packet.release,
            gate0=packet.gate0,
            d0=packet.d0,
            s1=packet.s1,
            solve_execution=packet.solve_execution,
            task_audit=type(next(value for key, value in artifacts.items() if key.endswith("task_audit_manifest.json"))).model_validate(
                next(value for key, value in artifacts.items() if key.endswith("task_audit_manifest.json")).model_dump(mode="python")
            ),
            pilot_dry_run=next(value for key, value in artifacts.items() if key.endswith("pilot/dry_run_manifest.json")),
            pilot_report=next(value for key, value in artifacts.items() if key.endswith("analysis/pilot_report.json")),
            factory_followup=packet.factory_followup,
            runtime_sessions=packet.runtime_sessions,
            limitations=packet.limitations,
            artifacts=missing,
        )

    payload = packet.model_dump(mode="python")
    payload["live_pilot_status"] = "passed"
    with pytest.raises(ValidationError):
        type(packet).model_validate(payload)
    payload = packet.model_dump(mode="python")
    payload["capability_claim_authorized"] = True
    with pytest.raises(ValidationError):
        type(packet).model_validate(payload)

    duplicate = dict(packet.file_digests)
    first_path, second_path = list(duplicate)[:2]
    duplicate[second_path] = duplicate[first_path]
    with pytest.raises(ValidationError, match="duplicate identical"):
        type(packet).model_validate({**packet.model_dump(mode="python"), "file_digests": duplicate, "packet_digest": ""})
