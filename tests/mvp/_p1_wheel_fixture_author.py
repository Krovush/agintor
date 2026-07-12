"""Author P1 replay fixtures using only an installed Agintor wheel.

This file is executed with ``python -I`` by the built-wheel end-to-end test.  It
is deliberately not imported by pytest: its value is proving that replay
fixtures can be authored against the same installed package later consumed by
the CLI, with no checkout package or test-helper imports on ``sys.path``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import agintor
from agintor.contracts.harness import HarnessArtifactChannel, HarnessPublicSessionContext
from agintor.contracts.harness_actions import (
    BudgetEffect,
    ChannelAddPatch,
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
from agintor.contracts.epochs import TaskEnvelope
from agintor.core.identity import canonical_identity_digest
from agintor.evaluation.contracts import EvaluationContract
from agintor.evaluation.runners.repo_patch_backends import IsolatedRepoPatchCommandBackend
from agintor.evaluation.runners.repo_patch_runner import RepoPatchEvaluatorRunner, RepoPatchFixture
from agintor.factory.harness_replay import (
    HarnessFactoryReplayRecorder,
    write_harness_factory_replay_manifest,
)
from agintor.factory.harness_service import HarnessFactoryBuildInput, build_harness_factory_release
from agintor.isolation.commands import (
    IsolatedCommandRequest,
    IsolatedCommandResult,
    IsolatedCommandStatus,
)
from agintor.isolation.replay import (
    IsolatedCommandReplayBinding,
    IsolatedCommandReplayRecorder,
    write_isolated_command_replay_manifest,
)
from agintor.runtime.api.composite_compiler import compile_composite_run_plan
from agintor.runtime.harness_profile import HarnessDeploymentProfile
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
)
from agintor.runtime.kernel.composite_replay_provider import (
    CompositeReplayBinding,
    CompositeReplayRecorder,
    write_composite_replay_manifest,
)
from agintor.runtime.kernel.composite_runtime import (
    ActorCallOutput,
    ActorCallRequest,
    ActorTerminalTurn,
    ActorToolRequest,
)
from agintor.runtime.sdk.harness_executor import execute_harness_solve
from agintor.runtime.sdk.harness_release_loader import load_active_harness_release


def _load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _digest(label: str) -> str:
    return canonical_identity_digest(label, domain="p1-wheel-fixture-author")


def _assert_installed(expected_root: str) -> str:
    package_path = Path(agintor.__file__).resolve()
    root = Path(expected_root).resolve()
    if root not in package_path.parents:
        raise RuntimeError(
            f"fixture author imported Agintor outside installed environment: {package_path}"
        )
    return str(package_path)


def _proposal(request: Any, *, index: int) -> tuple[SemanticTransactionProposal, ...]:
    channel_id = f"gain-{request.step_index}-{index}"
    channel = HarnessArtifactChannel(
        channel_id=channel_id,
        producer_actor_id="investigator",
        consumer_actor_id="implementer",
    )
    patch = ChannelAddPatch(channel=channel)
    return (
        SemanticTransactionProposal(
            transaction_id=f"txn.{channel_id}",
            operator=patch.operator,
            treatment_class="structural",
            proposal_source="matched_random",
            parent_source_protocol_digest=request.incumbent_protocol.source_digest(),
            parent_compiled_semantic_digest=(
                request.incumbent_anchor_plan.compiled_semantic_digest
            ),
            task_envelope_digest=request.anchor_task.task_manifest_digest,
            dependency_manifest_digest=request.dependency_manifest.manifest_digest(),
            mechanism_hypothesis="Exercise channel_add through the live artifact consumer.",
            applicability=TransactionApplicability(
                required_actor_ids=("investigator", "implementer"),
                absent_channel_ids=(channel_id,),
            ),
            normalized_patch=patch,
            touched_source_paths=(f"artifact_channels[{channel_id}]",),
            budget_effect=BudgetEffect(mode="unchanged"),
            predicted_trace_effect=PredictedTraceEffect(
                runtime_owner="artifact_store",
                trace_observation="artifact_deliveries",
                expected_effect="The named runtime consumer changes in evidence.",
            ),
        ),
    )


def _outcome_receipt(
    request: Any,
    pair_key: PairKey,
    build_input: HarnessFactoryBuildInput,
    task: TaskEnvelope,
    *,
    complete_repair: bool,
) -> OutcomeReceipt:
    dependencies = build_input.dependency_manifest
    epoch = build_input.epoch
    return OutcomeReceipt(
        receipt_id=f"receipt.{request.evaluation_id}.{pair_key.sampling_replicate}",
        execution_mode="deterministic_replay",
        live_inference_status="not_run",
        real_inference_requests_sent=0,
        data_state="development",
        epoch_id=epoch.epoch_id,
        epoch_manifest_digest=epoch.epoch_manifest_digest,
        release_digest=_digest("candidate-release"),
        release_manifest_digest=_digest("candidate-release-manifest"),
        profile_digest=request.deployment_profile_digest,
        split_manifest_digest=epoch.development_split_digest,
        task_manifest_id=task.task_manifest_id,
        task_manifest_digest=task.task_manifest_digest,
        evaluation_contract_id="evaluation.p1.wheel-author",
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
        command_container_policy_digest=epoch.deployment.command_container_policy_digest,
        evaluator_environment_digest=_digest(pair_key.environment_id),
        patch_digest=_digest(
            f"patch:{request.evaluation_id}:{pair_key.sampling_replicate}"
        ),
        complete_repair=complete_repair,
        health=OutcomeHealth(
            process_integrity=True,
            no_leakage=True,
            environment_integrity=True,
            evaluator_integrity=True,
            accounting_complete=True,
        ),
        cost=OutcomeCost(
            model_calls=2,
            input_tokens=1_000,
            output_tokens=500,
            cached_tokens=0,
            tool_calls=4,
            tool_output_bytes=2_000,
            artifact_bytes=1_000,
            patch_bytes=500,
            retries=0,
            wall_time_ms=5_000,
            known_cost_usd=0.2,
            estimated_cost_usd=0.0,
            unknown_dollars=False,
            within_epoch_envelope=True,
        ),
        diagnostics=(DiagnosticScore(name="ignored_trace_score", value=999.0),),
        issued_at_ms=1,
    )


def _proof_binding(
    request: Any,
    receipt: OutcomeReceipt,
    build_input: HarnessFactoryBuildInput,
) -> EvaluatorOutcomeProofBinding:
    plan = next(
        item.plan
        for item in request.compiled_plans
        if item.task_manifest_id == receipt.task_manifest_id
    )
    evidence_digest = _digest(f"run-evidence:{receipt.receipt_id}")
    epoch = build_input.epoch
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


def _factory_evaluator(build_input: HarnessFactoryBuildInput, calls: list[Any]):
    task_by_id = {task.task_manifest_id: task for task in build_input.task_panel}

    def evaluate(request: Any) -> tuple[EvaluatorOutcomeProofBinding, ...]:
        calls.append(request)
        bindings = []
        for pair_key in request.expected_pair_keys:
            if request.arm_kind in {"search_parent", "control"}:
                complete = pair_key.sampling_replicate == 0
            else:
                complete = any(
                    channel.channel_id.startswith("gain-")
                    for channel in request.protocol.artifact_channels
                ) or pair_key.sampling_replicate == 0
            receipt = _outcome_receipt(
                request,
                pair_key,
                build_input,
                task_by_id[pair_key.task_manifest_id],
                complete_repair=complete,
            )
            bindings.append(_proof_binding(request, receipt, build_input))
        return tuple(bindings)

    return evaluate


def _author_factory(args: argparse.Namespace) -> dict[str, Any]:
    build_input = HarnessFactoryBuildInput.model_validate(_load_json(args.build_input))
    proposal_calls: list[Any] = []
    evaluator_calls: list[Any] = []

    def proposals(request: Any) -> tuple[SemanticTransactionProposal, ...]:
        proposal_calls.append(request)
        return _proposal(request, index=args.proposal_index)

    recorder = HarnessFactoryReplayRecorder(
        build_input=build_input,
        proposal_callback=proposals,
        evaluator_callback=_factory_evaluator(build_input, evaluator_calls),
    )
    result = build_harness_factory_release(
        build_input,
        proposal_callback=recorder.proposal_callback,
        evaluator_callback=recorder.evaluator_callback,
    )
    if result.release_pointer is None:
        raise RuntimeError("factory fixture author did not publish a release")
    manifest_path = write_harness_factory_replay_manifest(
        args.output,
        recorder.manifest(manifest_id=args.manifest_id),
    )
    return {
        "manifest_path": str(manifest_path),
        "release_digest": result.release_pointer.release_digest,
        "proposal_calls": len(proposal_calls),
        "evaluator_calls": len(evaluator_calls),
        "execution_mode": result.execution_mode,
    }


class _ExecutingHostCommandBackend:
    def __init__(self) -> None:
        self.requests: list[IsolatedCommandRequest] = []

    @staticmethod
    def _resolved_argv(command: tuple[str, ...]) -> list[str]:
        argv = list(command)
        if argv[0] in {"python", "python3"}:
            argv[0] = sys.executable
        elif argv[0] == "git":
            git = shutil.which("git")
            if git is None:
                raise RuntimeError("git is required for evaluator replay recording")
            argv[0] = git
        return argv

    def run(self, request: IsolatedCommandRequest) -> IsolatedCommandResult:
        self.requests.append(request)
        working_directory = request.workspace
        if request.working_directory != ".":
            working_directory = request.workspace / request.working_directory
        started = time.perf_counter()
        environment = {
            name: os.environ[name]
            for name in ("SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP")
            if name in os.environ
        }
        environment.update(request.environment)
        try:
            completed = subprocess.run(
                self._resolved_argv(request.command),
                cwd=working_directory,
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                shell=False,
                check=False,
                timeout=request.timeout_s,
            )
            status = IsolatedCommandStatus.COMPLETED
            exit_code: int | None = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
            failure_detail = None
        except subprocess.TimeoutExpired as exc:
            status = IsolatedCommandStatus.TIMED_OUT
            exit_code = None
            stdout = exc.stdout or b""
            stderr = exc.stderr or b""
            failure_detail = "trusted recording command timed out"
        except Exception as exc:
            status = IsolatedCommandStatus.LAUNCH_FAILED
            exit_code = None
            stdout = b""
            stderr = str(exc).encode("utf-8")
            failure_detail = type(exc).__name__
        return IsolatedCommandResult(
            status=status,
            command=request.command,
            container_name=f"trusted-recording-{len(self.requests)}",
            exit_code=exit_code,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            stdout_digest=hashlib.sha256(stdout).hexdigest(),
            stderr_digest=hashlib.sha256(stderr).hexdigest(),
            duration_s=max(time.perf_counter() - started, 0.0),
            failure_detail=failure_detail,
        )


class _RecordingCommandBackend:
    def __init__(
        self,
        delegate: _ExecutingHostCommandBackend,
        recorder: IsolatedCommandReplayRecorder,
        *,
        policy: Any,
    ) -> None:
        self.delegate = delegate
        self.recorder = recorder
        self.policy = policy

    def run(self, request: IsolatedCommandRequest) -> IsolatedCommandResult:
        captured = self.recorder.capture_request(request)
        result = self.delegate.run(request)
        self.recorder.record(
            request=captured,
            result=result,
            workspace_after=request.workspace,
        )
        return result


class _PlanAwareRepairProvider:
    execution_provenance = ProviderExecutionProvenance(
        execution_mode="deterministic_replay",
        live_inference_status="not_run",
        real_inference_requests_sent=0,
    )

    def __init__(self, plan: Any, deployment: Any, *, replacement: str) -> None:
        self.plan = plan
        self.deployment_identity = deployment
        self.replacement = replacement
        self.requests: list[ActorCallRequest] = []

    def invoke(
        self,
        request: Any,
        *,
        control: ProviderCallControl,
        credential_reference: CredentialReference | None,
    ) -> ProviderInvocation:
        del control
        if credential_reference is not None:
            raise RuntimeError("offline fixture author received a credential reference")
        normalized = ActorCallRequest.model_validate(request)
        self.requests.append(normalized)
        planned = next(
            call for call in self.plan.actor_calls if call.call_id == normalized.call_id
        )
        request_id = f"{normalized.call_id}.tool.{normalized.turn_index}"
        if not planned.emits_final_patch:
            actions: tuple[tuple[str, dict[str, Any]], ...] = (
                ("repo.search", {"query": "VALUE", "path": "src"}),
                ("repo.read", {"path": "src/app.py"}),
            )
            if normalized.turn_index < len(actions):
                tool_id, arguments = actions[normalized.turn_index]
                response: ActorToolRequest | ActorTerminalTurn = ActorToolRequest(
                    request_id=request_id,
                    tool_id=tool_id,
                    arguments=arguments,
                )
            else:
                response = ActorTerminalTurn(
                    output=ActorCallOutput(
                        output_text="Located the public VALUE regression from repository evidence.",
                        artifact_payloads={
                            write.artifact_id: (
                                "src/app.py contains VALUE = 1 and public evidence requires "
                                "a VALUE = 2 prefix."
                            )
                            for write in planned.artifact_writes
                        },
                    )
                )
        else:
            actions = (
                ("repo.edit", {"path": "src/app.py", "content": self.replacement}),
                ("repo.public_test", {}),
                ("repo.diff", {}),
            )
            if normalized.turn_index < len(actions):
                tool_id, arguments = actions[normalized.turn_index]
                response = ActorToolRequest(
                    request_id=request_id,
                    tool_id=tool_id,
                    arguments=arguments,
                )
            else:
                response = ActorTerminalTurn(
                    output=ActorCallOutput(
                        output_text="Submitted the repository-derived candidate patch.",
                        artifact_payloads={
                            write.artifact_id: "Implementation and verification evidence complete."
                            for write in planned.artifact_writes
                        },
                        final_patch=normalized.tool_results[-1].output["patch"],
                    )
                )
        return ProviderInvocation(
            response=response,
            usage=ProviderUsageReport(
                usage_status=UsageStatus.KNOWN,
                input_tokens=10,
                output_tokens=8,
                cached_tokens=0,
                cost_status=CostStatus.KNOWN,
                cost_usd=0.0,
                response_id=f"p1.recorded.{len(self.requests)}",
            ),
        )


class _RecordingProvider:
    execution_provenance = _PlanAwareRepairProvider.execution_provenance

    def __init__(
        self,
        delegate: _PlanAwareRepairProvider,
        recorder: CompositeReplayRecorder,
    ) -> None:
        self.delegate = delegate
        self.recorder = recorder
        self.deployment_identity = delegate.deployment_identity

    def invoke(
        self,
        request: Any,
        *,
        control: ProviderCallControl,
        credential_reference: CredentialReference | None,
    ) -> ProviderInvocation:
        invocation = self.delegate.invoke(
            request,
            control=control,
            credential_reference=credential_reference,
        )
        self.recorder.record_invocation(request=request, invocation=invocation)
        return invocation


def _session_context(path: str | None) -> HarnessPublicSessionContext | None:
    if path is None:
        return None
    return HarnessPublicSessionContext.model_validate(_load_json(path))


def _author_solve(args: argparse.Namespace) -> dict[str, Any]:
    project = Path(args.project)
    task = TaskEnvelope.model_validate(_load_json(args.task))
    pair_key = PairKey.model_validate(_load_json(args.pair_key))
    session_context = _session_context(args.session_context)
    release = load_active_harness_release(project)
    plan = compile_composite_run_plan(task, release.protocol, release.dependencies)
    provider_recorder = CompositeReplayRecorder(
        CompositeReplayBinding.from_runtime_inputs(
            release_digest=release.manifest.release_digest,
            task=task,
            deployment=release.manifest.deployment,
            plan=plan,
            public_session_context=session_context,
        )
    )
    command_recorder = IsolatedCommandReplayRecorder(
        IsolatedCommandReplayBinding.from_runtime_inputs(
            release_digest=release.manifest.release_digest,
            task=task,
            command_policy_digest=(
                release.manifest.deployment.command_container_policy_digest
            ),
        )
    )
    host = _ExecutingHostCommandBackend()
    profile = HarnessDeploymentProfile.model_validate(release.profile.profile)
    provider = _RecordingProvider(
        _PlanAwareRepairProvider(
            plan,
            release.manifest.deployment,
            replacement=args.replacement.replace("\\n", "\n"),
        ),
        provider_recorder,
    )
    backend = _RecordingCommandBackend(
        host,
        command_recorder,
        policy=profile.command_container_policy.to_isolated_command_policy(),
    )
    destination = Path(args.output_root)
    recorded = execute_harness_solve(
        project,
        task,
        provider=provider,
        command_backend=backend,
        run_artifact_workspace=destination / "recording-run",
        run_id=args.run_id,
        workspace_id=args.workspace_id,
        pair_key=pair_key,
        public_session_context=session_context,
    )
    if recorded.status != "completed" or recorded.submitted_patch is None:
        raise RuntimeError(recorded.model_dump_json(indent=2))
    if not any(request.command == task.public_reproduction[0].argv for request in host.requests):
        raise RuntimeError("solve fixture author did not execute the public reproduction")
    provider_path = write_composite_replay_manifest(
        destination / "provider-replay.json",
        provider_recorder.finalize(),
    )
    command_path = write_isolated_command_replay_manifest(
        destination / "command-replay.json",
        command_recorder.finalize(),
    )
    return {
        "provider_manifest_path": str(provider_path),
        "command_manifest_path": str(command_path),
        "submitted_patch": recorded.submitted_patch.unified_diff,
        "executed_commands": len(host.requests),
    }


def _author_evaluator(args: argparse.Namespace) -> dict[str, Any]:
    project = Path(args.project)
    task = TaskEnvelope.model_validate(_load_json(args.task))
    contract = EvaluationContract.model_validate(_load_json(args.contract))
    patch = Path(args.patch).read_text(encoding="utf-8")
    release = load_active_harness_release(project)
    profile = HarnessDeploymentProfile.model_validate(release.profile.profile)
    recorder = IsolatedCommandReplayRecorder(
        IsolatedCommandReplayBinding.from_runtime_inputs(
            release_digest=release.manifest.release_digest,
            task=task,
            command_policy_digest=(
                release.manifest.deployment.command_container_policy_digest
            ),
        )
    )
    host = _ExecutingHostCommandBackend()
    recording = _RecordingCommandBackend(
        host,
        recorder,
        policy=profile.command_container_policy.to_isolated_command_policy(),
    )
    adapter = IsolatedRepoPatchCommandBackend(
        recording,
        environment_identity=profile.command_container_policy.model_dump(mode="json"),
    )
    fixture = RepoPatchFixture.from_evaluation_contract(
        contract,
        public_test_commands=task.public_reproduction,
    )
    result = RepoPatchEvaluatorRunner(adapter).run(
        candidate_artifact=patch,
        fixture=fixture,
    )
    expected = args.expected_complete_repair == "true"
    if result.complete_repair is not expected:
        raise RuntimeError(result.model_dump_json(indent=2))
    manifest_path = write_isolated_command_replay_manifest(
        Path(args.output_root) / "evaluator-command-replay.json",
        recorder.finalize(),
    )
    return {
        "command_manifest_path": str(manifest_path),
        "complete_repair": result.complete_repair,
        "executed_commands": len(host.requests),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-package-root", required=True)
    subparsers = parser.add_subparsers(dest="operation", required=True)

    factory = subparsers.add_parser("factory")
    factory.add_argument("--build-input", required=True)
    factory.add_argument("--output", required=True)
    factory.add_argument("--manifest-id", required=True)
    factory.add_argument("--proposal-index", type=int, default=0)

    solve = subparsers.add_parser("solve")
    solve.add_argument("--project", required=True)
    solve.add_argument("--task", required=True)
    solve.add_argument("--pair-key", required=True)
    solve.add_argument("--session-context")
    solve.add_argument("--replacement", required=True)
    solve.add_argument("--output-root", required=True)
    solve.add_argument("--run-id", required=True)
    solve.add_argument("--workspace-id", required=True)

    evaluator = subparsers.add_parser("evaluator")
    evaluator.add_argument("--project", required=True)
    evaluator.add_argument("--task", required=True)
    evaluator.add_argument("--contract", required=True)
    evaluator.add_argument("--patch", required=True)
    evaluator.add_argument("--expected-complete-repair", choices=("true", "false"), required=True)
    evaluator.add_argument("--output-root", required=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    imported_from = _assert_installed(args.expected_package_root)
    if args.operation == "factory":
        result = _author_factory(args)
    elif args.operation == "solve":
        result = _author_solve(args)
    else:
        result = _author_evaluator(args)
    print(json.dumps({"imported_from": imported_from, **result}, sort_keys=True))


if __name__ == "__main__":
    main()
