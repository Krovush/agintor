from __future__ import annotations

import hashlib
from typing import Any

import pytest
from pydantic import ValidationError

from agintor.contracts.epochs import (
    DeploymentIdentity,
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
from agintor.contracts.harness import (
    DependencyRef,
    RuntimeDependencyManifest,
    TrustedToolDependency,
)
from agintor.contracts.outcomes import OutcomeHealth, OutcomeReceipt, PairKey
from agintor.contracts.run_evidence import (
    EnvironmentEvidence,
    ToolReceiptEvidence,
    runtime_environment_evidence_digest,
)
from agintor.runtime.api.composite_compiler import (
    compile_composite_run_plan,
    load_canonical_harness_seed,
)
from agintor.runtime.evidence import (
    EvidenceAssemblyError,
    PartialCompositeRunObservation,
    ProviderCallDetail,
    PublicVerificationEvidence,
    assemble_run_evidence,
    bind_and_append_evaluator_proof,
    public_verification_action_digest,
    public_verification_plan_digest,
    tool_manifest_digest,
)
from agintor.runtime.kernel.composite_budget import (
    CostStatus,
    ProviderUsageReport,
    UsageStatus,
)
from agintor.runtime.kernel.composite_provider import (
    ProviderCallControl,
    ProviderInvocation,
)
from agintor.runtime.kernel.composite_runtime import (
    ActorCallOutput,
    ActorCallRequest,
    CompositeRuntime,
    ScratchWorkspaceBinding,
)
from agintor.storage.proof_records import ImmutableProofRecordStore
from agintor.utils import count_tokens_rough


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _task() -> TaskEnvelope:
    return TaskEnvelope(
        task_manifest_id="task.bridge",
        epoch_id="epoch.bridge",
        epoch_manifest_digest=_digest("placeholder-epoch"),
        data_state="development",
        split_manifest_digest=_digest("development"),
        issue="Repair the public parser regression.",
        workspace_snapshot=WorkspaceSnapshotRef(
            snapshot_id="snapshot.bridge",
            uri="public/snapshot",
            digest=_digest("snapshot"),
        ),
        public_reproduction=(
            PublicReproductionStep(
                step_id="public-test",
                argv=("python", "-m", "pytest", "-q"),
                timeout_ms=10_000,
            ),
        ),
        ceilings=TaskCeilings(
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


def _epoch_for(task: TaskEnvelope, dependencies: RuntimeDependencyManifest) -> ResearchEpochManifest:
    epoch = ResearchEpochManifest(
        epoch_id=task.epoch_id,
        task_manifest_digest=_digest("task-panel"),
        development_split_digest=task.split_manifest_digest,
        sealed_confirmation_split_digest=_digest("confirmation"),
        deployment=DeploymentIdentity(
            deployment_id="deployment.bridge",
            provider="offline-provider",
            model="offline-model",
            provider_config_digest=_digest("provider-config"),
            decoding_policy_digest=_digest("decoding"),
            price_schedule_digest=_digest("prices"),
            command_container_policy_digest=_digest("command-container-policy"),
        ),
        per_run_ceilings=task.ceilings,
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
                    tool.implementation_digest
                    for tool in dependencies.trusted_tools
                    if tool.tool_id == tool_id
                ),
                policy_digest=next(
                    tool.policy_digest
                    for tool in dependencies.trusted_tools
                    if tool.tool_id == tool_id
                ),
            )
            for tool_id in REPO_REPAIR_TRUSTED_TOOL_IDS
        ),
        stop_rule=StopRule(
            max_candidate_evaluations=1,
            max_consecutive_non_improving_steps=1,
        ),
        evaluator_authority=EvaluatorAuthority(
            evaluator_id="repo-evaluator.bridge",
            evaluator_identity_digest=_digest("evaluator"),
            evaluation_policy_digest=_digest("evaluation-policy"),
        ),
    )
    return epoch


def _bound_task_epoch_plan():
    draft = _task()
    dependencies = _dependencies(draft)
    epoch = _epoch_for(draft, dependencies)
    task = draft.model_copy(
        update={
            "epoch_manifest_digest": epoch.epoch_manifest_digest,
            "task_manifest_digest": "",
        }
    )
    task = TaskEnvelope.model_validate(
        task.model_dump(mode="python", exclude={"task_manifest_digest"})
    )
    plan = compile_composite_run_plan(
        task,
        load_canonical_harness_seed().protocol,
        dependencies,
    )
    return task, epoch, plan


def _patch() -> str:
    return (
        "--- a/pkg/example.py\n"
        "+++ b/pkg/example.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+fixed\n"
    )


class RecordingProvider:
    def __init__(self) -> None:
        self.requests: list[ActorCallRequest] = []
        self.response_ids: list[str] = []

    def invoke(
        self,
        request: Any,
        *,
        control: ProviderCallControl,
        credential_reference: Any,
    ) -> ProviderInvocation:
        normalized = ActorCallRequest.model_validate(request)
        self.requests.append(normalized)
        response_id = f"offline.{normalized.call_id}"
        self.response_ids.append(response_id)
        if normalized.call_id == "actor.investigator.initial":
            output = ActorCallOutput(
                output_text="Investigation complete.",
                artifact_payloads={
                    "artifact.investigation": "Parser normalization drops the sentinel."
                },
            )
        else:
            output = ActorCallOutput(
                output_text="Repair complete.",
                final_patch=_patch(),
            )
        return ProviderInvocation(
            response=output,
            usage=ProviderUsageReport(
                usage_status=UsageStatus.KNOWN,
                input_tokens=normalized.input_token_estimate,
                output_tokens=count_tokens_rough(output.model_dump_json()),
                cached_tokens=0,
                cost_status=CostStatus.KNOWN,
                cost_usd=0.0,
                response_id=response_id,
            ),
        )


def _execute():
    task, epoch, plan = _bound_task_epoch_plan()
    provider = RecordingProvider()
    result = CompositeRuntime(
        plan,
        task,
        ScratchWorkspaceBinding(
            workspace_id="scratch.bridge",
            workspace_digest=task.workspace_snapshot.digest,
        ),
        provider,
        run_id="run.bridge",
    ).run()
    return task, epoch, plan, provider, result


def _provider_details(epoch, provider, result) -> tuple[ProviderCallDetail, ...]:
    actor_calls = {call.call_id: call for call in result.actor_calls}
    contexts = {context.call_id: context for context in result.context_manifests}
    details = []
    for sequence_no, request in enumerate(provider.requests, start=1):
        call = actor_calls[request.call_id]
        round_ = call.provider_rounds[0]
        details.append(
            ProviderCallDetail(
                provider_call_id=f"provider.{sequence_no}",
                sequence_no=sequence_no,
                call_id=request.call_id,
                actor_id=request.actor_id,
                turn_index=round_.turn_index,
                attempt_index=0,
                runtime_context_manifest_digest=contexts[request.call_id].manifest_digest,
                reservation_id=call.provider_reservation_id,
                deployment_id=epoch.deployment.deployment_id,
                provider=epoch.deployment.provider,
                model=epoch.deployment.model,
                provider_config_digest=epoch.deployment.provider_config_digest,
                request_digest=request.request_digest,
                status="succeeded",
                request_sent=True,
                response_id=provider.response_ids[sequence_no - 1],
                response_digest=round_.response_digest,
                response_kind=round_.response_kind,
                tool_request_id=round_.tool_request_id,
                started_at_ms=round_.started_at_ms,
                finished_at_ms=round_.finished_at_ms,
            )
        )
    return tuple(details)


def _public_receipt(plan) -> ToolReceiptEvidence:
    return ToolReceiptEvidence(
        tool_call_id="tool.public-verification",
        sequence_no=1,
        call_id=plan.termination.final_actor_call_id,
        tool_id="repo.public_test",
        phase="terminal_public_verification",
        tool_request_id=None,
        verification_step_id=plan.public_verification.actions[0].step_id,
        invocation_digest=public_verification_action_digest(
            plan.public_verification.actions[0]
        ),
        receipt_id="receipt.public-verification",
        receipt_digest=_digest("public-receipt"),
        status="succeeded",
        output_digest=_digest("public-output"),
        output_bytes=17,
        retry_index=0,
        started_at_ms=30,
        finished_at_ms=31,
    )


def _environment(task, pair) -> EnvironmentEvidence:
    runtime_environment = {
        "environment_id": pair.environment_id,
        "command_container_policy_digest": _digest("command-container-policy"),
        "python_identity": "python-3.12-offline",
        "platform_identity": "test-platform",
        "workspace_snapshot_digest": task.workspace_snapshot.digest,
        "container_image_digest": None,
        "network_policy": "none",
        "filesystem_policy": "scratch-workspace-only",
    }
    return EnvironmentEvidence(
        **runtime_environment,
        runtime_environment_digest=runtime_environment_evidence_digest(
            runtime_environment
        ),
    )


def _success_assembly_inputs():
    task, epoch, plan, provider, result = _execute()
    pair = PairKey(
        task_manifest_id=task.task_manifest_id,
        environment_id="environment.bridge",
        sampling_replicate=0,
        provider_config_digest=epoch.deployment.provider_config_digest,
    )
    provider_details = _provider_details(epoch, provider, result)
    public_receipt = _public_receipt(plan)
    final_budget = result.budget.model_copy(
        update={
            "tool_calls": 1,
            "tool_output_bytes": public_receipt.output_bytes,
        }
    )
    verification = PublicVerificationEvidence(
        status="passed",
        plan_digest=public_verification_plan_digest(plan),
        patch_digest=result.final_patch_digest,
        action_receipt_digests=(public_receipt.receipt_digest,),
        completed_at_ms=32,
    )
    return {
        "plan": plan,
        "task": task,
        "epoch": epoch,
        "release_digest": _digest("release"),
        "release_manifest_digest": _digest("release-manifest"),
        "profile_digest": _digest("profile"),
        "execution_mode": "deterministic_replay",
        "live_inference_status": "not_run",
        "real_inference_requests_sent": 0,
        "result": result,
        "pair_key": pair,
        "provider_calls": provider_details,
        "tool_receipts": (public_receipt,),
        "retries": (),
        "public_verification": verification,
        "environment": _environment(task, pair),
        "final_budget": final_budget,
        "no_leakage": True,
        "environment_healthy": True,
        "completed_at_ms": 33,
    }


def test_assembly_preserves_and_cross_checks_complete_runtime_evidence() -> None:
    inputs = _success_assembly_inputs()
    evidence = assemble_run_evidence(**inputs)

    assert evidence.task_manifest_digest == inputs["task"].task_manifest_digest
    assert evidence.protocol_digest == inputs["plan"].source_protocol_digest
    assert evidence.compiled_semantic_digest == inputs["plan"].compiled_semantic_digest
    assert evidence.dependency_manifest_digest == inputs["plan"].dependency_manifest_digest
    assert evidence.compiler_digest == inputs["plan"].dependency_manifest.compiler.implementation_digest
    assert evidence.kernel_digest == inputs["plan"].dependency_manifest.kernel.implementation_digest
    assert evidence.tool_manifest_digest == tool_manifest_digest(inputs["plan"])
    assert evidence.provider_calls[0].response_id == "offline.actor.investigator.initial"
    assert evidence.contexts[1].entries[-1].observed.value == "Parser normalization drops the sentinel."
    assert evidence.deliveries[0].observed == evidence.reads[0].observed
    assert evidence.patch.observed.value == inputs["result"].final_patch
    assert evidence.patch.patch_digest == inputs["result"].final_patch_digest
    assert evidence.patch.public_verification_passed is True
    assert evidence.cost_ledger.cost.tool_calls == 1
    assert evidence.termination.success is True
    assert evidence.health.healthy


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda data: data.update(provider_calls=data["provider_calls"][:-1]), "missing explicit provider"),
        (lambda data: data.update(tool_receipts=()), "explicit receipts"),
        (
            lambda data: data.update(
                final_budget=data["final_budget"].model_copy(update={"model_calls": 1})
            ),
            "regressed below runtime prefix",
        ),
        (
            lambda data: data.update(
                result=data["result"].model_copy(update={"artifact_deliveries": ()})
            ),
            "deliveries do not exactly cover",
        ),
    ],
)
def test_assembly_fails_closed_on_missing_or_crossed_runtime_facts(mutator, message) -> None:
    inputs = _success_assembly_inputs()
    mutator(inputs)
    with pytest.raises(EvidenceAssemblyError, match=message):
        assemble_run_evidence(**inputs)


def test_assembly_rejects_crossed_provider_request_and_credential_bearing_detail() -> None:
    inputs = _success_assembly_inputs()
    first = inputs["provider_calls"][0]
    crossed = first.model_copy(update={"request_digest": _digest("crossed-request")})
    inputs["provider_calls"] = (crossed, *inputs["provider_calls"][1:])
    with pytest.raises(EvidenceAssemblyError, match="provider detail crossed"):
        assemble_run_evidence(**inputs)

    payload = first.model_dump(mode="python")
    payload["response_id"] = "sk-ant-abcdefghijklmnopqrstuvwxyz"
    with pytest.raises(ValidationError, match="resolved credential"):
        ProviderCallDetail.model_validate(payload)


def test_partial_failure_without_terminal_output_is_honest_and_not_promotion_eligible() -> None:
    inputs = _success_assembly_inputs()
    result = inputs["result"]
    details = list(inputs["provider_calls"])
    failed = details[-1].model_copy(
        update={
            "status": "failed_post_send",
            "reservation_id": None,
            "response_id": "offline.failed.implementer",
            "response_digest": None,
            "response_kind": None,
            "tool_request_id": None,
        }
    )
    partial = PartialCompositeRunObservation(
        run_id=result.run_id,
        failure_kind="provider_call_failed",
        failed_call_id=failed.call_id,
        task_envelope_digest=result.task_envelope_digest,
        compiled_semantic_digest=result.compiled_semantic_digest,
        scratch_workspace=result.scratch_workspace,
        actor_calls=result.actor_calls[:1],
        stages=result.stages[:1],
        context_manifests=result.context_manifests,
        artifacts=result.artifacts,
        artifact_deliveries=result.artifact_deliveries,
        budget=result.budget,
    )
    inputs.update(
        result=partial,
        provider_calls=(details[0], failed),
        tool_receipts=(),
        public_verification=PublicVerificationEvidence(
            status="not_run",
            plan_digest=public_verification_plan_digest(inputs["plan"]),
        ),
        final_budget=result.budget,
    )
    evidence = assemble_run_evidence(**inputs)

    assert evidence.patch.status == "not_emitted"
    assert evidence.termination.reason == "hard_failure"
    assert evidence.termination.success is False
    assert evidence.health.process_integrity is False
    assert not evidence.health.healthy


def _receipt_for_evidence(inputs, evidence) -> OutcomeReceipt:
    epoch = inputs["epoch"]
    task = inputs["task"]
    return OutcomeReceipt(
        receipt_id="receipt.bridge",
        execution_mode=evidence.execution_mode,
        live_inference_status=evidence.live_inference_status,
        real_inference_requests_sent=evidence.real_inference_requests_sent,
        data_state=task.data_state,
        epoch_id=epoch.epoch_id,
        epoch_manifest_digest=epoch.epoch_manifest_digest,
        release_digest=evidence.release_digest,
        release_manifest_digest=evidence.release_manifest_digest,
        profile_digest=evidence.profile_digest,
        split_manifest_digest=task.split_manifest_digest,
        task_manifest_id=task.task_manifest_id,
        task_manifest_digest=task.task_manifest_digest,
        evaluation_contract_id="evaluation.bridge",
        evaluation_contract_digest=_digest("evaluation-contract"),
        evaluator_id=epoch.evaluator_authority.evaluator_id,
        evaluator_identity_digest=epoch.evaluator_authority.evaluator_identity_digest,
        evaluation_policy_digest=epoch.evaluator_authority.evaluation_policy_digest,
        pair_key=evidence.pair_key,
        protocol_digest=evidence.protocol_digest,
        compiler_digest=evidence.compiler_digest,
        kernel_digest=evidence.kernel_digest,
        tool_manifest_digest=evidence.tool_manifest_digest,
        provider_config_digest=evidence.provider_config_digest,
        decoding_policy_digest=evidence.decoding_policy_digest,
        price_schedule_digest=evidence.price_schedule_digest,
        command_container_policy_digest=evidence.command_container_policy_digest,
        evaluator_environment_digest=_digest("evaluator-environment"),
        patch_digest=evidence.patch.patch_digest,
        complete_repair=True,
        health=OutcomeHealth(
            process_integrity=True,
            no_leakage=True,
            environment_integrity=True,
            evaluator_integrity=True,
            accounting_complete=True,
        ),
        cost=evidence.cost_ledger.cost,
        issued_at_ms=40,
    )


def test_evaluator_binding_appends_exact_receipt_to_immutable_proof_store(tmp_path: Path) -> None:
    inputs = _success_assembly_inputs()
    evidence = assemble_run_evidence(**inputs)
    receipt = _receipt_for_evidence(inputs, evidence)
    store = ImmutableProofRecordStore(tmp_path / "proof")

    record, path = bind_and_append_evaluator_proof(
        epoch=inputs["epoch"],
        task=inputs["task"],
        run_evidence=evidence,
        outcome_receipt=receipt,
        store=store,
    )

    assert path.is_file()
    assert store.lookup_outcome(receipt.receipt_digest) == record
    assert not (store.root / "checkpoints").exists()
    assert not (store.root / "state_store").exists()
    assert not (store.root / "traces").exists()


def test_evaluator_binding_rejects_partial_or_crossed_patch(tmp_path: Path) -> None:
    inputs = _success_assembly_inputs()
    evidence = assemble_run_evidence(**inputs)
    receipt = _receipt_for_evidence(inputs, evidence)
    crossed = receipt.model_copy(update={"patch_digest": _digest("crossed-patch")})
    from agintor.contracts.outcomes import outcome_receipt_digest

    crossed = crossed.model_copy(
        update={"receipt_digest": outcome_receipt_digest(crossed)}
    )
    with pytest.raises(EvidenceAssemblyError, match="crossed exact runtime proof"):
        bind_and_append_evaluator_proof(
            epoch=inputs["epoch"],
            task=inputs["task"],
            run_evidence=evidence,
            outcome_receipt=crossed,
            store=ImmutableProofRecordStore(tmp_path / "crossed"),
        )

    unhealthy_payload = evidence.model_dump(mode="python")
    unhealthy_payload["health"]["process_integrity"] = False
    unhealthy_payload["evidence_digest"] = ""
    unhealthy = type(evidence).model_validate(unhealthy_payload)
    with pytest.raises(EvidenceAssemblyError, match="partial or unhealthy"):
        bind_and_append_evaluator_proof(
            epoch=inputs["epoch"],
            task=inputs["task"],
            run_evidence=unhealthy,
            outcome_receipt=receipt,
            store=ImmutableProofRecordStore(tmp_path / "partial"),
        )
