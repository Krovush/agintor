from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agintor.authority.roles import PROCESS_ROLE_ENV
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
from agintor.contracts.harness import (
    DependencyRef,
    HarnessProtocol,
    RuntimeDependencyManifest,
    TrustedToolDependency,
)
from agintor.contracts.outcomes import PairKey
from agintor.contracts.run_evidence import (
    EnvironmentEvidence,
    RunEvidence,
    ToolReceiptEvidence,
    runtime_environment_evidence_digest,
)
from agintor.evaluation.contracts import (
    EvaluationContract,
    HiddenCheck,
    SealedCanary,
    SealedFixtureRef,
)
from agintor.evaluation.harness_service import (
    HarnessEvaluationDigestAssertions,
    HarnessEvaluationRejected,
    HarnessEvaluationService,
)
from agintor.evaluation.harness_entrypoint import (
    HARNESS_EVALUATION_ENTRY_REQUEST_SCHEMA_VERSION,
    main as evaluation_entry_main,
)
from agintor.evaluation.runners.repo_patch_backends import (
    IsolatedRepoPatchCommandBackend,
    TrustedLocalRepoPatchCommandBackend,
)
from agintor.evaluation.runners.repo_patch_runner import (
    RepoPatchEvaluatorRunner,
    RepoPatchFixture,
    environment_digest,
    repo_patch_fixture_digest,
)
from agintor.isolation.replay import (
    IsolatedCommandReplayBinding,
    IsolatedCommandReplayManifest,
    IsolatedCommandReplayRequest,
    IsolatedCommandReplayRow,
    write_isolated_command_replay_manifest,
)
from agintor.factory.harness_release import publish_harness_release
from agintor.factory.harness_release_contracts import (
    Gate0NotRunReport,
    Gate0PreregistrationPublic,
    HarnessReleaseRequest,
    PilotNotRunSummary,
    PublicSearchLineageRecord,
    PublicSelectionDecision,
)
from agintor.isolation.commands import IsolatedCommandRequest, IsolatedCommandResult
from agintor.repositories.workspaces import repository_snapshot_digest
from agintor.runtime.api.composite_compiler import (
    compile_composite_run_plan,
    load_canonical_harness_seed,
)
from agintor.runtime.evidence import (
    ProviderCallDetail,
    PublicVerificationEvidence,
    assemble_run_evidence,
    public_verification_action_digest,
    public_verification_plan_digest,
)
from agintor.runtime.harness_profile import (
    HarnessCommandContainerPolicy,
    HarnessDecodingPolicy,
    HarnessDeploymentProfile,
    HarnessProviderEndpoint,
    HarnessUsdPriceSchedule,
)
from agintor.runtime.kernel.composite_budget import (
    CostStatus,
    ProviderUsageReport,
    UsageStatus,
)
from agintor.runtime.kernel.composite_provider import ProviderCallControl, ProviderInvocation
from agintor.runtime.kernel.composite_runtime import (
    ActorCallOutput,
    ActorCallRequest,
    CompositeRuntime,
    ScratchWorkspaceBinding,
)
from agintor.storage.proof_records import ImmutableProofRecordStore
from agintor.utils import count_tokens_rough


CANARY = "F1-EVAL-SEALED-CANARY-71e28c"
GOOD_PATCH = (
    "diff --git a/src/app.py b/src/app.py\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1 +1 @@\n"
    "-VALUE = 1\n"
    "+VALUE = 2\n"
)
WRONG_PATCH = GOOD_PATCH.replace("+VALUE = 2\n", "+VALUE = 2  # plausible but wrong\n")
PUBLIC_WRONG_PATCH = GOOD_PATCH.replace("+VALUE = 2\n", "+VALUE = 3\n")


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _ceilings() -> TaskCeilings:
    return TaskCeilings(
        max_model_calls=6,
        max_input_tokens=20_000,
        max_output_tokens=8_000,
        max_cached_tokens=0,
        max_tool_calls=30,
        max_tool_output_bytes=100_000,
        max_artifact_bytes=40_000,
        max_patch_bytes=20_000,
        max_retries=1,
        max_wall_time_ms=120_000,
        provider_deadline_ms=60_000,
        max_known_cost_usd=2.0,
        max_estimated_cost_usd=3.0,
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
            implementation_digest=_digest("harness-contract"),
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
                implementation_digest=_digest(f"tool:{tool_id}"),
                policy_digest=_digest(f"policy:{tool_id}"),
            )
            for tool_id in sorted(REPO_REPAIR_TRUSTED_TOOL_IDS)
        ),
    )


def _profile() -> HarnessDeploymentProfile:
    return HarnessDeploymentProfile(
        deployment_id="offline.fixed.f1-eval",
        provider="offline-provider",
        model="offline-model",
        endpoint=HarnessProviderEndpoint(
            base_url="https://offline.invalid/v1",
            api_key_env="OFFLINE_API_KEY",
        ),
        decoding_policy=HarnessDecodingPolicy(
            temperature=0.0,
            top_p=1.0,
            max_output_tokens=4096,
        ),
        price_schedule=HarnessUsdPriceSchedule(
            billing_mode="free",
            input_usd_per_million_tokens=0.0,
            output_usd_per_million_tokens=0.0,
            cached_input_usd_per_million_tokens=0.0,
            provider_policy_justification="deterministic offline test provider",
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


def _epoch(dependencies: RuntimeDependencyManifest) -> ResearchEpochManifest:
    profile = _profile()
    return ResearchEpochManifest(
        epoch_id="epoch.f1-eval",
        task_manifest_digest=_digest("task-panel"),
        development_split_digest=_digest("development-split"),
        sealed_confirmation_split_digest=_digest("sealed-confirmation-split"),
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
            evaluator_id="evaluator.f1-eval",
            evaluator_identity_digest=_digest("evaluator"),
            evaluation_policy_digest=_digest("evaluation-policy"),
        ),
    )


def _write_source(source: Path) -> None:
    (source / "src").mkdir(parents=True)
    (source / "tests").mkdir()
    (source / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "tests" / "sentinel.txt").write_text("immutable\n", encoding="utf-8")


def _check_argv(assertion: str) -> tuple[str, ...]:
    return (
        "python",
        "-c",
        (
            "from pathlib import Path\n"
            "text = Path('src/app.py').read_text(encoding='utf-8')\n"
            f"assert {assertion}, text"
        ),
    )


def _task(epoch: ResearchEpochManifest, source: Path) -> TaskEnvelope:
    return TaskEnvelope(
        task_manifest_id="task.f1-eval",
        epoch_id=epoch.epoch_id,
        epoch_manifest_digest=epoch.epoch_manifest_digest,
        data_state="development",
        split_manifest_digest=epoch.development_split_digest,
        issue="Repair the public VALUE regression without private target hints.",
        workspace_snapshot=WorkspaceSnapshotRef(
            snapshot_id="snapshot.f1-eval",
            uri=str(source),
            digest=repository_snapshot_digest(source),
        ),
        public_reproduction=(
            PublicReproductionStep(
                step_id="public-value-check",
                argv=_check_argv("text.startswith('VALUE = 2')"),
                timeout_ms=5_000,
            ),
        ),
        ceilings=_ceilings(),
    )


def _release_request(
    epoch: ResearchEpochManifest,
    task: TaskEnvelope,
    protocol: HarnessProtocol,
    dependencies: RuntimeDependencyManifest,
) -> HarnessReleaseRequest:
    plan = compile_composite_run_plan(task, protocol, dependencies)
    selected = protocol.source_digest()
    gate0 = Gate0PreregistrationPublic(
        preregistration_id="gate0.f1-eval",
        panel_digest=_digest("gate0-panel"),
        deterministic_suite_digest=_digest("gate0-suite"),
        planned_provider_calls=1,
        frozen_thresholds={"intact_minimum": 0.7},
    )
    return HarnessReleaseRequest(
        epoch=epoch,
        selected_protocol=protocol,
        representative_plan=plan,
        dependency_manifest=dependencies,
        deployment_profile=_profile(),
        deployment=epoch.deployment,
        search_lineage=(
            PublicSearchLineageRecord(
                sequence_no=0,
                transaction_id="txn.f1-eval",
                operator="instruction_rewrite",
                parent_protocol_digest=_digest("parent-protocol"),
                child_protocol_digest=selected,
                transaction_digest=_digest("transaction"),
                mechanism_hypothesis_digest=_digest("hypothesis"),
                status="accepted",
            ),
        ),
        selection_decisions=(
            PublicSelectionDecision(
                sequence_no=0,
                decision_id="decision.f1-eval",
                incumbent_protocol_digest=_digest("parent-protocol"),
                candidate_protocol_digest=selected,
                selected_protocol_digest=selected,
                decision="retain_candidate",
                reason_codes=("offline_evidence",),
                evidence_digests=(_digest("public-evidence"),),
            ),
        ),
        gate0_preregistration=gate0,
        gate0_report=Gate0NotRunReport(
            preregistration_digest=gate0.preregistration_digest,
        ),
        pilot_summary=PilotNotRunSummary(
            pilot_id="pilot.f1-eval",
            planned_task_manifest_digest=_digest("pilot-task"),
        ),
        limitations=("Real-provider execution has not been run.",),
    )


class RecordingIsolatedBackend:
    def __init__(self) -> None:
        self.requests: list[IsolatedCommandRequest] = []
        self._delegate = TrustedLocalRepoPatchCommandBackend()

    def run(self, request: IsolatedCommandRequest) -> IsolatedCommandResult:
        self.requests.append(request)
        return self._delegate.run(request)


class TranscriptOnlyIsolatedBackend:
    """Records deterministic command facts without executing a host or container."""

    def __init__(self, *, failing_sequence_nos: tuple[int, ...] = ()) -> None:
        self.rows: list[IsolatedCommandReplayRow] = []
        self.failing_sequence_nos = frozenset(failing_sequence_nos)

    def run(self, request: IsolatedCommandRequest) -> IsolatedCommandResult:
        replay_request = IsolatedCommandReplayRequest.from_request(request)
        empty_digest = hashlib.sha256(b"").hexdigest()
        sequence_no = len(self.rows)
        result = IsolatedCommandResult(
            status="completed",
            command=request.command,
            container_name=f"offline-evaluator-replay-{sequence_no}",
            exit_code=(1 if sequence_no in self.failing_sequence_nos else 0),
            stdout="",
            stderr="",
            stdout_digest=empty_digest,
            stderr_digest=empty_digest,
            duration_s=0.0,
        )
        self.rows.append(
            IsolatedCommandReplayRow(
                sequence_no=sequence_no,
                request=replay_request,
                workspace_content_digest_after=(
                    replay_request.workspace_content_digest
                ),
                result=result,
            )
        )
        return result


def _isolated_backend(
    recorder: RecordingIsolatedBackend,
) -> IsolatedRepoPatchCommandBackend:
    return IsolatedRepoPatchCommandBackend(
        recorder,
        environment_identity=_profile().command_container_policy.model_dump(mode="json"),
    )


def _contract(
    epoch: ResearchEpochManifest,
    task: TaskEnvelope,
    backend: IsolatedRepoPatchCommandBackend,
) -> EvaluationContract:
    draft = EvaluationContract(
        evaluation_contract_id="evaluation.f1-eval",
        epoch_id=epoch.epoch_id,
        epoch_manifest_digest=epoch.epoch_manifest_digest,
        data_state="development",
        split_manifest_digest=task.split_manifest_digest,
        task_manifest_id=task.task_manifest_id,
        task_manifest_digest=task.task_manifest_digest,
        sealed_fixture=SealedFixtureRef(
            fixture_id="fixture.f1-eval",
            uri=task.workspace_snapshot.uri,
            fixture_digest="0" * 64,
            public_snapshot_digest=task.workspace_snapshot.digest,
        ),
        protected_paths=("tests",),
        hidden_checks=(
            HiddenCheck(
                check_id="hidden-exact-value",
                argv=_check_argv("text.strip() == 'VALUE = 2'"),
                timeout_ms=5_000,
            ),
        ),
        outcome_authority=epoch.evaluator_authority,
        canaries=(SealedCanary(canary_id="canary.f1-eval", value=CANARY),),
    )
    fixture = RepoPatchFixture.from_evaluation_contract(
        draft,
        public_test_commands=task.public_reproduction,
    )
    payload = draft.model_dump(mode="python")
    payload.pop("evaluation_contract_digest", None)
    payload["sealed_fixture"]["fixture_digest"] = repo_patch_fixture_digest(
        fixture,
        backend,
    )
    return EvaluationContract.model_validate(payload)


class ScriptedProvider:
    def __init__(self, plan: Any, patch: str) -> None:
        self.plan = plan
        self.patch = patch
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
        call = next(item for item in self.plan.actor_calls if item.call_id == normalized.call_id)
        if call.emits_final_patch:
            output = ActorCallOutput(output_text="repair complete", final_patch=self.patch)
        else:
            output = ActorCallOutput(
                output_text="investigation complete",
                artifact_payloads={
                    write.artifact_id: "The public VALUE check identifies the regression."
                    for write in call.artifact_writes
                },
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


def _run_evidence(
    *,
    epoch: ResearchEpochManifest,
    task: TaskEnvelope,
    protocol: HarnessProtocol,
    dependencies: RuntimeDependencyManifest,
    pair_key: PairKey,
    patch: str,
    release_digest: str,
    release_manifest_digest: str,
    profile_digest: str,
    public_verification_passed: bool = True,
) -> RunEvidence:
    plan = compile_composite_run_plan(task, protocol, dependencies)
    provider = ScriptedProvider(plan, patch)
    result = CompositeRuntime(
        plan,
        task,
        ScratchWorkspaceBinding(
            workspace_id="scratch.f1-eval",
            workspace_digest=task.workspace_snapshot.digest,
        ),
        provider,
        run_id=f"run.{hashlib.sha256(patch.encode()).hexdigest()[:12]}",
    ).run()
    contexts = {item.call_id: item for item in result.context_manifests}
    provider_details = tuple(
        ProviderCallDetail(
            provider_call_id=f"provider.{sequence_no}",
            sequence_no=sequence_no,
            call_id=call.call_id,
            actor_id=call.actor_id,
            turn_index=round_.turn_index,
            attempt_index=0,
            runtime_context_manifest_digest=contexts[call.call_id].manifest_digest,
            reservation_id=round_.reservation_id,
            deployment_id=epoch.deployment.deployment_id,
            provider=epoch.deployment.provider,
            model=epoch.deployment.model,
            provider_config_digest=epoch.deployment.provider_config_digest,
            request_digest=round_.request_digest,
            status="succeeded",
            request_sent=True,
            response_id=round_.response_id,
            response_digest=round_.response_digest,
            response_kind=round_.response_kind,
            tool_request_id=round_.tool_request_id,
            started_at_ms=round_.started_at_ms,
            finished_at_ms=round_.finished_at_ms,
        )
        for sequence_no, (call, round_) in enumerate(
            (
                (call, round_)
                for call in result.actor_calls
                for round_ in call.provider_rounds
            ),
            start=1,
        )
    )
    receipts = tuple(
        ToolReceiptEvidence(
            tool_call_id=f"tool.public-verification.{index}",
            sequence_no=index,
            call_id=plan.termination.final_actor_call_id,
            tool_id="repo.public_test",
            phase="terminal_public_verification",
            verification_step_id=action.step_id,
            invocation_digest=public_verification_action_digest(action),
            receipt_id=f"receipt.public-verification.{index}",
            receipt_digest=_digest(f"public-receipt:{index}:{patch}"),
            status=("succeeded" if public_verification_passed else "failed"),
            output_digest=_digest(f"public-output:{index}:{patch}"),
            output_bytes=17,
            retry_index=0,
            started_at_ms=50 + index,
            finished_at_ms=51 + index,
        )
        for index, action in enumerate(plan.public_verification.actions, start=1)
    )
    final_budget = result.budget.model_copy(
        update={
            "tool_calls": len(receipts),
            "tool_output_bytes": sum(item.output_bytes for item in receipts),
        }
    )
    runtime_environment = {
        "environment_id": pair_key.environment_id,
        "command_container_policy_digest": epoch.deployment.command_container_policy_digest,
        "python_identity": "python-3.12-offline",
        "platform_identity": "deterministic-test-platform",
        "workspace_snapshot_digest": task.workspace_snapshot.digest,
        "container_image_digest": None,
        "network_policy": "none",
        "filesystem_policy": "scratch-workspace-only",
    }
    return assemble_run_evidence(
        plan=plan,
        task=task,
        epoch=epoch,
        release_digest=release_digest,
        release_manifest_digest=release_manifest_digest,
        profile_digest=profile_digest,
        execution_mode="deterministic_replay",
        live_inference_status="not_run",
        real_inference_requests_sent=0,
        result=result,
        pair_key=pair_key,
        provider_calls=provider_details,
        tool_receipts=receipts,
        retries=(),
        public_verification=PublicVerificationEvidence(
            status=("passed" if public_verification_passed else "failed"),
            plan_digest=public_verification_plan_digest(plan),
            patch_digest=result.final_patch_digest,
            action_receipt_digests=tuple(item.receipt_digest for item in receipts),
            completed_at_ms=80,
        ),
        environment=EnvironmentEvidence(
            **runtime_environment,
            runtime_environment_digest=runtime_environment_evidence_digest(
                runtime_environment
            ),
        ),
        final_budget=final_budget,
        no_leakage=True,
        environment_healthy=True,
        completed_at_ms=81,
    )


def _setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    patch: str = GOOD_PATCH,
    public_verification_passed: bool = True,
):
    monkeypatch.setenv(PROCESS_ROLE_ENV, "evaluator")
    source = tmp_path / "source"
    _write_source(source)
    dependencies = _dependencies()
    epoch = _epoch(dependencies)
    task = _task(epoch, source)
    protocol = load_canonical_harness_seed().protocol
    project = tmp_path / "project"
    release, _ = publish_harness_release(
        project_root=project,
        request=_release_request(epoch, task, protocol, dependencies),
    )
    recorder = RecordingIsolatedBackend()
    backend = _isolated_backend(recorder)
    contract = _contract(epoch, task, backend)
    fixture = RepoPatchFixture.from_evaluation_contract(
        contract,
        public_test_commands=task.public_reproduction,
    )
    pair_key = PairKey(
        task_manifest_id=task.task_manifest_id,
        environment_id="environment.f1-eval",
        sampling_replicate=0,
        provider_config_digest=epoch.deployment.provider_config_digest,
    )
    evidence = _run_evidence(
        epoch=epoch,
        task=task,
        protocol=protocol,
        dependencies=dependencies,
        pair_key=pair_key,
        patch=patch,
        release_digest=release.manifest.release_digest,
        release_manifest_digest=release.manifest.manifest_digest,
        profile_digest=release.manifest.profile_digest,
        public_verification_passed=public_verification_passed,
    )
    store = ImmutableProofRecordStore(tmp_path / "controlled-proof-store")
    service = HarnessEvaluationService(
        project_root=project,
        proof_store=store,
        command_backend=backend,
        epoch_resolver=lambda digest: epoch if digest == epoch.epoch_manifest_digest else None,
        clock_ms=lambda: 1_234_567,
    )
    return service, recorder, store, epoch, task, contract, pair_key, evidence


def test_dry_run_is_exact_not_run_and_never_invokes_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, recorder, _, epoch, task, contract, pair_key, _ = _setup(
        tmp_path,
        monkeypatch,
    )

    manifest = service.dry_run(
        contract=contract,
        task=task,
        submitted_unified_diff=GOOD_PATCH,
        pair_key=pair_key,
        digest_assertions=HarnessEvaluationDigestAssertions(
            epoch_manifest_digest=epoch.epoch_manifest_digest,
        ),
    )

    assert manifest.status == "not_run"
    assert manifest.backend_invocations == 0
    assert recorder.requests == []
    assert manifest.commands[0].argv == (
        "git",
        "apply",
        "--whitespace=nowarn",
        "--",
        "../.agintor_evaluator_input/candidate.patch",
    )
    assert [command.phase for command in manifest.commands] == [
        "patch_apply",
        "public_check",
        "sealed_check",
    ]
    assert manifest.mounts[0].target == "/workspace"
    assert manifest.mounts[0].immutable_source_not_mounted is True
    assert manifest.identity.task_manifest_digest == task.task_manifest_digest
    assert manifest.identity.epoch_manifest_digest == epoch.epoch_manifest_digest


def test_evaluate_issues_public_summary_and_appends_cross_bound_proof_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, recorder, store, _, task, contract, pair_key, evidence = _setup(
        tmp_path,
        monkeypatch,
    )

    result = service.evaluate(
        contract=contract,
        task=task,
        submitted_unified_diff=GOOD_PATCH,
        run_evidence=evidence,
        pair_key=pair_key,
    )

    assert result.summary.status == "accepted"
    assert result.summary.complete_repair is True
    assert result.summary.run_evidence_digest == evidence.evidence_digest
    assert len(recorder.requests) == 3
    record_path = store.root / result.proof_references.proof_record_ref
    assert record_path.is_file()
    assert (store.root / result.proof_references.outcome_link_ref).is_file()
    records = list(store.iter_records())
    assert len(records) == 1
    assert records[0].run_evidence.evidence_digest == evidence.evidence_digest
    assert records[0].outcome_receipt is not None
    assert records[0].outcome_receipt.complete_repair is True
    public_text = result.model_dump_json()
    assert GOOD_PATCH not in public_text
    assert CANARY not in public_text
    assert "hidden-exact-value" not in public_text

    with pytest.raises(HarnessEvaluationRejected) as caught:
        service.evaluate(
            contract=contract,
            task=task,
            submitted_unified_diff=GOOD_PATCH,
            run_evidence=evidence,
            pair_key=pair_key,
        )
    assert caught.value.code == "duplicate_outcome"
    assert len(recorder.requests) == 3


def test_plausible_wrong_patch_gets_negative_receipt_but_invalid_inputs_get_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, recorder, store, _, task, contract, pair_key, wrong_evidence = _setup(
        tmp_path,
        monkeypatch,
        patch=WRONG_PATCH,
    )

    wrong = service.evaluate(
        contract=contract,
        task=task,
        submitted_unified_diff=WRONG_PATCH,
        run_evidence=wrong_evidence,
        pair_key=pair_key,
    )
    assert wrong.summary.status == "accepted"
    assert wrong.summary.complete_repair is False
    records = list(store.iter_records())
    assert len(records) == 1
    assert records[0].outcome_receipt is not None
    assert records[0].outcome_receipt.complete_repair is False
    assert records[0].outcome_receipt.health.passes_promotion_floor
    assert len(recorder.requests) == 3

    for patch in (
        "",
        "--- a/../escape.py\n+++ b/../escape.py\n@@ -0,0 +1 @@\n+escape\n",
        "--- a/tests/sentinel.txt\n+++ b/tests/sentinel.txt\n@@ -1 +1 @@\n-immutable\n+tampered\n",
    ):
        before = len(recorder.requests)
        with pytest.raises(HarnessEvaluationRejected):
            service.dry_run(
                contract=contract,
                task=task,
                submitted_unified_diff=patch,
                pair_key=pair_key,
            )
        assert len(recorder.requests) == before

    payload = wrong_evidence.model_dump(mode="python")
    payload.pop("evidence_digest", None)
    payload["health"]["accounting_complete"] = False
    payload["cost_ledger"].pop("ledger_digest", None)
    payload["cost_ledger"]["reconciled"] = False
    unhealthy = RunEvidence.model_validate(payload)
    before = len(recorder.requests)
    with pytest.raises(HarnessEvaluationRejected) as rejected:
        service.evaluate(
            contract=contract,
            task=task,
            submitted_unified_diff=WRONG_PATCH,
            run_evidence=unhealthy,
            pair_key=pair_key,
        )
    assert rejected.value.code == "run_evidence_unhealthy"
    assert len(recorder.requests) == before
    assert len(list(store.iter_records())) == 1


def test_public_verification_failure_is_healthy_negative_evaluator_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, recorder, store, _, task, contract, pair_key, evidence = _setup(
        tmp_path,
        monkeypatch,
        patch=PUBLIC_WRONG_PATCH,
        public_verification_passed=False,
    )

    result = service.evaluate(
        contract=contract,
        task=task,
        submitted_unified_diff=PUBLIC_WRONG_PATCH,
        run_evidence=evidence,
        pair_key=pair_key,
    )

    assert evidence.health.healthy
    assert evidence.termination.reason == "public_verification_failed"
    assert evidence.termination.success is False
    assert result.summary.complete_repair is False
    assert len(recorder.requests) == 3
    record = next(store.iter_records())
    assert record.outcome_receipt is not None
    assert record.outcome_receipt.complete_repair is False
    assert record.outcome_receipt.health.passes_promotion_floor


def test_role_backend_release_and_digest_assertion_boundaries_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, recorder, store, _, task, contract, pair_key, _ = _setup(
        tmp_path,
        monkeypatch,
    )

    with pytest.raises(HarnessEvaluationRejected) as assertion:
        service.dry_run(
            contract=contract,
            task=task,
            submitted_unified_diff=GOOD_PATCH,
            pair_key=pair_key,
            digest_assertions=HarnessEvaluationDigestAssertions(
                release_digest="0" * 64,
            ),
        )
    assert assertion.value.code == "digest_assertion_mismatch"
    assert recorder.requests == []

    monkeypatch.delenv(PROCESS_ROLE_ENV)
    with pytest.raises(HarnessEvaluationRejected) as role:
        service.dry_run(
            contract=contract,
            task=task,
            submitted_unified_diff=GOOD_PATCH,
            pair_key=pair_key,
        )
    assert role.value.code == "evaluator_role_required"

    monkeypatch.setenv(PROCESS_ROLE_ENV, "evaluator")
    with pytest.raises(HarnessEvaluationRejected) as backend:
        HarnessEvaluationService(
            project_root=service.project_root,
            proof_store=store,
            command_backend=TrustedLocalRepoPatchCommandBackend(),
            epoch_resolver=lambda _digest_value: service.epoch_resolver(
                contract.epoch_manifest_digest
            ),
        )
    assert backend.value.code == "isolated_backend_required"


@pytest.mark.parametrize("role", ["runtime", "factory"])
def test_public_process_cannot_import_evaluation_contract_through_service(role: str) -> None:
    environment = dict(os.environ)
    environment[PROCESS_ROLE_ENV] = role
    completed = subprocess.run(
        [sys.executable, "-c", "import agintor.evaluation.harness_service"],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "cannot import evaluator-only EvaluationContract code" in completed.stderr


@pytest.mark.parametrize("role", ["runtime", "factory", "proposer"])
def test_public_process_cannot_import_evaluator_product_entrypoint(role: str) -> None:
    environment = dict(os.environ)
    environment[PROCESS_ROLE_ENV] = role
    completed = subprocess.run(
        [sys.executable, "-c", "import agintor.evaluation.harness_entrypoint"],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "cannot import evaluator-only EvaluationContract code" in completed.stderr


def test_evaluator_entrypoint_requires_explicit_process_role_and_writes_stable_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(PROCESS_ROLE_ENV, raising=False)
    output = tmp_path / "result.json"

    exit_code = evaluation_entry_main(
        [
            "--project-root",
            str(tmp_path / "missing-project"),
            "--request-json",
            str(tmp_path / "missing-request.json"),
            "--output-json",
            str(output),
        ]
    )

    assert exit_code == 2
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload == {
        "schema_version": "repo-repair-harness-evaluation-entry-error-v1",
        "status": "failed",
        "code": "evaluator_role_required",
        "message": (
            "harness evaluation entrypoint runs only with "
            "AGINTOR_PROCESS_ROLE=evaluator"
        ),
    }
    assert list(tmp_path.glob(".result.json.*.tmp")) == []


def test_evaluator_entrypoint_dry_run_makes_zero_replay_backend_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _, epoch, task, contract, pair_key, _ = _setup(
        tmp_path,
        monkeypatch,
    )
    transcript = TranscriptOnlyIsolatedBackend()
    fixture = RepoPatchFixture.from_evaluation_contract(
        contract,
        public_test_commands=task.public_reproduction,
    )
    RepoPatchEvaluatorRunner(
        _isolated_backend(transcript),
    ).run(candidate_artifact=GOOD_PATCH, fixture=fixture)
    binding = IsolatedCommandReplayBinding.from_runtime_inputs(
        release_digest=(
            json.loads((service.project_root / "active_release.json").read_text(encoding="utf-8"))[
                "release_digest"
            ]
        ),
        task=task,
        command_policy_digest=_profile().command_container_policy_digest,
    )
    replay_path = tmp_path / "commands.json"
    write_isolated_command_replay_manifest(
        replay_path,
        IsolatedCommandReplayManifest(binding=binding, rows=tuple(transcript.rows)),
    )
    request_path = tmp_path / "dry-run-request.json"
    request_path.write_text(
        json.dumps(
            {
                "schema_version": HARNESS_EVALUATION_ENTRY_REQUEST_SCHEMA_VERSION,
                "operation": "dry_run",
                "execution": {
                    "mode": "replay",
                    "command_manifest_path": replay_path.name,
                },
                "epoch": epoch.model_dump(mode="json"),
                "contract": contract.model_dump(mode="json"),
                "task": task.model_dump(mode="json"),
                "submitted_unified_diff": GOOD_PATCH,
                "pair_key": pair_key.model_dump(mode="json"),
                "digest_assertions": {
                    "epoch_manifest_digest": epoch.epoch_manifest_digest,
                },
                "proof_store_root": "controlled-entry-proofs",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    output = tmp_path / "dry-run-result.json"

    exit_code = evaluation_entry_main(
        [
            "--project-root",
            str(service.project_root),
            "--request-json",
            str(request_path),
            "--output-json",
            str(output),
        ]
    )

    assert exit_code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "not_run"
    assert payload["backend_invocations"] == 0
    assert payload["real_docker_requests_sent"] == 0
    assert payload["nonpublic_command_details_withheld"] is True
    assert [command["phase"] for command in payload["public_commands"]] == [
        "patch_apply",
        "public_check",
    ]
    assert not (tmp_path / "controlled-entry-proofs").exists()
    output_text = output.read_text(encoding="utf-8")
    assert CANARY not in output_text
    assert "text.strip() == 'VALUE = 2'" not in output_text
    assert "evaluation_contract_digest" not in output_text
    assert "source_fixture_digest" not in output_text

    live_request = json.loads(request_path.read_text(encoding="utf-8"))
    live_request["execution"] = {"mode": "live"}
    request_path.write_text(json.dumps(live_request, sort_keys=True), encoding="utf-8")
    live_output = tmp_path / "dry-run-live-result.json"

    def forbidden_docker(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("dry-run attempted Docker execution")

    monkeypatch.setattr(
        "agintor.isolation.commands.DockerCommandBackend.run",
        forbidden_docker,
    )
    live_exit_code = evaluation_entry_main(
        [
            "--project-root",
            str(service.project_root),
            "--request-json",
            str(request_path),
            "--output-json",
            str(live_output),
        ]
    )
    live_payload = json.loads(live_output.read_text(encoding="utf-8"))
    assert live_exit_code == 0
    assert live_payload["execution_mode"] == "live"
    assert live_payload["backend_invocations"] == 0
    assert live_payload["real_docker_requests_sent"] == 0


@pytest.mark.parametrize(
    (
        "patch",
        "failing_sequence_nos",
        "public_verification_passed",
        "expected_complete_repair",
    ),
    [
        (GOOD_PATCH, (), True, True),
        (WRONG_PATCH, (2,), True, False),
        (PUBLIC_WRONG_PATCH, (1, 2), False, False),
    ],
)
def test_evaluator_entrypoint_replays_positive_and_negative_outcomes_without_docker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patch: str,
    failing_sequence_nos: tuple[int, ...],
    public_verification_passed: bool,
    expected_complete_repair: bool,
) -> None:
    service, _, _, epoch, task, contract, pair_key, evidence = _setup(
        tmp_path,
        monkeypatch,
        patch=patch,
        public_verification_passed=public_verification_passed,
    )
    transcript = TranscriptOnlyIsolatedBackend(
        failing_sequence_nos=failing_sequence_nos,
    )
    fixture = RepoPatchFixture.from_evaluation_contract(
        contract,
        public_test_commands=task.public_reproduction,
    )
    transcript_result = RepoPatchEvaluatorRunner(
        _isolated_backend(transcript),
    ).run(candidate_artifact=patch, fixture=fixture)
    assert transcript_result.complete_repair is expected_complete_repair
    active = json.loads(
        (service.project_root / "active_release.json").read_text(encoding="utf-8")
    )
    manifest = IsolatedCommandReplayManifest(
        binding=IsolatedCommandReplayBinding.from_runtime_inputs(
            release_digest=active["release_digest"],
            task=task,
            command_policy_digest=_profile().command_container_policy_digest,
        ),
        rows=tuple(transcript.rows),
    )
    replay_path = tmp_path / "commands.json"
    write_isolated_command_replay_manifest(replay_path, manifest)
    request = {
        "schema_version": HARNESS_EVALUATION_ENTRY_REQUEST_SCHEMA_VERSION,
        "operation": "evaluate",
        "execution": {
            "mode": "replay",
            "command_manifest_path": replay_path.name,
        },
        "epoch": epoch.model_dump(mode="json"),
        "contract": contract.model_dump(mode="json"),
        "task": task.model_dump(mode="json"),
        "submitted_unified_diff": patch,
        "pair_key": pair_key.model_dump(mode="json"),
        "run_evidence": evidence.model_dump(mode="json"),
        "digest_assertions": {
            "release_digest": active["release_digest"],
            "epoch_manifest_digest": epoch.epoch_manifest_digest,
        },
        "proof_store_root": "controlled-entry-proofs",
    }
    request_path = tmp_path / "evaluate-request.json"
    request_path.write_text(json.dumps(request, sort_keys=True), encoding="utf-8")
    output = tmp_path / "evaluate-result.json"

    def forbidden_docker(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("replay evaluation attempted Docker execution")

    monkeypatch.setattr(
        "agintor.isolation.commands.DockerCommandBackend.run",
        forbidden_docker,
    )
    exit_code = evaluation_entry_main(
        [
            "--project-root",
            str(service.project_root),
            "--request-json",
            str(request_path),
            "--output-json",
            str(output),
        ]
    )

    assert exit_code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["summary"]["status"] == "accepted"
    assert payload["summary"]["complete_repair"] is expected_complete_repair
    assert payload["summary"]["run_evidence_digest"] == evidence.evidence_digest
    assert set(payload) == {"summary", "proof_references"}
    assert CANARY not in output.read_text(encoding="utf-8")
    assert "hidden-exact-value" not in output.read_text(encoding="utf-8")
    assert (
        tmp_path
        / "controlled-entry-proofs"
        / payload["proof_references"]["proof_record_ref"]
    ).is_file()
    assert list(tmp_path.glob(".evaluate-result.json.*.tmp")) == []
