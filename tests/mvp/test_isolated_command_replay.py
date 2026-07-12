from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agintor.contracts.outcomes import PairKey
from agintor.core.identity import canonical_identity_digest
from agintor.factory.harness_release import publish_harness_release
from agintor.isolation.commands import (
    IsolatedCommandRequest,
    IsolatedCommandResult,
    IsolatedCommandStatus,
)
from agintor.isolation.replay import (
    IsolatedCommandReplayBackend,
    IsolatedCommandReplayBinding,
    IsolatedCommandReplayError,
    IsolatedCommandReplayManifest,
    IsolatedCommandReplayRecorder,
    IsolatedCommandReplayRequest,
    IsolatedCommandReplayRow,
    load_isolated_command_replay_manifest,
    write_isolated_command_replay_manifest,
)
from agintor.runtime.api.composite_compiler import compile_composite_run_plan
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
from agintor.runtime.sdk.harness_executor import (
    execute_harness_solve,
    load_controlled_run_evidence,
)

from tests.mvp import test_harness_sdk_execution as sdk_fixtures


def _digest(label: str) -> str:
    return canonical_identity_digest(label, domain="test-isolated-command-replay")


def _workspace(root: Path, value: str = "old") -> Path:
    workspace = root / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src/app.py").write_text(
        f'def value():\n    return "{value}"\n',
        encoding="utf-8",
        newline="\n",
    )
    return workspace


def _binding() -> IsolatedCommandReplayBinding:
    return IsolatedCommandReplayBinding(
        release_digest=_digest("release"),
        epoch_id="epoch.command-replay",
        epoch_manifest_digest=_digest("epoch"),
        task_envelope_digest=_digest("task"),
        workspace_snapshot_id="snapshot.command-replay",
        workspace_snapshot_digest=_digest("snapshot"),
        command_policy_digest=_digest("command-policy"),
    )


def _policy():
    return sdk_fixtures._deployment_profile().command_container_policy.to_isolated_command_policy()


def _request(workspace: Path, *command: str, timeout_s: float = 2.0) -> IsolatedCommandRequest:
    return IsolatedCommandRequest(
        command=command or ("python", "-m", "pytest", "-q"),
        workspace=workspace,
        working_directory=".",
        environment={"LANG": "C.UTF-8"},
        timeout_s=timeout_s,
    )


def _result(request: IsolatedCommandRequest, *, label: str = "passed") -> IsolatedCommandResult:
    stdout = f"{label}\n"
    stderr = ""
    return IsolatedCommandResult(
        status=IsolatedCommandStatus.COMPLETED,
        command=request.command,
        container_name=f"replay-{label}",
        exit_code=0,
        stdout=stdout,
        stderr=stderr,
        stdout_digest=hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
        stderr_digest=hashlib.sha256(stderr.encode("utf-8")).hexdigest(),
        duration_s=0.01,
    )


def _manifest(*pairs: tuple[IsolatedCommandRequest, IsolatedCommandResult]) -> IsolatedCommandReplayManifest:
    recorder = IsolatedCommandReplayRecorder(_binding())
    for request, result in pairs:
        recorder.record(request=request, result=result)
    return recorder.finalize()


def test_replay_matches_workspace_content_not_host_path_and_is_single_use(tmp_path: Path) -> None:
    first_workspace = _workspace(tmp_path / "first")
    second_workspace = _workspace(tmp_path / "second")
    recorded_request = _request(first_workspace, "python", "-m", "pytest", "-q")
    expected_result = _result(recorded_request)
    manifest = _manifest((recorded_request, expected_result))
    backend = IsolatedCommandReplayBackend(
        manifest,
        expected_binding=_binding(),
        policy=_policy(),
    )

    actual = backend.run(
        _request(second_workspace, "python", "-m", "pytest", "-q")
    )

    assert actual == expected_result
    assert str(first_workspace.resolve()) not in json.dumps(manifest.model_dump(mode="json"))
    assert str(second_workspace.resolve()) not in json.dumps(manifest.model_dump(mode="json"))
    assert backend.assert_reconciled().complete
    with pytest.raises(IsolatedCommandReplayError) as reused:
        backend.run(_request(second_workspace, "python", "-m", "pytest", "-q"))
    assert reused.value.code == "manifest_reused"


def test_request_capture_freezes_pre_dispatch_mutable_workspace_identity(
    tmp_path: Path,
) -> None:
    recorded_workspace = _workspace(tmp_path / "recorded")
    replay_workspace = _workspace(tmp_path / "replayed")
    request = _request(recorded_workspace, "git", "apply", "candidate.patch")
    recorder = IsolatedCommandReplayRecorder(_binding())

    captured = recorder.capture_request(request)
    (recorded_workspace / "src/app.py").write_text(
        'def value():\n    return "new"\n',
        encoding="utf-8",
        newline="\n",
    )
    assert captured.workspace_content_digest != (
        IsolatedCommandReplayRequest.from_request(request).workspace_content_digest
    )
    recorder.record(
        request=captured,
        result=_result(request, label="mutated"),
        workspace_after=recorded_workspace,
    )
    recorded_verification = _request(
        recorded_workspace,
        "python",
        "-c",
        "assert True",
    )
    recorder.record(
        request=recorder.capture_request(recorded_verification),
        result=_result(recorded_verification, label="verified"),
        workspace_after=recorded_workspace,
    )

    backend = IsolatedCommandReplayBackend(
        recorder.finalize(),
        expected_binding=_binding(),
        policy=_policy(),
    )
    assert backend.run(
        _request(replay_workspace, "git", "apply", "candidate.patch")
    ).succeeded
    assert backend.run(
        _request(replay_workspace, "python", "-c", "assert True")
    ).succeeded
    assert "old" in (replay_workspace / "src/app.py").read_text(encoding="utf-8")
    assert backend.assert_reconciled().complete


def test_order_missing_extra_cancellation_and_binding_fail_closed(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    first = _request(workspace, "python", "-m", "pytest", "-q")
    second = _request(workspace, "python", "-m", "compileall", "src")
    manifest = _manifest((first, _result(first, label="first")), (second, _result(second, label="second")))
    backend = IsolatedCommandReplayBackend(
        manifest,
        expected_binding=_binding(),
        policy=_policy(),
    )

    with pytest.raises(IsolatedCommandReplayError) as out_of_order:
        backend.run(second)
    assert out_of_order.value.code == "request_mismatch"
    backend.run(first)
    with pytest.raises(IsolatedCommandReplayError) as extra:
        backend.assert_reconciled()
    assert extra.value.code == "extra_rows"
    backend.run(second)
    assert backend.assert_reconciled().complete

    cancelled = IsolatedCommandReplayBackend(
        manifest,
        expected_binding=_binding(),
        policy=_policy(),
    )
    cancelled.cancel()
    with pytest.raises(IsolatedCommandReplayError) as cancelled_error:
        cancelled.run(first)
    assert cancelled_error.value.code == "cancelled"

    crossed = _binding().model_copy(update={"command_policy_digest": _digest("crossed-policy")})
    with pytest.raises(IsolatedCommandReplayError) as identity_error:
        IsolatedCommandReplayBackend(
            manifest,
            expected_binding=crossed,
            policy=_policy(),
        )
    assert identity_error.value.code == "identity_mismatch"


def test_manifest_rejects_shell_secret_and_incoherent_deadline_rows(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    with pytest.raises(ValueError, match="shell"):
        IsolatedCommandReplayRequest(
            command=("sh", "-c", "pytest -q"),
            workspace_content_digest=_digest("workspace"),
            working_directory=".",
            environment={},
            timeout_s=1.0,
        )
    with pytest.raises(ValueError):
        IsolatedCommandReplayRequest(
            command=("python", "-m", "pytest"),
            workspace_content_digest=_digest("workspace"),
            working_directory=".",
            environment={"SAFE_NAME": "sk-abcdefghijklmnopqrstuvwxyz"},
            timeout_s=1.0,
        )
    request = _request(workspace, "python", "-m", "pytest", timeout_s=1.0)
    late = _result(request).model_copy(update={"duration_s": 2.0})
    with pytest.raises(ValueError, match="deadline"):
        IsolatedCommandReplayRow(
            sequence_no=0,
            request=IsolatedCommandReplayRequest.from_request(request),
            workspace_content_digest_after=(
                IsolatedCommandReplayRequest.from_request(
                    request
                ).workspace_content_digest
            ),
            result=late,
        )


def test_rows_require_post_state_and_manifests_reject_broken_transition_chain(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    first_request = _request(workspace, "python", "-c", "assert True")
    second_request = _request(workspace, "python", "-c", "assert 1")
    first_replay = IsolatedCommandReplayRequest.from_request(first_request)
    second_replay = IsolatedCommandReplayRequest.from_request(second_request)
    with pytest.raises(ValueError, match="workspace_content_digest_after"):
        IsolatedCommandReplayRow(
            sequence_no=0,
            request=first_replay,
            result=_result(first_request),
        )
    first_row = IsolatedCommandReplayRow(
        sequence_no=0,
        request=first_replay,
        workspace_content_digest_after=_digest("broken-transition"),
        result=_result(first_request, label="first"),
    )
    second_row = IsolatedCommandReplayRow(
        sequence_no=1,
        request=second_replay,
        workspace_content_digest_after=second_replay.workspace_content_digest,
        result=_result(second_request, label="second"),
    )
    with pytest.raises(ValueError, match="workspace transition"):
        IsolatedCommandReplayManifest(
            binding=_binding(),
            rows=(first_row, second_row),
        )


def test_manifest_io_is_atomic_immutable_and_detects_tampering(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    request = _request(workspace, "python", "-m", "pytest", "-q")
    manifest = _manifest((request, _result(request)))
    path = tmp_path / "command-replay.json"

    assert write_isolated_command_replay_manifest(path, manifest) == path
    assert write_isolated_command_replay_manifest(path, manifest) == path
    assert load_isolated_command_replay_manifest(path) == manifest
    crossed = IsolatedCommandReplayManifest(
        binding=manifest.binding.model_copy(update={"release_digest": _digest("other")}),
        rows=manifest.rows,
    )
    with pytest.raises(FileExistsError):
        write_isolated_command_replay_manifest(path, crossed)

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["rows"][0]["result"]["stdout"] = "tampered\n"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        load_isolated_command_replay_manifest(path)


class _RecordingProvider:
    execution_provenance = ProviderExecutionProvenance(
        execution_mode="deterministic_replay",
        live_inference_status="not_run",
        real_inference_requests_sent=0,
    )

    def __init__(self, provider: Any, recorder: CompositeReplayRecorder) -> None:
        self._provider = provider
        self._recorder = recorder
        self.deployment_identity = provider.deployment_identity

    def invoke(
        self,
        request: Any,
        *,
        control: ProviderCallControl,
        credential_reference: CredentialReference | None,
    ) -> ProviderInvocation:
        invocation = self._provider.invoke(
            request,
            control=control,
            credential_reference=credential_reference,
        )
        self._recorder.record_invocation(request=request, invocation=invocation)
        return invocation


class _RecordingCommandBackend:
    def __init__(
        self,
        backend: Any,
        recorder: IsolatedCommandReplayRecorder,
    ) -> None:
        self._backend = backend
        self._recorder = recorder
        self.policy = backend.policy

    def run(self, request: IsolatedCommandRequest) -> IsolatedCommandResult:
        captured = self._recorder.capture_request(request)
        result = self._backend.run(request)
        self._recorder.record(
            request=captured,
            result=result,
            workspace_after=request.workspace,
        )
        return result


def test_source_hidden_runtime_entry_solves_with_dual_replay_and_no_docker(
    tmp_path: Path,
) -> None:
    epoch = sdk_fixtures._epoch()
    project_root = tmp_path / "factory"
    release, _ = publish_harness_release(
        project_root=project_root,
        request=sdk_fixtures._release_request(epoch),
    )
    source = sdk_fixtures._source_repository(tmp_path / "dual-replay-task")
    task = sdk_fixtures._task(epoch, source)
    plan = compile_composite_run_plan(
        task,
        sdk_fixtures.load_canonical_harness_seed().protocol,
        sdk_fixtures._dependencies(),
    )
    provider_binding = CompositeReplayBinding.from_runtime_inputs(
        release_digest=release.manifest.release_digest,
        task=task,
        deployment=epoch.deployment,
        plan=plan,
    )
    provider_recorder = CompositeReplayRecorder(provider_binding)
    command_binding = IsolatedCommandReplayBinding.from_runtime_inputs(
        release_digest=release.manifest.release_digest,
        task=task,
        command_policy_digest=epoch.deployment.command_container_policy_digest,
    )
    command_recorder = IsolatedCommandReplayRecorder(command_binding)
    recording_provider = _RecordingProvider(
        sdk_fixtures.ScriptedRepairProvider(epoch.deployment),
        provider_recorder,
    )
    recording_backend = _RecordingCommandBackend(
        sdk_fixtures.PassingCommandBackend(),
        command_recorder,
    )
    run_id = "run.source-hidden-dual-replay"
    workspace_id = "workspace.source-hidden-dual-replay"
    pair_key = PairKey(
        task_manifest_id=task.task_manifest_id,
        environment_id="environment.source-hidden-replay",
        sampling_replicate=0,
        provider_config_digest=epoch.deployment.provider_config_digest,
    )

    recorded_result = execute_harness_solve(
        project_root,
        task,
        provider=recording_provider,
        command_backend=recording_backend,
        run_artifact_workspace=tmp_path / "recording-run",
        run_id=run_id,
        workspace_id=workspace_id,
    )
    assert recorded_result.status == "completed"
    provider_manifest_path = tmp_path / "provider-replay.json"
    command_manifest_path = tmp_path / "command-replay.json"
    write_composite_replay_manifest(provider_manifest_path, provider_recorder.finalize())
    write_isolated_command_replay_manifest(command_manifest_path, command_recorder.finalize())

    replay_workspace = tmp_path / "source-hidden-run"
    request_path = tmp_path / "solve-request.json"
    output_path = tmp_path / "solve-output.json"
    request_path.write_text(
        json.dumps(
            {
                "schema_version": "repo-repair-harness-solve-request-v1",
                "task": task.model_dump(mode="json"),
                "execution": {
                    "mode": "replay",
                    "provider_manifest_path": str(provider_manifest_path),
                    "command_manifest_path": str(command_manifest_path),
                },
                "run_artifact_workspace": str(replay_workspace),
                    "run_id": run_id,
                    "workspace_id": workspace_id,
                    "pair_key": pair_key.model_dump(mode="json"),
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    bundle_root = (
        project_root
        / "releases"
        / release.manifest.release_digest
        / "runtime/runtime_sdk"
    )
    script = (
        "import sys; "
        f"sys.path.insert(0, {str(bundle_root)!r}); "
        "from agintor_runtime.runtime.sdk.harness_entrypoint import main; "
        "raise SystemExit(main(["
        "'solve', "
        f"'--project-root', {str(project_root)!r}, "
        f"'--request-json', {str(request_path)!r}, "
        f"'--output-json', {str(output_path)!r}]))"
    )

    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["status"] == "completed"
    assert payload["execution_mode"] == "deterministic_replay"
    assert payload["live_inference_status"] == "not_run"
    assert payload["real_inference_requests_sent"] == 0
    assert payload["capability_promotion_authorized"] is False
    assert payload["eligible_for_evaluator_submission"] is True
    assert payload["controlled_run_evidence"] is not None
    evidence = load_controlled_run_evidence(
        replay_workspace,
        payload["controlled_run_evidence"],
    )
    assert evidence.pair_key == pair_key
    assert evidence.execution_mode == "deterministic_replay"
    assert evidence.real_inference_requests_sent == 0
    assert payload["budget"]["model_calls"] == 9
    assert payload["budget"]["reconciled"] is True
    assert {item["tool_id"] for item in payload["evidence"]["tool_receipts"]} == set(
        sdk_fixtures.REPO_REPAIR_TRUSTED_TOOL_IDS
    )
    assert '-    return "old"' in payload["submitted_patch"]["unified_diff"]
    assert '+    return "new"' in payload["submitted_patch"]["unified_diff"]
    assert (source / "src/app.py").read_text(encoding="utf-8") == (
        'def value():\n    return "old"\n'
    )
    assert (replay_workspace / "repository/working/src/app.py").read_text(encoding="utf-8") == (
        'def value():\n    return "new"\n'
    )
