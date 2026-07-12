from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

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
    HarnessPublicSessionContext,
    RuntimeDependencyManifest,
    TrustedToolDependency,
)
from agintor.contracts.outcomes import PairKey
from agintor.core.identity import canonical_identity_digest
from agintor.factory.harness_release import publish_harness_release
from agintor.factory.harness_release_contracts import (
    Gate0NotRunReport,
    Gate0PreregistrationPublic,
    HarnessReleaseRequest,
    PilotNotRunSummary,
    PublicSearchLineageRecord,
    PublicSelectionDecision,
)
from agintor.isolation.commands import IsolatedCommandResult, IsolatedCommandStatus
from agintor.repositories.workspaces import repository_snapshot_digest
from agintor.runtime.api.composite_compiler import (
    compile_composite_run_plan,
    load_canonical_harness_seed,
)
from agintor.runtime.kernel.composite_budget import (
    CostStatus,
    ProviderUsageReport,
    UsageStatus,
)
from agintor.runtime.kernel.composite_provider import (
    CredentialReference,
    ProviderCallControl,
    ProviderExecutionProvenance,
    ProviderInvocation,
    ProviderInvocationError,
)
from agintor.runtime.kernel.composite_runtime import (
    ActorCallOutput,
    ActorCallRequest,
    ActorTerminalTurn,
    ActorToolRequest,
)
from agintor.runtime.harness_profile import (
    HarnessCommandContainerPolicy,
    HarnessDecodingPolicy,
    HarnessDeploymentProfile,
    HarnessProviderEndpoint,
    HarnessUsdPriceSchedule,
)
from agintor.runtime.sdk.harness_executor import (
    CONTROLLED_RUN_EVIDENCE_REF,
    HARNESS_SOLVE_RESULT_FILE,
    HarnessSolveError,
    HarnessSolveResult,
    execute_harness_solve,
    load_controlled_run_evidence,
)
from agintor.runtime.sdk.harness_entrypoint import (
    HarnessReplayExecution,
    HarnessSolveFileRequest,
)
from agintor.storage.harness_session_store import HarnessSessionStore


def _digest(label: str) -> str:
    return canonical_identity_digest(label, domain="test-harness-sdk")


def _pair_key(task: TaskEnvelope, epoch: ResearchEpochManifest) -> PairKey:
    return PairKey(
        task_manifest_id=task.task_manifest_id,
        environment_id="environment.sdk.replay",
        sampling_replicate=0,
        provider_config_digest=epoch.deployment.provider_config_digest,
    )


def _ceilings() -> TaskCeilings:
    return TaskCeilings(
        max_model_calls=20,
        max_input_tokens=80_000,
        max_output_tokens=30_000,
        max_cached_tokens=10_000,
        max_tool_calls=40,
        max_tool_output_bytes=200_000,
        max_artifact_bytes=50_000,
        max_patch_bytes=30_000,
        max_retries=1,
        max_wall_time_ms=120_000,
        provider_deadline_ms=30_000,
        max_known_cost_usd=10.0,
        max_estimated_cost_usd=12.0,
    )


def _deployment_profile() -> HarnessDeploymentProfile:
    return HarnessDeploymentProfile(
        deployment_id="scripted.fixed.sdk",
        provider="scripted",
        model="scripted-repair-v1",
        endpoint=HarnessProviderEndpoint(
            base_url_env="SCRIPTED_BASE_URL",
            api_key_env="SCRIPTED_PROVIDER_API_KEY",
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
            provider_policy_justification="scripted SDK fixture has no provider billing",
        ),
        command_container_policy=HarnessCommandContainerPolicy(
            image="python@sha256:" + "d" * 64,
            timeout_s=30.0,
            memory_bytes=512 * 1024 * 1024,
            cpu_count=1.0,
            pids_limit=128,
            output_bytes=1_000_000,
            tmpfs_bytes=64 * 1024 * 1024,
            nofile_limit=256,
        ),
    )


def _deployment() -> DeploymentIdentity:
    return _deployment_profile().to_deployment_identity()


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


def _epoch() -> ResearchEpochManifest:
    return ResearchEpochManifest(
        epoch_id="epoch.harness-sdk",
        task_manifest_digest=_digest("task-manifest"),
        development_split_digest=_digest("development-split"),
        sealed_confirmation_split_digest=_digest("sealed-split"),
        deployment=_deployment(),
        per_run_ceilings=_ceilings(),
        search_envelope=SearchEnvelope(
            max_steps=2,
            offspring_per_step=2,
            sampling_replicates=2,
            task_panel_digest=_digest("task-panel"),
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
            max_candidate_evaluations=4,
            max_consecutive_non_improving_steps=2,
        ),
        evaluator_authority=EvaluatorAuthority(
            evaluator_id="evaluator.harness-sdk",
            evaluator_identity_digest=_digest("evaluator"),
            evaluation_policy_digest=_digest("evaluation-policy"),
        ),
    )


def _representative_task(epoch: ResearchEpochManifest) -> TaskEnvelope:
    return TaskEnvelope(
        task_manifest_id="task.harness-sdk.representative",
        epoch_id=epoch.epoch_id,
        epoch_manifest_digest=epoch.epoch_manifest_digest,
        data_state="development",
        split_manifest_digest=epoch.development_split_digest,
        issue="Repair the public regression using repository evidence.",
        workspace_snapshot=WorkspaceSnapshotRef(
            snapshot_id="snapshot.harness-sdk.representative",
            uri="public-snapshot-reference",
            digest=_digest("representative-snapshot"),
        ),
        public_reproduction=(
            PublicReproductionStep(
                step_id="public-test",
                argv=("python", "-m", "pytest", "-q"),
                timeout_ms=20_000,
            ),
        ),
        ceilings=_ceilings(),
    )


def _release_request(epoch: ResearchEpochManifest) -> HarnessReleaseRequest:
    protocol = load_canonical_harness_seed().protocol
    dependencies = _dependencies()
    plan = compile_composite_run_plan(
        _representative_task(epoch),
        protocol,
        dependencies,
    )
    protocol_digest = protocol.source_digest()
    gate0 = Gate0PreregistrationPublic(
        preregistration_id="gate0.harness-sdk",
        panel_digest=_digest("gate0-panel"),
        deterministic_suite_digest=_digest("gate0-suite"),
        planned_provider_calls=32,
        frozen_thresholds={"complete_minimum": 0.7},
    )
    return HarnessReleaseRequest(
        epoch=epoch,
        selected_protocol=protocol,
        representative_plan=plan,
        dependency_manifest=dependencies,
        deployment_profile=_deployment_profile(),
        deployment=epoch.deployment,
        search_lineage=(
            PublicSearchLineageRecord(
                sequence_no=0,
                transaction_id="txn.harness-sdk.seed",
                operator="instruction_rewrite",
                parent_protocol_digest=protocol_digest,
                child_protocol_digest=protocol_digest,
                transaction_digest=_digest("seed-transaction"),
                mechanism_hypothesis_digest=_digest("seed-hypothesis"),
                status="accepted",
            ),
        ),
        selection_decisions=(
            PublicSelectionDecision(
                sequence_no=0,
                decision_id="decision.harness-sdk.seed",
                incumbent_protocol_digest=protocol_digest,
                candidate_protocol_digest=protocol_digest,
                selected_protocol_digest=protocol_digest,
                decision="retain_incumbent",
                reason_codes=("canonical_seed",),
                evidence_digests=(_digest("seed-evidence"),),
            ),
        ),
        gate0_preregistration=gate0,
        gate0_report=Gate0NotRunReport(
            preregistration_digest=gate0.preregistration_digest,
        ),
        pilot_summary=PilotNotRunSummary(
            pilot_id="pilot.harness-sdk",
            planned_task_manifest_digest=_digest("pilot-task"),
        ),
        limitations=("Real-provider inference has not been run.",),
    )


@dataclass(frozen=True)
class ReleasedFactory:
    project_root: Path
    epoch: ResearchEpochManifest
    release_digest: str


@pytest.fixture(scope="module")
def released_factory(tmp_path_factory: pytest.TempPathFactory) -> ReleasedFactory:
    root = tmp_path_factory.mktemp("harness-sdk-release") / "factory"
    epoch = _epoch()
    release, _ = publish_harness_release(
        project_root=root,
        request=_release_request(epoch),
    )
    return ReleasedFactory(
        project_root=root,
        epoch=epoch,
        release_digest=release.manifest.release_digest,
    )


def _source_repository(root: Path) -> Path:
    source = root / "source"
    (source / "src").mkdir(parents=True)
    (source / "src" / "app.py").write_text(
        'def value():\n    return "old"\n',
        encoding="utf-8",
        newline="\n",
    )
    return source


def _task(
    epoch: ResearchEpochManifest,
    source: Path,
    *,
    issue: str = "Change the public value from old to new.",
    snapshot_digest: str | None = None,
    epoch_manifest_digest: str | None = None,
) -> TaskEnvelope:
    return TaskEnvelope(
        task_manifest_id=f"task.harness-sdk.{source.parent.name}",
        epoch_id=epoch.epoch_id,
        epoch_manifest_digest=epoch_manifest_digest or epoch.epoch_manifest_digest,
        data_state="development",
        split_manifest_digest=epoch.development_split_digest,
        issue=issue,
        workspace_snapshot=WorkspaceSnapshotRef(
            snapshot_id=f"snapshot.harness-sdk.{source.parent.name}",
            uri=str(source),
            digest=snapshot_digest or repository_snapshot_digest(source),
        ),
        public_reproduction=(
            PublicReproductionStep(
                step_id="public-test",
                argv=("python", "-m", "pytest", "-q"),
                timeout_ms=20_000,
            ),
        ),
        ceilings=_ceilings(),
    )


class ScriptedRepairProvider:
    execution_provenance = ProviderExecutionProvenance(
        execution_mode="deterministic_replay",
        live_inference_status="not_run",
        real_inference_requests_sent=0,
    )

    def __init__(self, deployment: DeploymentIdentity) -> None:
        self.deployment_identity = deployment
        self.requests: list[ActorCallRequest] = []
        self.credential_references: list[CredentialReference | None] = []

    def invoke(
        self,
        request: Any,
        *,
        control: ProviderCallControl,
        credential_reference: CredentialReference | None,
    ) -> ProviderInvocation:
        del control
        request = ActorCallRequest.model_validate(request)
        self.requests.append(request)
        self.credential_references.append(credential_reference)
        response = self._response(request)
        sequence = len(self.requests)
        return ProviderInvocation(
            response=response,
            usage=ProviderUsageReport(
                usage_status=UsageStatus.KNOWN,
                input_tokens=10,
                output_tokens=4,
                cached_tokens=0,
                cost_status=CostStatus.KNOWN,
                cost_usd=0.0,
                response_id=f"scripted.response.{sequence}",
            ),
        )

    @staticmethod
    def _response(request: ActorCallRequest) -> ActorToolRequest | ActorTerminalTurn:
        request_id = f"{request.call_id}.tool.{request.turn_index}"
        if request.call_id == "actor.investigator.initial":
            if request.turn_index == 0:
                return ActorToolRequest(
                    request_id=request_id,
                    tool_id="repo.search",
                    arguments={"query": "old", "path": "src"},
                )
            if request.turn_index == 1:
                return ActorToolRequest(
                    request_id=request_id,
                    tool_id="repo.read",
                    arguments={"path": "src/app.py"},
                )
            if request.turn_index == 2:
                return ActorToolRequest(
                    request_id=request_id,
                    tool_id="repo.public_test",
                    arguments={},
                )
            if request.turn_index == 3:
                return ActorToolRequest(
                    request_id=request_id,
                    tool_id="repo.diff",
                    arguments={},
                )
            return ActorTerminalTurn(
                output=ActorCallOutput(
                    output_text="The public value is still old in src/app.py.",
                    artifact_payloads={
                        "artifact.investigation": "src/app.py returns the stale public value old."
                    },
                )
            )
        if request.call_id == "actor.implementer.initial":
            if request.turn_index == 0:
                return ActorToolRequest(
                    request_id=request_id,
                    tool_id="repo.edit",
                    arguments={
                        "path": "src/app.py",
                        "content": 'def value():\n    return "new"\n',
                    },
                )
            if request.turn_index == 1:
                return ActorToolRequest(
                    request_id=request_id,
                    tool_id="repo.public_test",
                    arguments={},
                )
            if request.turn_index == 2:
                return ActorToolRequest(
                    request_id=request_id,
                    tool_id="repo.diff",
                    arguments={},
                )
            patch = request.tool_results[-1].output["patch"]
            return ActorTerminalTurn(
                output=ActorCallOutput(
                    output_text="Submitted the reconciled public repair.",
                    final_patch=patch,
                )
            )
        raise AssertionError(f"unexpected call id: {request.call_id}")


class FakeLiveRepairProvider(ScriptedRepairProvider):
    def __init__(
        self,
        deployment: DeploymentIdentity,
        *,
        final_report: str = "honest",
    ) -> None:
        super().__init__(deployment)
        self.final_report = final_report

    @property
    def execution_provenance(self) -> ProviderExecutionProvenance:
        if not self.requests or self.final_report == "stale_not_run":
            return ProviderExecutionProvenance(
                execution_mode="live_provider",
                live_inference_status="not_run",
                real_inference_requests_sent=0,
            )
        reported_count = len(self.requests)
        if self.final_report == "wrong_count":
            reported_count -= 1
        return ProviderExecutionProvenance(
            execution_mode="live_provider",
            live_inference_status="completed",
            real_inference_requests_sent=reported_count,
        )


class PassingCommandBackend:
    def __init__(self) -> None:
        self.policy = _deployment_profile().command_container_policy.to_isolated_command_policy()
        self.requests: list[Any] = []

    def run(self, request: Any) -> IsolatedCommandResult:
        self.requests.append(request)
        stdout = b"public checks passed\n"
        stderr = b""
        return IsolatedCommandResult(
            status=IsolatedCommandStatus.COMPLETED,
            command=request.command,
            container_name=f"fake-command-{len(self.requests)}",
            exit_code=0,
            stdout=stdout.decode(),
            stderr="",
            stdout_digest=hashlib.sha256(stdout).hexdigest(),
            stderr_digest=hashlib.sha256(stderr).hexdigest(),
            duration_s=0.001,
        )


class MutatingPublicTestBackend(PassingCommandBackend):
    def __init__(self) -> None:
        super().__init__()
        self.request_workspace_digests: list[str] = []

    def run(self, request: Any) -> IsolatedCommandResult:
        self.request_workspace_digests.append(
            repository_snapshot_digest(request.workspace)
        )
        (request.workspace / "src" / "app.py").write_text(
            'def value():\n    return "verification-only"\n',
            encoding="utf-8",
            newline="\n",
        )
        cache = request.workspace / "src" / "__pycache__"
        cache.mkdir()
        (cache / "app.cpython-312.pyc").write_bytes(b"cache")
        return super().run(request)


class AccountingFailureProvider:
    execution_provenance = ProviderExecutionProvenance(
        execution_mode="deterministic_replay",
        live_inference_status="not_run",
        real_inference_requests_sent=0,
    )

    def __init__(self, deployment: DeploymentIdentity) -> None:
        self.deployment_identity = deployment
        self.calls = 0

    def invoke(
        self,
        request: Any,
        *,
        control: ProviderCallControl,
        credential_reference: CredentialReference | None,
    ) -> ProviderInvocation:
        del request, control, credential_reference
        self.calls += 1
        raise ProviderInvocationError(
            request_sent=True,
            usage=ProviderUsageReport.unknown(),
        )


def test_sdk_executes_real_tool_turns_and_returns_public_identity_evidence(
    released_factory: ReleasedFactory,
    tmp_path: Path,
) -> None:
    source = _source_repository(tmp_path)
    task = _task(released_factory.epoch, source)
    workspace = tmp_path / "run-artifacts"
    provider = ScriptedRepairProvider(released_factory.epoch.deployment)
    backend = PassingCommandBackend()
    result = execute_harness_solve(
        released_factory.project_root,
        task,
        provider=provider,
        command_backend=backend,
        run_artifact_workspace=workspace,
        run_id="run.replay.sdk",
        workspace_id="workspace.replay.sdk",
        pair_key=_pair_key(task, released_factory.epoch),
    )

    assert result.status == "completed"
    assert result.run_id == "run.replay.sdk"
    assert result.workspace_id == "workspace.replay.sdk"
    assert {request.run_id for request in provider.requests} == {"run.replay.sdk"}
    assert result.eligible_for_evaluator_submission
    assert result.controlled_run_evidence is not None
    assert result.controlled_run_evidence.relative_path == CONTROLLED_RUN_EVIDENCE_REF
    run_evidence = load_controlled_run_evidence(
        workspace,
        result.controlled_run_evidence,
    )
    assert run_evidence.pair_key == _pair_key(task, released_factory.epoch)
    assert run_evidence.execution_mode == "deterministic_replay"
    assert run_evidence.live_inference_status == "not_run"
    assert run_evidence.real_inference_requests_sent == 0
    assert result.capability_promotion_authorized is False
    assert result.release.release_digest == released_factory.release_digest
    assert result.task.task_envelope_digest == task.task_manifest_digest
    assert result.submitted_patch is not None
    assert '-    return "old"' in result.submitted_patch.unified_diff
    assert '+    return "new"' in result.submitted_patch.unified_diff
    assert result.public_verification.status == "passed"
    assert result.termination.status == "completed"
    assert result.budget.model_calls == len(provider.requests) == 9
    assert result.budget.reconciled and result.budget.healthy
    requested_tools = {
        round_.tool_request_id and request.tool_results[-1].tool_id
        for request, round_ in zip(provider.requests[1:], result.evidence.provider_rounds[1:])
        if request.tool_results and round_.response_kind == "tool_request"
    }
    evidence_tools = {receipt.tool_id for receipt in result.evidence.tool_receipts}
    assert evidence_tools == set(REPO_REPAIR_TRUSTED_TOOL_IDS)
    assert requested_tools <= evidence_tools
    assert len(backend.requests) == 3
    assert all(item is None for item in provider.credential_references)
    assert (source / "src/app.py").read_text(encoding="utf-8") == (
        'def value():\n    return "old"\n'
    )
    assert (workspace / "repository/working/src/app.py").read_text(encoding="utf-8") == (
        'def value():\n    return "new"\n'
    )
    persisted = HarnessSolveResult.model_validate_json(
        (workspace / HARNESS_SOLVE_RESULT_FILE).read_text(encoding="utf-8")
    )
    assert persisted == result
    assert "SCRIPTED_PROVIDER_API_KEY" not in (workspace / HARNESS_SOLVE_RESULT_FILE).read_text(
        encoding="utf-8"
    )


def test_sdk_public_tests_mutate_only_disposable_workspaces(
    released_factory: ReleasedFactory,
    tmp_path: Path,
) -> None:
    source = _source_repository(tmp_path)
    task = _task(released_factory.epoch, source)
    workspace = tmp_path / "mutating-public-test-run"
    backend = MutatingPublicTestBackend()

    result = execute_harness_solve(
        released_factory.project_root,
        task,
        provider=ScriptedRepairProvider(released_factory.epoch.deployment),
        command_backend=backend,
        run_artifact_workspace=workspace,
        run_id="run.disposable.public",
        workspace_id="workspace.disposable.public",
        pair_key=_pair_key(task, released_factory.epoch),
    )

    working = (workspace / "repository/working").resolve()
    assert result.status == "public_verification_failed"
    assert result.public_verification.status == "failed"
    assert result.termination.status == "public_verification_failed"
    assert result.final_workspace_digest == repository_snapshot_digest(working)
    assert backend.request_workspace_digests == [
        task.workspace_snapshot.digest,
        result.final_workspace_digest,
        result.final_workspace_digest,
    ]
    assert all(request.workspace != working for request in backend.requests)
    assert all(not request.workspace.exists() for request in backend.requests)
    assert (working / "src/app.py").read_text(encoding="utf-8") == (
        'def value():\n    return "new"\n'
    )
    assert not (working / "src/__pycache__").exists()
    assert result.submitted_patch is not None
    assert "verification-only" not in result.submitted_patch.unified_diff
    public_receipts = [
        receipt
        for receipt in result.evidence.tool_receipts
        if receipt.tool_id == "repo.public_test"
    ]
    assert [receipt.status for receipt in public_receipts] == ["failed"] * 3
    assert [receipt.phase for receipt in public_receipts] == [
        "actor_tool",
        "actor_tool",
        "terminal_public_verification",
    ]
    assert result.public_verification.receipt_ids == result.evidence.public_verification_receipt_ids
    assert len(result.public_verification.command_evidence_digests) == 1


def test_fake_live_provider_provenance_is_cross_bound_to_every_provider_round(
    released_factory: ReleasedFactory,
    tmp_path: Path,
) -> None:
    source = _source_repository(tmp_path)
    task = _task(released_factory.epoch, source)
    workspace = tmp_path / "live-run-artifacts"
    provider = FakeLiveRepairProvider(released_factory.epoch.deployment)

    result = execute_harness_solve(
        released_factory.project_root,
        task,
        provider=provider,
        command_backend=PassingCommandBackend(),
        run_artifact_workspace=workspace,
        pair_key=_pair_key(task, released_factory.epoch),
    )

    assert result.execution_mode == "live_provider"
    assert result.live_inference_status == "completed"
    assert result.real_inference_requests_sent == len(provider.requests) == 9
    assert result.controlled_run_evidence is not None
    evidence = load_controlled_run_evidence(
        workspace,
        result.controlled_run_evidence,
    )
    assert evidence.real_inference_requests_sent == len(evidence.provider_calls) == 9
    assert all(reference is not None for reference in provider.credential_references)


@pytest.mark.parametrize("final_report", ["stale_not_run", "wrong_count"])
def test_fake_live_provider_cannot_report_success_without_exact_request_provenance(
    released_factory: ReleasedFactory,
    tmp_path: Path,
    final_report: str,
) -> None:
    source = _source_repository(tmp_path / final_report)
    task = _task(released_factory.epoch, source)
    workspace = tmp_path / f"live-{final_report}-run"
    provider = FakeLiveRepairProvider(
        released_factory.epoch.deployment,
        final_report=final_report,
    )

    with pytest.raises(HarnessSolveError) as error:
        execute_harness_solve(
            released_factory.project_root,
            task,
            provider=provider,
            command_backend=PassingCommandBackend(),
            run_artifact_workspace=workspace,
            pair_key=_pair_key(task, released_factory.epoch),
        )

    assert error.value.code == "provider_provenance_incomplete"
    assert not (workspace / HARNESS_SOLVE_RESULT_FILE).exists()


def test_sdk_solve_receives_only_same_release_public_session_carryover(
    released_factory: ReleasedFactory,
    tmp_path: Path,
) -> None:
    source = _source_repository(tmp_path)
    task = _task(released_factory.epoch, source)
    store = HarnessSessionStore(released_factory.project_root)
    store.create_session(
        active_release_digest=released_factory.release_digest,
        session_id="session.sdk.continued",
    )
    store.append_message(
        "session.sdk.continued",
        active_release_digest=released_factory.release_digest,
        expected_version=0,
        message_summary="Previous public solve retained a concise public result.",
        carryover=(
            {
                "artifact_ref": "runs/run.previous/harness_solve_result.json",
                "artifact_digest": _digest("previous-public-result"),
                "summary": "Previous public result changed value from old to new.",
            },
        ),
    )
    session_context = store.context_for_next(
        "session.sdk.continued",
        active_release_digest=released_factory.release_digest,
    ).to_public_runtime_context()
    request = HarnessSolveFileRequest(
        task=task,
        execution=HarnessReplayExecution(
            provider_manifest_path="provider-replay.json",
            command_manifest_path="command-replay.json",
        ),
        run_artifact_workspace=str(tmp_path / "request-round-trip"),
        run_id="run.session.request",
        workspace_id="workspace.session.request",
        session_context=session_context,
    )
    round_trip = HarnessSolveFileRequest.model_validate_json(request.model_dump_json())
    assert round_trip.session_context == session_context
    assert "payload" not in request.model_dump_json().casefold()
    provider = ScriptedRepairProvider(released_factory.epoch.deployment)
    backend = PassingCommandBackend()

    result = execute_harness_solve(
        released_factory.project_root,
        task,
        provider=provider,
        command_backend=backend,
        run_artifact_workspace=tmp_path / "continued-run",
        run_id="run.session.continued",
        workspace_id="workspace.session.continued",
        public_session_context=session_context,
    )

    assert result.status == "completed"
    first_read = next(
        read
        for read in provider.requests[0].context.reads
        if read.source_kind == "session"
    )
    assert first_read.provenance_ref == session_context.context_digest
    assert first_read.value["session_id"] == "session.sdk.continued"
    assert first_read.value["carryover"] == [
        session_context.carryover[0].model_dump(mode="json")
    ]
    serialized = json.dumps(first_read.value, sort_keys=True)
    assert "Previous public result" in serialized
    assert "payload" not in serialized.casefold()
    assert "repository_snapshot" not in serialized.casefold()
    assert "workspace_snapshot" not in serialized.casefold()

    fresh_provider = ScriptedRepairProvider(released_factory.epoch.deployment)
    fresh_backend = PassingCommandBackend()
    execute_harness_solve(
        released_factory.project_root,
        task,
        provider=fresh_provider,
        command_backend=fresh_backend,
        run_artifact_workspace=tmp_path / "fresh-run",
        run_id="run.session.fresh",
        workspace_id="workspace.session.fresh",
    )
    fresh_read = next(
        read
        for read in fresh_provider.requests[0].context.reads
        if read.source_kind == "session"
    )
    assert fresh_read.value["session_id"] is None
    assert fresh_read.value["carryover"] == []
    assert fresh_read.value_digest != first_read.value_digest


def test_release_mismatched_public_session_context_fails_before_execution(
    released_factory: ReleasedFactory,
    tmp_path: Path,
) -> None:
    source = _source_repository(tmp_path)
    task = _task(released_factory.epoch, source)
    store = HarnessSessionStore(released_factory.project_root)
    store.create_session(
        active_release_digest=released_factory.release_digest,
        session_id="session.sdk.mismatch",
    )
    context = store.context_for_next(
        "session.sdk.mismatch",
        active_release_digest=released_factory.release_digest,
    ).to_public_runtime_context()
    payload = context.model_dump(mode="python", exclude={"context_digest"})
    payload["active_release_digest"] = _digest("other-release")
    mismatched = HarnessPublicSessionContext.model_validate(payload)
    provider = ScriptedRepairProvider(released_factory.epoch.deployment)
    backend = PassingCommandBackend()
    workspace = tmp_path / "mismatched-run"

    with pytest.raises(HarnessSolveError, match="different immutable release"):
        execute_harness_solve(
            released_factory.project_root,
            task,
            provider=provider,
            command_backend=backend,
            run_artifact_workspace=workspace,
            run_id="run.session.mismatch",
            workspace_id="workspace.session.mismatch",
            public_session_context=mismatched,
        )

    assert provider.requests == []
    assert backend.requests == []
    assert not workspace.exists()


def test_implicit_workspaces_are_collision_free_under_the_caller_run_root(
    released_factory: ReleasedFactory,
    tmp_path: Path,
) -> None:
    source = _source_repository(tmp_path)
    task = _task(released_factory.epoch, source)
    run_root = tmp_path / "runs"

    first = execute_harness_solve(
        released_factory.project_root,
        task,
        provider=ScriptedRepairProvider(released_factory.epoch.deployment),
        command_backend=PassingCommandBackend(),
        run_root=run_root,
    )
    second = execute_harness_solve(
        released_factory.project_root,
        task,
        provider=ScriptedRepairProvider(released_factory.epoch.deployment),
        command_backend=PassingCommandBackend(),
        run_root=run_root,
    )

    children = [path for path in run_root.iterdir() if path.is_dir()]
    assert first.run_id != second.run_id
    assert first.workspace_id != second.workspace_id
    assert len(children) == 2
    assert all((path / HARNESS_SOLVE_RESULT_FILE).is_file() for path in children)


def test_explicit_workspace_reuse_is_rejected_before_provider_dispatch(
    released_factory: ReleasedFactory,
    tmp_path: Path,
) -> None:
    source = _source_repository(tmp_path)
    task = _task(released_factory.epoch, source)
    workspace = tmp_path / "single-use-run"
    execute_harness_solve(
        released_factory.project_root,
        task,
        provider=ScriptedRepairProvider(released_factory.epoch.deployment),
        command_backend=PassingCommandBackend(),
        run_artifact_workspace=workspace,
    )
    rejected_provider = ScriptedRepairProvider(released_factory.epoch.deployment)

    with pytest.raises(HarnessSolveError) as raised:
        execute_harness_solve(
            released_factory.project_root,
            task,
            provider=rejected_provider,
            command_backend=PassingCommandBackend(),
            run_artifact_workspace=workspace,
        )

    assert raised.value.code == "workspace_already_exists"
    assert rejected_provider.requests == []


def test_task_release_and_snapshot_mismatches_fail_before_workspace_creation(
    released_factory: ReleasedFactory,
    tmp_path: Path,
) -> None:
    source = _source_repository(tmp_path)
    wrong_epoch_task = _task(
        released_factory.epoch,
        source,
        epoch_manifest_digest=_digest("wrong-epoch"),
    )
    mismatch_workspace = tmp_path / "epoch-mismatch-run"
    with pytest.raises(HarnessSolveError) as epoch_error:
        execute_harness_solve(
            released_factory.project_root,
            wrong_epoch_task,
            provider=ScriptedRepairProvider(released_factory.epoch.deployment),
            command_backend=PassingCommandBackend(),
            run_artifact_workspace=mismatch_workspace,
        )
    assert epoch_error.value.code == "task_release_mismatch"
    assert not mismatch_workspace.exists()

    wrong_snapshot_task = _task(
        released_factory.epoch,
        source,
        snapshot_digest=_digest("wrong-snapshot"),
    )
    snapshot_workspace = tmp_path / "snapshot-mismatch-run"
    with pytest.raises(HarnessSolveError) as snapshot_error:
        execute_harness_solve(
            released_factory.project_root,
            wrong_snapshot_task,
            provider=ScriptedRepairProvider(released_factory.epoch.deployment),
            command_backend=PassingCommandBackend(),
            run_artifact_workspace=snapshot_workspace,
        )
    assert snapshot_error.value.code == "snapshot_validation_failed"
    assert not snapshot_workspace.exists()


def test_old_runtime_kind_is_rejected(
    released_factory: ReleasedFactory,
    tmp_path: Path,
) -> None:
    old_project = tmp_path / "old-runtime"
    old_project.mkdir()
    (old_project / "active_release.json").write_text(
        json.dumps(
            {
                "runtime_kind": "langgraph",
                "release_digest": _digest("old-release"),
                "release_path": f"releases/{_digest('old-release')}",
                "manifest_digest": _digest("old-manifest"),
            }
        ),
        encoding="utf-8",
    )
    source = _source_repository(tmp_path / "old-kind-task")
    task = _task(released_factory.epoch, source)
    with pytest.raises(HarnessSolveError) as old_kind:
        execute_harness_solve(
            old_project,
            task,
            provider=ScriptedRepairProvider(released_factory.epoch.deployment),
            command_backend=PassingCommandBackend(),
            run_artifact_workspace=tmp_path / "old-kind-run",
    )
    assert old_kind.value.code == "unsupported_runtime_kind"


def test_provider_deployment_and_accounting_failures_never_promote(
    released_factory: ReleasedFactory,
    tmp_path: Path,
) -> None:
    source = _source_repository(tmp_path)
    task = _task(released_factory.epoch, source)
    crossed = released_factory.epoch.deployment.model_copy(
        update={"model": "crossed-model"}
    )
    mismatch_workspace = tmp_path / "provider-mismatch-run"
    with pytest.raises(HarnessSolveError) as mismatch:
        execute_harness_solve(
            released_factory.project_root,
            task,
            provider=ScriptedRepairProvider(crossed),
            command_backend=PassingCommandBackend(),
            run_artifact_workspace=mismatch_workspace,
        )
    assert mismatch.value.code == "provider_deployment_mismatch"
    assert not mismatch_workspace.exists()

    failure_workspace = tmp_path / "provider-accounting-run"
    provider = AccountingFailureProvider(released_factory.epoch.deployment)
    result = execute_harness_solve(
        released_factory.project_root,
        task,
        provider=provider,
        command_backend=PassingCommandBackend(),
        run_artifact_workspace=failure_workspace,
    )
    assert provider.calls == 1
    assert result.status == "failed"
    assert result.submitted_patch is None
    assert not result.eligible_for_evaluator_submission
    assert result.capability_promotion_authorized is False
    assert result.failure is not None and result.failure.provider is not None
    assert result.failure.kind == "provider_call_failed"
    assert result.failure.provider.failure_kind == "post_send_failure"
    assert result.failure.provider.request_sent
    assert not result.failure.provider.accounting_healthy
    assert result.budget.unknown_usage_events == 1
    assert result.public_verification.status == "not_run"
    assert (failure_workspace / HARNESS_SOLVE_RESULT_FILE).is_file()
    assert (source / "src/app.py").read_text(encoding="utf-8") == (
        'def value():\n    return "old"\n'
    )


def test_source_hidden_bundle_imports_executor_and_validates_active_release(
    released_factory: ReleasedFactory,
) -> None:
    bundle_root = (
        released_factory.project_root
        / "releases"
        / released_factory.release_digest
        / "runtime/runtime_sdk"
    )
    script = (
        "import sys; "
        f"sys.path.insert(0, {str(bundle_root)!r}); "
        "from agintor_runtime.runtime.sdk.harness_executor import execute_harness_solve; "
        "from agintor_runtime.runtime.sdk.harness_release_loader import load_active_harness_release; "
        f"release = load_active_harness_release({str(released_factory.project_root)!r}); "
        "assert callable(execute_harness_solve); "
        "print(release.manifest.release_digest)"
    )

    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script],
        cwd=released_factory.project_root,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == released_factory.release_digest
