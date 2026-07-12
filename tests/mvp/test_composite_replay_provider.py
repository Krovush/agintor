from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any

import pytest
from pydantic import ValidationError

from agintor.contracts.epochs import (
    DeploymentIdentity,
    PublicReproductionStep,
    TaskCeilings,
    TaskEnvelope,
    WorkspaceSnapshotRef,
)
from agintor.contracts.harness import (
    DependencyRef,
    NO_PUBLIC_SESSION_CONTEXT_DIGEST,
    RuntimeDependencyManifest,
    TrustedToolDependency,
)
from agintor.isolation.commands import (
    IsolatedCommandRequest,
    IsolatedCommandResult,
    IsolatedCommandStatus,
)
from agintor.repositories.workspaces import (
    TaskWorkspace,
    materialize_task_workspace,
    repository_snapshot_digest,
)
from agintor.runtime.api.composite_compiler import (
    compile_composite_run_plan,
    load_canonical_harness_seed,
)
from agintor.runtime.kernel.composite_budget import (
    AggregateBudgetLedger,
    CostStatus,
    ProviderUsageReport,
    UsageStatus,
)
from agintor.runtime.kernel.composite_provider import (
    CompositeProviderController,
    CredentialReference,
    ProviderCallControl,
    ProviderCallStatus,
    ProviderFailureKind,
    ProviderInvocation,
)
from agintor.runtime.kernel.composite_replay_provider import (
    CompositeReplayBinding,
    CompositeReplayInvocationError,
    CompositeReplayManifest,
    CompositeReplayMismatchError,
    CompositeReplayProvider,
    CompositeReplayRecorder,
    CompositeReplayRow,
    load_composite_replay_manifest,
    write_composite_replay_manifest,
)
from agintor.runtime.kernel.composite_runtime import (
    ActualContextRead,
    ActorCallOutput,
    ActorCallRequest,
    ActorTerminalTurn,
    ActorToolRequest,
    CompositeRuntime,
    PreCallContextManifest,
    ScratchWorkspaceBinding,
)
from agintor.runtime.kernel.repair_tools import TrustedRepairToolService
from agintor.utils import count_tokens_rough


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _deployment() -> DeploymentIdentity:
    return DeploymentIdentity(
        deployment_id="offline-replay-deployment",
        provider="offline-replay",
        model="recorded-transcript-v1",
        provider_config_digest=_digest("provider-config"),
        decoding_policy_digest=_digest("decoding-policy"),
        price_schedule_digest=_digest("price-schedule"),
        command_container_policy_digest=_digest("command-container-policy"),
    )


def _binding(*, compiled_digest: str = _digest("compiled")) -> CompositeReplayBinding:
    return CompositeReplayBinding(
        release_digest=_digest("release"),
        epoch_id="repo-repair-development",
        epoch_manifest_digest=_digest("epoch"),
        deployment=_deployment(),
        source_protocol_digest=_digest("protocol"),
        task_envelope_digest=_digest("task"),
        compiled_semantic_digest=compiled_digest,
        public_session_context_digest=NO_PUBLIC_SESSION_CONTEXT_DIGEST,
    )


def _request(
    label: str,
    *,
    compiled_digest: str = _digest("compiled"),
) -> ActorCallRequest:
    context = PreCallContextManifest(
        call_id="actor.offline.initial",
        actor_id="offline",
        task_envelope_digest=_digest("task"),
        reads=(),
    )
    return ActorCallRequest(
        run_id=f"run.{label}",
        compiled_semantic_digest=compiled_digest,
        call_id="actor.offline.initial",
        actor_id="offline",
        call_kind="initial",
        instruction=f"Respond deterministically for {label}.",
        allowed_tool_ids=("repo.read",),
        budget_share_bps=10_000,
        context=context,
        input_token_estimate=10,
        max_output_tokens=20,
    )


def _turn(label: str) -> ActorTerminalTurn:
    return ActorTerminalTurn(output=ActorCallOutput(output_text=f"recorded {label}"))


def _usage(label: str, *, input_tokens: int = 10) -> ProviderUsageReport:
    return ProviderUsageReport(
        usage_status=UsageStatus.KNOWN,
        input_tokens=input_tokens,
        output_tokens=3,
        cached_tokens=0,
        cost_status=CostStatus.KNOWN,
        cost_usd=0.0,
        response_id=f"response.{label}",
    )


def _manifest(*requests: ActorCallRequest) -> CompositeReplayManifest:
    rows = tuple(
        CompositeReplayRow(
            sequence_no=index,
            request_digest=request.request_digest,
            turn=_turn(str(index)),
            usage=_usage(str(index)),
        )
        for index, request in enumerate(requests)
    )
    return CompositeReplayManifest(binding=_binding(), rows=rows)


def _control(
    *,
    cancellation_event: threading.Event | None = None,
    deadline_monotonic: float | None = None,
) -> ProviderCallControl:
    return ProviderCallControl(
        reservation_id="provider.reservation.test",
        timeout_ms=5_000,
        deadline_monotonic=deadline_monotonic or time.monotonic() + 5.0,
        cancellation_event=cancellation_event or threading.Event(),
    )


def _ceilings() -> TaskCeilings:
    return TaskCeilings(
        max_model_calls=8,
        max_input_tokens=50_000,
        max_output_tokens=20_000,
        max_cached_tokens=0,
        max_tool_calls=20,
        max_tool_output_bytes=300_000,
        max_artifact_bytes=40_000,
        max_patch_bytes=20_000,
        max_retries=1,
        max_wall_time_ms=30_000,
        provider_deadline_ms=5_000,
        max_known_cost_usd=1.0,
        max_estimated_cost_usd=2.0,
    )


def test_exact_order_mismatch_is_pre_send_and_does_not_consume_or_poison_accounting() -> None:
    first = _request("first")
    second = _request("second")
    provider = CompositeReplayProvider(
        _manifest(first, second),
        expected_binding=_binding(),
    )
    ledger = AggregateBudgetLedger(_ceilings())

    mismatch = CompositeProviderController(ledger).call(
        provider,
        second,
        input_tokens=second.input_token_estimate,
        max_output_tokens=second.max_output_tokens,
        estimated_cost_usd=0.1,
    )

    assert mismatch.status is ProviderCallStatus.FAILED
    assert mismatch.failure.kind is ProviderFailureKind.PRE_SEND_FAILURE
    assert mismatch.failure.request_sent is False
    assert mismatch.ledger.model_calls == 0
    assert mismatch.ledger.unknown_usage_events == 0
    assert mismatch.ledger.unknown_cost_events == 0
    assert provider.reconciliation().consumed_count == 0

    invocation = provider.invoke(
        first,
        control=_control(),
        credential_reference=None,
    )
    assert invocation.response == _turn("0")
    assert invocation.usage == _usage("0")
    assert provider.reconciliation().consumed_count == 1


def test_identity_and_credential_mismatches_are_rejected_before_consumption() -> None:
    request = _request("identity")
    manifest = _manifest(request)
    crossed_binding = _binding().model_copy(update={"release_digest": _digest("other-release")})
    with pytest.raises(CompositeReplayMismatchError) as crossed:
        CompositeReplayProvider(manifest, expected_binding=crossed_binding)
    assert crossed.value.code == "identity_mismatch"

    provider = CompositeReplayProvider(manifest, expected_binding=_binding())
    wrong_plan_request = _request("wrong-plan", compiled_digest=_digest("another-plan"))
    with pytest.raises(CompositeReplayInvocationError) as wrong_plan:
        provider.invoke(
            wrong_plan_request,
            control=_control(),
            credential_reference=None,
        )
    assert wrong_plan.value.code == "identity_mismatch"
    assert wrong_plan.value.request_sent is False

    reference = CredentialReference(
        provider_name="offline-replay",
        api_key_env="OPENAI_API_KEY",
    )
    with pytest.raises(CompositeReplayInvocationError) as credential:
        provider.invoke(
            request,
            control=_control(),
            credential_reference=reference,
        )
    assert credential.value.code == "credential_not_allowed"
    assert credential.value.request_sent is False
    assert provider.reconciliation().consumed_count == 0

    secret_context = PreCallContextManifest(
        call_id="actor.offline.initial",
        actor_id="offline",
        task_envelope_digest=_digest("task"),
        reads=(
            ActualContextRead(
                read_id="resolved-secret",
                source_kind="task",
                source_ref="issue",
                value={"api_key": "sk-abcdefghijklmnopqrstuv"},
                value_digest=_digest("resolved-secret"),
                provenance_ref=_digest("task"),
            ),
        ),
    )
    secret_request = ActorCallRequest(
        run_id="run.secret",
        compiled_semantic_digest=_digest("compiled"),
        call_id="actor.offline.initial",
        actor_id="offline",
        call_kind="initial",
        instruction="Do not accept a resolved credential.",
        allowed_tool_ids=("repo.read",),
        budget_share_bps=10_000,
        context=secret_context,
        input_token_estimate=10,
        max_output_tokens=20,
    )
    secret_manifest = CompositeReplayManifest(
        binding=_binding(),
        rows=(
            CompositeReplayRow(
                sequence_no=0,
                request_digest=secret_request.request_digest,
                turn=_turn("secret"),
                usage=_usage("secret"),
            ),
        ),
    )
    secret_provider = CompositeReplayProvider(secret_manifest, expected_binding=_binding())
    with pytest.raises(CompositeReplayInvocationError) as secret:
        secret_provider.invoke(
            secret_request,
            control=_control(),
            credential_reference=None,
        )
    assert secret.value.code == "non_public_request"
    assert secret.value.request_sent is False
    assert secret_provider.reconciliation().consumed_count == 0


def test_cancellation_and_deadline_leave_the_next_row_available() -> None:
    request = _request("control")
    provider = CompositeReplayProvider(_manifest(request), expected_binding=_binding())
    cancelled = threading.Event()
    cancelled.set()

    with pytest.raises(CompositeReplayInvocationError) as cancelled_error:
        provider.invoke(
            request,
            control=_control(cancellation_event=cancelled),
            credential_reference=None,
        )
    assert cancelled_error.value.cancelled is True
    assert cancelled_error.value.request_sent is False

    with pytest.raises(CompositeReplayInvocationError) as deadline_error:
        provider.invoke(
            request,
            control=_control(deadline_monotonic=time.monotonic() - 1.0),
            credential_reference=None,
        )
    assert deadline_error.value.deadline_exceeded is True
    assert deadline_error.value.request_sent is False
    assert provider.reconciliation().consumed_count == 0

    provider.invoke(request, control=_control(), credential_reference=None)
    assert provider.assert_reconciled().complete is True


def test_final_reconciliation_detects_extra_missing_and_reused_rows() -> None:
    first = _request("one")
    second = _request("two")
    third = _request("three")
    provider = CompositeReplayProvider(
        _manifest(first, second),
        expected_binding=_binding(),
    )

    provider.invoke(first, control=_control(), credential_reference=None)
    partial = provider.reconciliation()
    assert partial.complete is False
    assert partial.consumed_count == 1
    assert partial.remaining_request_digests == (second.request_digest,)
    with pytest.raises(CompositeReplayMismatchError) as extra:
        provider.assert_reconciled()
    assert extra.value.code == "extra_rows"

    provider.invoke(second, control=_control(), credential_reference=None)
    complete = provider.assert_reconciled()
    assert complete.complete is True
    assert complete.consumed_response_ids == ("response.0", "response.1")
    assert complete.reconciliation_digest == provider.reconciliation().reconciliation_digest

    with pytest.raises(CompositeReplayInvocationError) as reused:
        provider.invoke(second, control=_control(), credential_reference=None)
    assert reused.value.code == "row_reuse"
    with pytest.raises(CompositeReplayInvocationError) as missing:
        provider.invoke(third, control=_control(), credential_reference=None)
    assert missing.value.code == "missing_row"


def test_manifest_io_is_atomic_immutable_digest_checked_and_public_safe(tmp_path: Path) -> None:
    request = _request("io")
    manifest = _manifest(request)
    path = tmp_path / "replay" / "manifest.json"

    assert write_composite_replay_manifest(path, manifest) == path
    assert write_composite_replay_manifest(path, manifest) == path
    loaded = load_composite_replay_manifest(path)
    assert loaded == manifest
    assert not list(path.parent.glob(".*.tmp"))

    other = _manifest(_request("different"))
    with pytest.raises(FileExistsError, match="immutable"):
        write_composite_replay_manifest(path, other)

    tampered_path = tmp_path / "tampered.json"
    payload = manifest.model_dump(mode="json")
    payload["rows"][0]["usage"]["output_tokens"] += 1
    tampered_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValidationError, match="row digest mismatch"):
        load_composite_replay_manifest(tampered_path)

    canary = "SEALED-CANARY-MUST-NOT-CROSS"
    canary_manifest = CompositeReplayManifest(
        binding=_binding(),
        rows=(
            CompositeReplayRow(
                sequence_no=0,
                request_digest=request.request_digest,
                turn=ActorTerminalTurn(
                    output=ActorCallOutput(output_text=f"public text {canary}")
                ),
                usage=_usage("canary"),
            ),
        ),
    )
    with pytest.raises(ValueError, match="canary"):
        write_composite_replay_manifest(
            tmp_path / "canary.json",
            canary_manifest,
            forbidden_markers=(canary,),
        )

    with pytest.raises(ValueError, match="resolved credential"):
        CompositeReplayRow(
            sequence_no=0,
            request_digest=request.request_digest,
            turn=ActorTerminalTurn(
                output=ActorCallOutput(output_text="Bearer abcdefghijklmnopqrstuvwxyz")
            ),
            usage=_usage("credential"),
        )
    with pytest.raises(ValueError, match="exact known or estimated token usage"):
        CompositeReplayRow(
            sequence_no=0,
            request_digest=request.request_digest,
            turn=_turn("unknown-usage"),
            usage=ProviderUsageReport.unknown(),
        )
    with pytest.raises(ValueError, match="forbidden"):
        CompositeReplayRow(
            sequence_no=0,
            request_digest=request.request_digest,
            turn=ActorToolRequest(
                request_id="sealed",
                tool_id="repo.read",
                arguments={"hidden_tests": "must-not-cross"},
            ),
            usage=_usage("sealed"),
        )


def _write_source(root: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir(parents=True)
    (root / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "tests" / "test_public.py").write_text(
        "def test_value():\n    assert True\n",
        encoding="utf-8",
    )


def _task(source: Path) -> TaskEnvelope:
    return TaskEnvelope(
        task_manifest_id="composite-replay-task",
        epoch_id="repo-repair-development",
        epoch_manifest_digest=_digest("runtime-epoch"),
        data_state="development",
        split_manifest_digest=_digest("runtime-split"),
        issue="Change VALUE from 1 to 2 and preserve the public test.",
        workspace_snapshot=WorkspaceSnapshotRef(
            snapshot_id="replay-source",
            uri=str(source),
            digest=repository_snapshot_digest(source),
        ),
        public_reproduction=(
            PublicReproductionStep(
                step_id="public-test",
                argv=("python", "-m", "pytest", "-q"),
                timeout_ms=2_000,
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
                policy_digest=_digest(f"policy:{tool_id}"),
            )
            for tool_id in sorted(task.allowed_capabilities)
        ),
    )


class _IsolationBackend:
    def run(self, request: IsolatedCommandRequest) -> IsolatedCommandResult:
        assert (request.workspace / "src" / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"
        stdout = "public replay verification passed\n"
        return IsolatedCommandResult(
            status=IsolatedCommandStatus.COMPLETED,
            command=request.command,
            container_name="offline-replay-isolation",
            exit_code=0,
            stdout=stdout,
            stderr="",
            stdout_digest=hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
            stderr_digest=hashlib.sha256(b"").hexdigest(),
            duration_s=0.01,
            output_truncated=False,
            failure_detail=None,
        )


class _RecordingToolLoopProvider:
    def __init__(self, recorder: CompositeReplayRecorder) -> None:
        self.recorder = recorder
        self.requests: list[ActorCallRequest] = []

    def invoke(
        self,
        request: Any,
        *,
        control: ProviderCallControl,
        credential_reference: CredentialReference | None,
    ) -> ProviderInvocation:
        assert control.cancelled is False
        assert credential_reference is None
        normalized = ActorCallRequest.model_validate(request)
        self.requests.append(normalized)
        if normalized.call_id == "actor.investigator.initial":
            if normalized.turn_index == 0:
                turn: ActorToolRequest | ActorTerminalTurn = ActorToolRequest(
                    request_id="investigator.search",
                    tool_id="repo.search",
                    arguments={"query": "VALUE", "path": "src"},
                )
            else:
                turn = ActorTerminalTurn(
                    output=ActorCallOutput(
                        output_text="Located the public failure in src/app.py.",
                        artifact_payloads={
                            "artifact.investigation": "Change src/app.py from VALUE = 1 to VALUE = 2."
                        },
                    )
                )
        elif normalized.turn_index == 0:
            turn = ActorToolRequest(
                request_id="implementer.edit",
                tool_id="repo.edit",
                arguments={"path": "src/app.py", "content": "VALUE = 2\n"},
            )
        elif normalized.turn_index == 1:
            turn = ActorToolRequest(
                request_id="implementer.diff",
                tool_id="repo.diff",
                arguments={},
            )
        else:
            turn = ActorTerminalTurn(
                output=ActorCallOutput(
                    output_text="The public repair is complete.",
                    final_patch=str(normalized.tool_results[-1].output["patch"]),
                )
            )
        usage = ProviderUsageReport(
            usage_status=UsageStatus.KNOWN,
            input_tokens=normalized.input_token_estimate,
            output_tokens=count_tokens_rough(
                json.dumps(turn.model_dump(mode="json"), sort_keys=True)
            ),
            cached_tokens=0,
            cost_status=CostStatus.KNOWN,
            cost_usd=0.0,
            response_id=f"recorded.{normalized.call_id}.{normalized.turn_index}",
        )
        invocation = ProviderInvocation(response=turn, usage=usage)
        self.recorder.record_invocation(request=normalized, invocation=invocation)
        return invocation


def _tool_service(task: TaskEnvelope, workspace: TaskWorkspace) -> TrustedRepairToolService:
    return TrustedRepairToolService(task, workspace, _IsolationBackend())


def _runtime(
    *,
    task: TaskEnvelope,
    plan,
    workspace: TaskWorkspace,
    provider,
) -> CompositeRuntime:
    return CompositeRuntime(
        plan,
        task,
        ScratchWorkspaceBinding(
            workspace_id="scratch.replay",
            workspace_digest=task.workspace_snapshot.digest,
        ),
        provider,
        run_id="run.composite.replay",
        tool_interface=_tool_service(task, workspace),
    )


def test_real_composite_runtime_tool_loop_replays_reproducibly_with_exact_accounting(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _write_source(source)
    task = _task(source)
    plan = compile_composite_run_plan(
        task,
        load_canonical_harness_seed().protocol,
        _dependencies(task),
    )
    binding = CompositeReplayBinding.from_runtime_inputs(
        release_digest=_digest("runtime-release"),
        task=task,
        deployment=_deployment(),
        plan=plan,
    )
    recorder = CompositeReplayRecorder(binding)
    recording_provider = _RecordingToolLoopProvider(recorder)
    first_workspace = materialize_task_workspace(task.workspace_snapshot, tmp_path / "first")

    recorded_result = _runtime(
        task=task,
        plan=plan,
        workspace=first_workspace,
        provider=recording_provider,
    ).run()
    manifest = recorder.finalize()
    manifest_path = tmp_path / "replay_manifest.json"
    write_composite_replay_manifest(manifest_path, manifest)
    loaded = load_composite_replay_manifest(manifest_path)

    replay_provider = CompositeReplayProvider(loaded, expected_binding=binding)
    second_workspace = materialize_task_workspace(task.workspace_snapshot, tmp_path / "second")
    replayed_result = _runtime(
        task=task,
        plan=plan,
        workspace=second_workspace,
        provider=replay_provider,
    ).run()

    assert recorded_result.status == replayed_result.status == "completed"
    assert recorded_result.final_patch == replayed_result.final_patch
    assert recorded_result.final_patch_digest == replayed_result.final_patch_digest
    assert recorded_result.final_workspace_digest == replayed_result.final_workspace_digest
    def semantic_actor_calls(result):
        payload = [call.model_dump(mode="json") for call in result.actor_calls]
        for call in payload:
            for round_ in call["provider_rounds"]:
                round_.pop("started_at_ms")
                round_.pop("finished_at_ms")
        return payload

    assert semantic_actor_calls(recorded_result) == semantic_actor_calls(replayed_result)
    for result in (recorded_result, replayed_result):
        for call in result.actor_calls:
            for round_ in call.provider_rounds:
                assert round_.started_at_ms >= 0
                assert round_.finished_at_ms >= round_.started_at_ms
    assert recorded_result.context_manifests == replayed_result.context_manifests
    assert recorded_result.artifacts == replayed_result.artifacts
    assert [receipt.output_digest for receipt in recorded_result.tool_receipts] == [
        receipt.output_digest for receipt in replayed_result.tool_receipts
    ]
    assert [request.request_digest for request in recording_provider.requests] == [
        row.request_digest for row in loaded.rows
    ]

    expected_input = sum(row.usage.input_tokens or 0 for row in loaded.rows)
    expected_output = sum(row.usage.output_tokens or 0 for row in loaded.rows)
    assert replayed_result.budget.model_calls == len(loaded.rows) == 5
    assert replayed_result.budget.input_tokens == expected_input
    assert replayed_result.budget.output_tokens == expected_output
    assert replayed_result.budget.known_cost_usd == 0.0
    assert replayed_result.budget.unknown_usage_events == 0
    assert replayed_result.budget.unknown_cost_events == 0
    assert replayed_result.budget.reconciled is True
    reconciliation = replay_provider.assert_reconciled()
    assert reconciliation.complete is True
    assert reconciliation.consumed_response_ids == tuple(
        str(row.usage.response_id) for row in loaded.rows
    )


def test_replay_contract_loads_from_source_hidden_runtime_bundle(tmp_path: Path) -> None:
    from agintor.runtime.sdk.bundle import bundle_runtime_kernel

    runtime_dir = tmp_path / "runtime"
    manifest = bundle_runtime_kernel(runtime_dir, force=True)
    module_path = "agintor_runtime/runtime/kernel/composite_replay_provider.py"
    assert module_path in manifest.files
    bundle_root = runtime_dir / "runtime_sdk"
    replay_path = tmp_path / "bundled-replay.json"
    script = f"""
import sys
sys.path.insert(0, {str(bundle_root)!r})
from agintor_runtime.contracts.epochs import DeploymentIdentity
from agintor_runtime.runtime.kernel.composite_budget import CostStatus, ProviderUsageReport, UsageStatus
from agintor_runtime.contracts.harness import NO_PUBLIC_SESSION_CONTEXT_DIGEST
from agintor_runtime.runtime.kernel.composite_replay_provider import CompositeReplayBinding, CompositeReplayManifest, CompositeReplayRow, load_composite_replay_manifest, write_composite_replay_manifest
from agintor_runtime.runtime.kernel.composite_runtime import ActorCallOutput, ActorTerminalTurn

digest = "a" * 64
deployment = DeploymentIdentity(deployment_id="bundled", provider="offline", model="replay", provider_config_digest=digest, decoding_policy_digest=digest, price_schedule_digest=digest, command_container_policy_digest=digest)
binding = CompositeReplayBinding(release_digest=digest, epoch_id="epoch", epoch_manifest_digest=digest, deployment=deployment, source_protocol_digest=digest, task_envelope_digest=digest, compiled_semantic_digest=digest, public_session_context_digest=NO_PUBLIC_SESSION_CONTEXT_DIGEST)
usage = ProviderUsageReport(usage_status=UsageStatus.KNOWN, input_tokens=1, output_tokens=1, cached_tokens=0, cost_status=CostStatus.KNOWN, cost_usd=0.0, response_id="bundled.response")
row = CompositeReplayRow(sequence_no=0, request_digest=digest, turn=ActorTerminalTurn(output=ActorCallOutput(output_text="bundled offline replay")), usage=usage)
manifest = CompositeReplayManifest(binding=binding, rows=(row,))
write_composite_replay_manifest({str(replay_path)!r}, manifest)
assert load_composite_replay_manifest({str(replay_path)!r}) == manifest
print(manifest.manifest_digest)
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == load_composite_replay_manifest(replay_path).manifest_digest
