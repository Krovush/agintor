from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import venv
from pathlib import Path
from typing import Any

from agintor.contracts.epochs import (
    PublicReproductionStep,
    TaskEnvelope,
    WorkspaceSnapshotRef,
)
from agintor.contracts.outcomes import PairKey
from agintor.core.identity import canonical_identity_digest
from agintor.evaluation.contracts import (
    EvaluationContract,
    HiddenCheck,
    SealedCanary,
    SealedFixtureRef,
)
from agintor.evaluation.harness_entrypoint import (
    HARNESS_EVALUATION_ENTRY_REQUEST_SCHEMA_VERSION,
)
from agintor.evaluation.pilot import (
    audit_public_development_tasks,
    reserve_audited_pilot_task,
)
from agintor.evaluation.runners.repo_patch_backends import (
    IsolatedRepoPatchCommandBackend,
)
from agintor.evaluation.runners.repo_patch_runner import (
    RepoPatchEvaluatorRunner,
    RepoPatchFixture,
    repo_patch_fixture_digest,
)
from agintor.factory.harness_replay import (
    HarnessFactoryReplayRecorder,
    write_harness_factory_replay_manifest,
)
from agintor.factory.harness_service import build_harness_factory_release
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
from agintor.repositories.workspaces import repository_snapshot_digest
from agintor.runtime.api.composite_compiler import compile_composite_run_plan
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
from agintor.runtime.sdk.harness_executor import (
    execute_harness_solve,
    load_controlled_run_evidence,
)
from agintor.runtime.sdk.harness_release_loader import load_active_harness_release
from agintor.storage.harness_session_store import HarnessSessionStore
from agintor.storage.proof_records import ImmutableProofRecordStore

from tests.mvp.test_harness_factory_replay import _multitask_evaluator
from tests.mvp.test_harness_factory_service import _build_input, _gain_proposals
from tests.mvp.test_p1_pilot_evidence import (
    _artifact_raw as _readiness_artifact_raw,
    _core as _readiness_core,
    _full_evaluation_contract as _readiness_evaluation_contract,
    _packet_fixture as _readiness_packet_fixture,
)


def _write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def _run(
    argv: list[str],
    *,
    cwd: Path,
    environment: dict[str, str] | None = None,
    timeout: float = 180.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
        timeout=timeout,
    )


def _install_built_wheel(tmp_path: Path) -> tuple[Path, Path]:
    repo_root = Path(__file__).resolve().parents[2]
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    build_environment = dict(os.environ)
    build_environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    build_environment["PIP_NO_INDEX"] = "1"
    built = _run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            ".",
            "--no-deps",
            "--no-build-isolation",
            "-w",
            str(wheel_dir),
        ],
        cwd=repo_root,
        environment=build_environment,
        timeout=180.0,
    )
    assert built.returncode == 0, built.stderr
    wheel = next(wheel_dir.glob("agintor-*.whl"))

    environment_root = tmp_path / "wheel-environment"
    venv.EnvBuilder(with_pip=True, system_site_packages=True).create(environment_root)
    scripts = environment_root / ("Scripts" if os.name == "nt" else "bin")
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    cli = scripts / ("agintor.exe" if os.name == "nt" else "agintor")
    installed = _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-deps",
            str(wheel),
        ],
        cwd=tmp_path,
        environment=build_environment,
        timeout=180.0,
    )
    assert installed.returncode == 0, installed.stderr
    assert cli.is_file()
    imported = _run(
        [
            str(python),
            "-I",
            "-c",
            "import agintor; print(agintor.__file__)",
        ],
        cwd=tmp_path,
        environment=build_environment,
    )
    assert imported.returncode == 0, imported.stderr
    assert environment_root.resolve() in Path(imported.stdout.strip()).resolve().parents
    return cli, python


def _cli_environment(tmp_path: Path) -> tuple[dict[str, str], Path]:
    environment = dict(os.environ)
    environment.pop("AGINTOR_PROCESS_ROLE", None)
    for name in tuple(environment):
        if any(marker in name.upper() for marker in ("API_KEY", "TOKEN", "SECRET")):
            environment.pop(name, None)
    environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    environment["PIP_NO_INDEX"] = "1"
    sentinel_root = tmp_path / "forbidden-executables"
    sentinel_root.mkdir()
    marker = sentinel_root / "docker-was-invoked"
    if os.name == "nt":
        (sentinel_root / "docker.cmd").write_text(
            f"@echo off\r\ntype nul > \"{marker}\"\r\nexit /b 97\r\n",
            encoding="utf-8",
        )
    else:
        executable = sentinel_root / "docker"
        executable.write_text(
            f"#!/bin/sh\n: > {str(marker)!r}\nexit 97\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
    environment["PATH"] = str(sentinel_root) + os.pathsep + environment.get("PATH", "")
    return environment, marker


def _author_wheel_fixture(
    wheel_python: Path,
    environment: dict[str, str],
    tmp_path: Path,
    *arguments: str,
) -> dict[str, Any]:
    author = Path(__file__).with_name("_p1_wheel_fixture_author.py").resolve()
    environment_root = wheel_python.resolve().parents[1]
    completed = _run(
        [
            str(wheel_python),
            "-I",
            str(author),
            "--expected-package-root",
            str(environment_root),
            *arguments,
        ],
        cwd=tmp_path,
        environment=environment,
        timeout=180.0,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload = json.loads(completed.stdout)
    assert environment_root in Path(payload["imported_from"]).resolve().parents
    return payload


class ExecutingHostCommandBackend:
    """Test-only recorder that executes every requested argv without a shell."""

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
                raise RuntimeError("git is required for the evaluator replay recording")
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
            for name in (
                "SYSTEMROOT",
                "WINDIR",
                "PATH",
                "PATHEXT",
                "TEMP",
                "TMP",
            )
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


class RecordingCommandBackend:
    def __init__(
        self,
        delegate: ExecutingHostCommandBackend,
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


class PlanAwareRepairProvider:
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
        assert credential_reference is None
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
                                "src/app.py contains VALUE = 1 and the public check requires "
                                "a VALUE = 2 prefix."
                            )
                            for write in planned.artifact_writes
                        },
                    )
                )
        else:
            actions = (
                (
                    "repo.edit",
                    {"path": "src/app.py", "content": self.replacement},
                ),
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
                patch = normalized.tool_results[-1].output["patch"]
                response = ActorTerminalTurn(
                    output=ActorCallOutput(
                        output_text="Submitted the repository-derived candidate patch.",
                        artifact_payloads={
                            write.artifact_id: "Implementation and verification evidence complete."
                            for write in planned.artifact_writes
                        },
                        final_patch=patch,
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


class RecordingProvider:
    execution_provenance = PlanAwareRepairProvider.execution_provenance

    def __init__(self, delegate: PlanAwareRepairProvider, recorder: CompositeReplayRecorder) -> None:
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


def _raw_repository(root: Path) -> Path:
    source = root / "raw-repository"
    (source / "src").mkdir(parents=True)
    (source / "tests").mkdir()
    (source / "src/app.py").write_text(
        "VALUE = 1\n", encoding="utf-8", newline="\n"
    )
    (source / "tests/sentinel.txt").write_text(
        "immutable\n", encoding="utf-8", newline="\n"
    )
    return source


def _pilot_task(epoch: Any, source: Path) -> TaskEnvelope:
    public_check = (
        "from pathlib import Path; "
        "text=Path('src/app.py').read_text(encoding='utf-8'); "
        "assert text.startswith('VALUE = 2'), text"
    )
    return TaskEnvelope(
        task_manifest_id="task.p1.heldout-pilot",
        epoch_id=epoch.epoch_id,
        epoch_manifest_digest=epoch.epoch_manifest_digest,
        data_state="development",
        split_manifest_digest=epoch.development_split_digest,
        issue="Repair the public VALUE regression using only repository evidence.",
        workspace_snapshot=WorkspaceSnapshotRef(
            snapshot_id="snapshot.p1.heldout-pilot",
            uri=str(source),
            digest=repository_snapshot_digest(source),
        ),
        public_reproduction=(
            PublicReproductionStep(
                step_id="public-value-prefix",
                argv=("python", "-c", public_check),
                timeout_ms=20_000,
            ),
        ),
        ceilings=epoch.per_run_ceilings,
    )


def _pair_key(task: TaskEnvelope, epoch: Any, replicate: int) -> PairKey:
    return PairKey(
        task_manifest_id=task.task_manifest_id,
        environment_id="environment.p1.offline-replay",
        sampling_replicate=replicate,
        provider_config_digest=epoch.deployment.provider_config_digest,
    )


def _record_solve_replay(
    *,
    project: Path,
    task: TaskEnvelope,
    pair_key: PairKey,
    run_id: str,
    workspace_id: str,
    replacement: str,
    destination: Path,
    session_context: Any = None,
) -> tuple[Path, Path, str]:
    release = load_active_harness_release(project)
    plan = compile_composite_run_plan(task, release.protocol, release.dependencies)
    provider_binding = CompositeReplayBinding.from_runtime_inputs(
        release_digest=release.manifest.release_digest,
        task=task,
        deployment=release.manifest.deployment,
        plan=plan,
        public_session_context=session_context,
    )
    provider_recorder = CompositeReplayRecorder(provider_binding)
    command_recorder = IsolatedCommandReplayRecorder(
        IsolatedCommandReplayBinding.from_runtime_inputs(
            release_digest=release.manifest.release_digest,
            task=task,
            command_policy_digest=(
                release.manifest.deployment.command_container_policy_digest
            ),
        )
    )
    host = ExecutingHostCommandBackend()
    from agintor.runtime.harness_profile import HarnessDeploymentProfile

    deployment_profile = HarnessDeploymentProfile.model_validate(
        release.profile.profile
    )
    isolated_policy = (
        deployment_profile.command_container_policy.to_isolated_command_policy()
    )
    provider = RecordingProvider(
        PlanAwareRepairProvider(
            plan,
            release.manifest.deployment,
            replacement=replacement,
        ),
        provider_recorder,
    )
    backend = RecordingCommandBackend(
        host,
        command_recorder,
        policy=isolated_policy,
    )
    recorded = execute_harness_solve(
        project,
        task,
        provider=provider,
        command_backend=backend,
        run_artifact_workspace=destination / "recording-run",
        run_id=run_id,
        workspace_id=workspace_id,
        pair_key=pair_key,
        public_session_context=session_context,
    )
    assert recorded.status == "completed"
    assert recorded.submitted_patch is not None
    assert host.requests
    assert any(request.command == task.public_reproduction[0].argv for request in host.requests)
    provider_path = write_composite_replay_manifest(
        destination / "provider-replay.json",
        provider_recorder.finalize(),
    )
    command_path = write_isolated_command_replay_manifest(
        destination / "command-replay.json",
        command_recorder.finalize(),
    )
    return provider_path, command_path, recorded.submitted_patch.unified_diff


def _run_cli_solve(
    *,
    cli: Path,
    environment: dict[str, str],
    project: Path,
    task_path: Path,
    pair_path: Path,
    provider_manifest: Path,
    command_manifest: Path,
    workspace: Path,
    run_id: str,
    workspace_id: str,
    session_id: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    argv = [
        str(cli),
        "solve",
        str(project),
        "--task-envelope",
        str(task_path),
        "--pair-key",
        str(pair_path),
        "--replay-provider-manifest",
        str(provider_manifest),
        "--replay-command-manifest",
        str(command_manifest),
        "--workspace",
        str(workspace),
        "--run-id",
        run_id,
        "--workspace-id",
        workspace_id,
        "--session" if session_id is not None else "--new-session",
    ]
    if session_id is not None:
        argv.append(session_id)
    completed = _run(
        argv,
        cwd=project.parent,
        environment=environment,
        timeout=180.0,
    )
    payload = json.loads(completed.stdout)
    return completed, payload


def _evaluation_contract(
    *,
    task: TaskEnvelope,
    epoch: Any,
    profile: Any,
) -> EvaluationContract:
    environment_identity = profile.command_container_policy.model_dump(mode="json")
    adapter = IsolatedRepoPatchCommandBackend(
        ExecutingHostCommandBackend(),
        environment_identity=environment_identity,
    )
    hidden_argv = (
        "python",
        "-c",
        (
            "from pathlib import Path; "
            "text=Path('src/app.py').read_text(encoding='utf-8'); "
            "assert text.strip() == 'VALUE = 2', text"
        ),
    )
    draft = EvaluationContract(
        evaluation_contract_id="evaluation.p1.heldout",
        epoch_id=epoch.epoch_id,
        epoch_manifest_digest=epoch.epoch_manifest_digest,
        data_state="development",
        split_manifest_digest=task.split_manifest_digest,
        task_manifest_id=task.task_manifest_id,
        task_manifest_digest=task.task_manifest_digest,
        sealed_fixture=SealedFixtureRef(
            fixture_id="fixture.p1.heldout",
            uri=task.workspace_snapshot.uri,
            fixture_digest="0" * 64,
            public_snapshot_digest=task.workspace_snapshot.digest,
        ),
        protected_paths=("tests",),
        hidden_checks=(
            HiddenCheck(
                check_id="hidden-exact-value",
                argv=hidden_argv,
                timeout_ms=10_000,
            ),
        ),
        outcome_authority=epoch.evaluator_authority,
        canaries=(
            SealedCanary(
                canary_id="canary.p1.heldout",
                value="P1-SEALED-CANARY-DO-NOT-PUBLISH",
            ),
        ),
    )
    fixture = RepoPatchFixture.from_evaluation_contract(
        draft,
        public_test_commands=task.public_reproduction,
    )
    payload = draft.model_dump(mode="python")
    payload.pop("evaluation_contract_digest", None)
    payload["sealed_fixture"]["fixture_digest"] = repo_patch_fixture_digest(
        fixture,
        adapter,
    )
    return EvaluationContract.model_validate(payload)


def _record_evaluator_replay(
    *,
    project: Path,
    task: TaskEnvelope,
    contract: EvaluationContract,
    patch: str,
    expected_complete_repair: bool,
    destination: Path,
) -> Path:
    release = load_active_harness_release(project)
    profile = release.profile.profile
    from agintor.runtime.harness_profile import HarnessDeploymentProfile

    deployment_profile = HarnessDeploymentProfile.model_validate(profile)
    recorder = IsolatedCommandReplayRecorder(
        IsolatedCommandReplayBinding.from_runtime_inputs(
            release_digest=release.manifest.release_digest,
            task=task,
            command_policy_digest=(
                release.manifest.deployment.command_container_policy_digest
            ),
        )
    )
    host = ExecutingHostCommandBackend()
    recording = RecordingCommandBackend(
        host,
        recorder,
        policy=deployment_profile.command_container_policy.to_isolated_command_policy(),
    )
    adapter = IsolatedRepoPatchCommandBackend(
        recording,
        environment_identity=(
            deployment_profile.command_container_policy.model_dump(mode="json")
        ),
    )
    fixture = RepoPatchFixture.from_evaluation_contract(
        contract,
        public_test_commands=task.public_reproduction,
    )
    result = RepoPatchEvaluatorRunner(adapter).run(
        candidate_artifact=patch,
        fixture=fixture,
    )
    assert result.complete_repair is expected_complete_repair, result.model_dump_json(
        indent=2
    )
    assert len(host.requests) == 3
    return write_isolated_command_replay_manifest(
        destination / "evaluator-command-replay.json",
        recorder.finalize(),
    )


def _generation_bytes(project: Path, release_digest: str) -> dict[str, bytes]:
    root = project / "releases" / release_digest
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_built_wheel_factory_repair_evaluator_and_release_followup_end_to_end(
    tmp_path: Path,
) -> None:
    source_project = tmp_path / "factory-replay-source"
    initial_input, _, dependencies = _build_input(
        source_project,
        task_ids=("task.search.1", "task.search.2"),
    )
    raw_repository = _raw_repository(tmp_path / "pilot")
    pilot_task = _pilot_task(initial_input.epoch, raw_repository)
    initial_input, _, dependencies = _build_input(
        source_project,
        task_ids=("task.search.1", "task.search.2"),
        pilot_task_digest=pilot_task.task_manifest_digest,
    )
    audit = audit_public_development_tasks(
        audit_id="audit.p1.development-panel",
        epoch=initial_input.epoch,
        tasks=(*initial_input.task_panel, pilot_task),
        inspected_at_ms=1_000,
    )
    audit = reserve_audited_pilot_task(
        audit,
        pilot_id="pilot.p1.heldout",
        task_manifest_digest=pilot_task.task_manifest_digest,
        reserved_at_ms=1_001,
    )
    assert audit.reserved_task is not None
    assert audit.reserved_task.task_manifest_id not in {
        task.task_manifest_id for task in initial_input.task_panel
    }

    cli, wheel_python = _install_built_wheel(tmp_path)
    cli_environment, docker_marker = _cli_environment(tmp_path)
    source_build_request = _write_json(
        tmp_path / "source-build-request.json",
        initial_input.model_dump(mode="json", exclude_none=True),
    )
    authored_factory = _author_wheel_fixture(
        wheel_python,
        cli_environment,
        tmp_path,
        "factory",
        "--build-input",
        str(source_build_request),
        "--output",
        str(tmp_path / "factory-replay.json"),
        "--manifest-id",
        "factory-replay.p1-end-to-end",
    )
    factory_manifest_path = Path(authored_factory["manifest_path"])
    assert authored_factory["proposal_calls"] > 0
    assert authored_factory["evaluator_calls"] > 0
    assert authored_factory["execution_mode"] == "offline_scripted"

    target_project = tmp_path / "factory-target"
    target_input, _, _ = _build_input(
        target_project,
        task_ids=("task.search.1", "task.search.2"),
        pilot_task_digest=pilot_task.task_manifest_digest,
    )
    build_request = _write_json(
        tmp_path / "build-request.json",
        target_input.model_dump(mode="json", exclude_none=True),
    )
    built = _run(
        [
            str(cli),
            "build-runtime",
            str(target_project),
            "--request-json",
            str(build_request),
            "--replay-manifest",
            str(factory_manifest_path),
        ],
        cwd=tmp_path,
        environment=cli_environment,
        timeout=180.0,
    )
    assert built.returncode == 0, built.stderr or built.stdout
    build_payload = json.loads(built.stdout)
    assert build_payload["status"] == "succeeded"
    assert build_payload["replay_provenance"] == {
        **build_payload["replay_provenance"],
        "execution_mode": "deterministic_replay",
        "live_inference_status": "not_run",
        "real_inference_requests_sent": 0,
    }
    initial_release_digest = build_payload["result"]["release_pointer"][
        "release_digest"
    ]
    assert initial_release_digest == authored_factory["release_digest"]
    initial_release_bytes = _generation_bytes(target_project, initial_release_digest)
    release = load_active_harness_release(target_project)
    assert release.protocol.source_digest() != target_input.founding_protocol.source_digest()
    assert any(
        channel.channel_id.startswith("gain-")
        for channel in release.protocol.artifact_channels
    )
    raw_digest = repository_snapshot_digest(raw_repository)

    task_path = _write_json(
        tmp_path / "pilot-task.json",
        pilot_task.model_dump(mode="json"),
    )
    good_pair = _pair_key(pilot_task, target_input.epoch, 0)
    good_pair_path = _write_json(
        tmp_path / "good-pair.json",
        good_pair.model_dump(mode="json"),
    )
    session_store = HarnessSessionStore(target_project)
    session_id = "session.p1.initial-release"
    session_store.create_session(
        active_release_digest=initial_release_digest,
        session_id=session_id,
    )
    initial_session_context = session_store.context_for_next(
        session_id,
        active_release_digest=initial_release_digest,
    ).to_public_runtime_context()
    initial_session_context_path = _write_json(
        tmp_path / "initial-session-context.json",
        initial_session_context.model_dump(mode="json"),
    )
    authored_good = _author_wheel_fixture(
        wheel_python,
        cli_environment,
        tmp_path,
        "solve",
        "--project",
        str(target_project),
        "--task",
        str(task_path),
        "--pair-key",
        str(good_pair_path),
        "--session-context",
        str(initial_session_context_path),
        "--replacement",
        "VALUE = 2\n",
        "--output-root",
        str(tmp_path / "good-replay"),
        "--run-id",
        "run.p1.good",
        "--workspace-id",
        "workspace.p1.good",
    )
    good_provider = Path(authored_good["provider_manifest_path"])
    good_commands = Path(authored_good["command_manifest_path"])
    good_patch = authored_good["submitted_patch"]
    assert authored_good["executed_commands"] > 0
    good_workspace = tmp_path / "good-source-hidden-run"
    good_process, good_payload = _run_cli_solve(
        cli=cli,
        environment=cli_environment,
        project=target_project,
        task_path=task_path,
        pair_path=good_pair_path,
        provider_manifest=good_provider,
        command_manifest=good_commands,
        workspace=good_workspace,
        run_id="run.p1.good",
        workspace_id="workspace.p1.good",
        session_id=session_id,
    )
    assert good_process.returncode == 0, good_process.stderr or good_process.stdout
    good_result = good_payload["result"]
    assert good_result["status"] == "completed"
    assert good_result["execution_mode"] == "deterministic_replay"
    assert good_result["live_inference_status"] == "not_run"
    assert good_result["real_inference_requests_sent"] == 0
    assert good_result["eligible_for_evaluator_submission"] is True
    good_evidence = load_controlled_run_evidence(
        good_workspace,
        good_result["controlled_run_evidence"],
    )
    assert good_evidence.pair_key == good_pair
    assert good_evidence.patch.observed is not None
    assert good_evidence.patch.observed.value == good_patch
    assert repository_snapshot_digest(raw_repository) == raw_digest

    assert good_payload["session"]["session_id"] == session_id
    session_context = session_store.context_for_next(
        session_id,
        active_release_digest=initial_release_digest,
    ).to_public_runtime_context()
    assert session_context.next_sequence == 1
    assert len(session_context.model_dump_json().encode("utf-8")) < 64_000

    wrong_pair = _pair_key(pilot_task, target_input.epoch, 1)
    wrong_pair_path = _write_json(
        tmp_path / "wrong-pair.json",
        wrong_pair.model_dump(mode="json"),
    )
    session_context_path = _write_json(
        tmp_path / "continued-session-context.json",
        session_context.model_dump(mode="json"),
    )
    authored_wrong = _author_wheel_fixture(
        wheel_python,
        cli_environment,
        tmp_path,
        "solve",
        "--project",
        str(target_project),
        "--task",
        str(task_path),
        "--pair-key",
        str(wrong_pair_path),
        "--session-context",
        str(session_context_path),
        "--replacement",
        "VALUE = 2  # plausible but wrong\n",
        "--output-root",
        str(tmp_path / "wrong-replay"),
        "--run-id",
        "run.p1.wrong",
        "--workspace-id",
        "workspace.p1.wrong",
    )
    wrong_provider = Path(authored_wrong["provider_manifest_path"])
    wrong_commands = Path(authored_wrong["command_manifest_path"])
    wrong_patch = authored_wrong["submitted_patch"]
    assert authored_wrong["executed_commands"] > 0
    wrong_workspace = tmp_path / "wrong-source-hidden-run"
    wrong_process, wrong_payload = _run_cli_solve(
        cli=cli,
        environment=cli_environment,
        project=target_project,
        task_path=task_path,
        pair_path=wrong_pair_path,
        provider_manifest=wrong_provider,
        command_manifest=wrong_commands,
        workspace=wrong_workspace,
        run_id="run.p1.wrong",
        workspace_id="workspace.p1.wrong",
        session_id=session_id,
    )
    assert wrong_process.returncode == 0, wrong_process.stderr or wrong_process.stdout
    wrong_result = wrong_payload["result"]
    wrong_evidence = load_controlled_run_evidence(
        wrong_workspace,
        wrong_result["controlled_run_evidence"],
    )
    assert wrong_evidence.pair_key == wrong_pair
    assert any(
        entry.source_kind == "session"
        for context in wrong_evidence.contexts
        for entry in context.entries
    )
    assert repository_snapshot_digest(raw_repository) == raw_digest

    from agintor.runtime.harness_profile import HarnessDeploymentProfile

    deployment_profile = HarnessDeploymentProfile.model_validate(release.profile.profile)
    contract = _evaluation_contract(
        task=pilot_task,
        epoch=target_input.epoch,
        profile=deployment_profile,
    )
    contract_path = _write_json(
        tmp_path / "evaluation-contract.json",
        contract.model_dump(mode="json"),
    )
    proof_root = tmp_path / "controlled-proofs"
    evaluator_results: list[dict[str, Any]] = []
    for label, pair, evidence, patch, expected in (
        ("good", good_pair, good_evidence, good_patch, True),
        ("wrong", wrong_pair, wrong_evidence, wrong_patch, False),
    ):
        patch_path = tmp_path / "evaluator-patches" / f"{label}.patch"
        patch_path.parent.mkdir(parents=True, exist_ok=True)
        patch_path.write_bytes(patch.encode("utf-8"))
        authored_evaluator = _author_wheel_fixture(
            wheel_python,
            cli_environment,
            tmp_path,
            "evaluator",
            "--project",
            str(target_project),
            "--task",
            str(task_path),
            "--contract",
            str(contract_path),
            "--patch",
            str(patch_path),
            "--expected-complete-repair",
            str(expected).lower(),
            "--output-root",
            str(tmp_path / f"{label}-evaluator-replay"),
        )
        evaluator_manifest = Path(authored_evaluator["command_manifest_path"])
        assert authored_evaluator["complete_repair"] is expected
        assert authored_evaluator["executed_commands"] == 3
        evaluator_request = _write_json(
            tmp_path / "evaluator-requests" / f"{label}.json",
            {
                "schema_version": HARNESS_EVALUATION_ENTRY_REQUEST_SCHEMA_VERSION,
                "operation": "evaluate",
                "execution": {
                    "mode": "replay",
                    "command_manifest_path": str(evaluator_manifest),
                },
                "epoch": target_input.epoch.model_dump(mode="json"),
                "contract": contract.model_dump(mode="json"),
                "task": pilot_task.model_dump(mode="json"),
                "submitted_unified_diff": patch,
                "pair_key": pair.model_dump(mode="json"),
                "run_evidence": evidence.model_dump(mode="json"),
                "digest_assertions": {
                    "release_digest": initial_release_digest,
                    "epoch_manifest_digest": target_input.epoch.epoch_manifest_digest,
                },
                "proof_store_root": str(proof_root),
            },
        )
        evaluator_output = tmp_path / "evaluator-outputs" / f"{label}.json"
        evaluated = _run(
            [
                str(cli),
                "eval",
                "--project-root",
                str(target_project),
                "--request-json",
                str(evaluator_request),
                "--output-json",
                str(evaluator_output),
            ],
            cwd=tmp_path,
            environment=cli_environment,
            timeout=180.0,
        )
        assert evaluated.returncode == 0, evaluated.stderr or evaluated.stdout
        evaluator_payload = json.loads(evaluated.stdout)
        assert evaluator_payload == json.loads(
            evaluator_output.read_text(encoding="utf-8")
        )
        evaluation_summary = evaluator_payload["result"]["summary"]
        assert evaluation_summary["complete_repair"] is expected
        assert evaluation_summary["run_evidence_digest"] == evidence.evidence_digest
        assert "P1-SEALED-CANARY-DO-NOT-PUBLISH" not in evaluated.stdout
        assert "evaluation_contract_digest" not in evaluated.stdout
        evaluator_results.append(evaluator_payload)

    proof_store = ImmutableProofRecordStore(proof_root)
    proof_records = tuple(proof_store.iter_records())
    assert len(proof_records) == 2
    assert {record.run_evidence.evidence_digest for record in proof_records} == {
        good_evidence.evidence_digest,
        wrong_evidence.evidence_digest,
    }
    assert {
        record.outcome_receipt.complete_repair
        for record in proof_records
        if record.outcome_receipt is not None
    } == {False, True}

    initial_message_id = build_payload["result"]["factory_message"]["message_id"]
    followup_input, _, _ = _build_input(
        target_project,
        prompt="Retain the proven repair harness and add a second structural evidence channel.",
        founding_protocol=release.protocol,
        task_ids=("task.search.1", "task.search.2"),
        pilot_task_digest=pilot_task.task_manifest_digest,
        expected_parent_message_id=initial_message_id,
        expected_message_index=1,
    )
    followup_source_project = tmp_path / "followup-factory-replay-source"
    followup_bootstrap_input = target_input.model_copy(
        update={"project_root": str(followup_source_project)}
    )
    followup_bootstrap_request = _write_json(
        tmp_path / "followup-bootstrap-build-request.json",
        followup_bootstrap_input.model_dump(mode="json", exclude_none=True),
    )
    followup_bootstrapped = _run(
        [
            str(cli),
            "build-runtime",
            str(followup_source_project),
            "--request-json",
            str(followup_bootstrap_request),
            "--replay-manifest",
            str(factory_manifest_path),
        ],
        cwd=tmp_path,
        environment=cli_environment,
        timeout=180.0,
    )
    assert followup_bootstrapped.returncode == 0, (
        followup_bootstrapped.stderr or followup_bootstrapped.stdout
    )
    assert json.loads(followup_bootstrapped.stdout)["result"]["release_pointer"][
        "release_digest"
    ] == initial_release_digest
    followup_source_input = followup_input.model_copy(
        update={"project_root": str(followup_source_project)}
    )
    followup_source_request = _write_json(
        tmp_path / "followup-source-build-request.json",
        followup_source_input.model_dump(mode="json", exclude_none=True),
    )
    authored_followup = _author_wheel_fixture(
        wheel_python,
        cli_environment,
        tmp_path,
        "factory",
        "--build-input",
        str(followup_source_request),
        "--output",
        str(tmp_path / "followup-factory-replay.json"),
        "--manifest-id",
        "factory-replay.p1-followup",
        "--proposal-index",
        "1",
    )
    followup_request = _write_json(
        tmp_path / "followup-build-request.json",
        followup_input.model_dump(mode="json", exclude_none=True),
    )
    followed_up = _run(
        [
            str(cli),
            "build-runtime",
            str(target_project),
            "--request-json",
            str(followup_request),
            "--replay-manifest",
            authored_followup["manifest_path"],
        ],
        cwd=tmp_path,
        environment=cli_environment,
        timeout=180.0,
    )
    assert followed_up.returncode == 0, followed_up.stderr or followed_up.stdout
    followup_payload = json.loads(followed_up.stdout)
    new_release_digest = followup_payload["result"]["release_pointer"][
        "release_digest"
    ]
    assert new_release_digest == authored_followup["release_digest"]
    assert new_release_digest != initial_release_digest
    assert _generation_bytes(target_project, initial_release_digest) == initial_release_bytes

    rejected_workspace = tmp_path / "rejected-old-session-run"
    rejected, rejected_payload = _run_cli_solve(
        cli=cli,
        environment=cli_environment,
        project=target_project,
        task_path=task_path,
        pair_path=good_pair_path,
        provider_manifest=good_provider,
        command_manifest=good_commands,
        workspace=rejected_workspace,
        run_id="run.p1.old-session-rejected",
        workspace_id="workspace.p1.old-session-rejected",
        session_id=session_id,
    )
    assert rejected.returncode == 2
    assert rejected_payload["code"] == "session_release_mismatch"
    assert not rejected_workspace.exists()

    new_pair = _pair_key(pilot_task, target_input.epoch, 2)
    new_pair_path = _write_json(
        tmp_path / "new-release-pair.json",
        new_pair.model_dump(mode="json"),
    )
    new_session_id = "session.p1.followup-release"
    session_store.create_session(
        active_release_digest=new_release_digest,
        session_id=new_session_id,
    )
    new_session_context = session_store.context_for_next(
        new_session_id,
        active_release_digest=new_release_digest,
    ).to_public_runtime_context()
    new_session_context_path = _write_json(
        tmp_path / "new-release-session-context.json",
        new_session_context.model_dump(mode="json"),
    )
    authored_new = _author_wheel_fixture(
        wheel_python,
        cli_environment,
        tmp_path,
        "solve",
        "--project",
        str(target_project),
        "--task",
        str(task_path),
        "--pair-key",
        str(new_pair_path),
        "--session-context",
        str(new_session_context_path),
        "--replacement",
        "VALUE = 2\n",
        "--output-root",
        str(tmp_path / "new-release-replay"),
        "--run-id",
        "run.p1.new-release",
        "--workspace-id",
        "workspace.p1.new-release",
    )
    new_provider = Path(authored_new["provider_manifest_path"])
    new_commands = Path(authored_new["command_manifest_path"])
    new_workspace = tmp_path / "new-release-run"
    new_process, new_payload = _run_cli_solve(
        cli=cli,
        environment=cli_environment,
        project=target_project,
        task_path=task_path,
        pair_path=new_pair_path,
        provider_manifest=new_provider,
        command_manifest=new_commands,
        workspace=new_workspace,
        run_id="run.p1.new-release",
        workspace_id="workspace.p1.new-release",
        session_id=new_session_id,
    )
    assert new_process.returncode == 0, new_process.stderr or new_process.stdout
    assert new_payload["session"]["session_id"] == new_session_id
    assert new_session_id != session_id
    assert new_payload["session"]["active_release_digest"] == new_release_digest
    assert new_payload["result"]["execution_mode"] == "deterministic_replay"
    assert new_payload["result"]["live_inference_status"] == "not_run"
    assert new_payload["result"]["real_inference_requests_sent"] == 0
    assert repository_snapshot_digest(raw_repository) == raw_digest
    assert _generation_bytes(target_project, initial_release_digest) == initial_release_bytes

    readiness_packet, readiness_artifacts = _readiness_packet_fixture()
    _, readiness_epoch, readiness_task, _, _ = _readiness_core()
    readiness_contract = _readiness_evaluation_contract(
        readiness_epoch,
        readiness_task,
    )
    readiness_root = tmp_path / "readiness-controlled"
    readiness_root.mkdir()
    contract_relative = "authority/evaluation-contract.json"
    _write_json(
        readiness_root / contract_relative,
        readiness_contract.model_dump(mode="json"),
    )
    artifact_sources: list[dict[str, str]] = []
    for index, (packet_path, value) in enumerate(sorted(readiness_artifacts.items())):
        source_relative = f"inputs/artifacts/{index:04d}.bin"
        source_path = readiness_root / source_relative
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(_readiness_artifact_raw(value))
        artifact_sources.append(
            {"packet_path": packet_path, "source_path": source_relative}
        )
    build_request_relative = "requests/build.json"
    _write_json(
        readiness_root / build_request_relative,
        {
            "schema_version": "repo-repair-harness-readiness-entry-request-v1",
            "operation": "build",
            "packet_id": readiness_packet.packet_id,
            "destination_root": "generations",
            "evaluation_contract_source_path": contract_relative,
            "artifacts": artifact_sources,
            "release": readiness_packet.release.model_dump(mode="json"),
            "gate0": readiness_packet.gate0.model_dump(mode="json"),
            "d0": readiness_packet.d0.model_dump(mode="json"),
            "s1": readiness_packet.s1.model_dump(mode="json"),
            "solve_execution": readiness_packet.solve_execution.model_dump(mode="json"),
            "task_audit": readiness_artifacts[
                "controlled_development_and_evaluator_evidence/evaluator/task_audit_manifest.json"
            ].model_dump(mode="json"),
            "pilot_dry_run": readiness_artifacts[
                "controlled_development_and_evaluator_evidence/pilot/dry_run_manifest.json"
            ].model_dump(mode="json"),
            "pilot_report": readiness_artifacts[
                "controlled_development_and_evaluator_evidence/analysis/pilot_report.json"
            ].model_dump(mode="json"),
            "factory_followup": readiness_packet.factory_followup.model_dump(mode="json"),
            "runtime_sessions": readiness_packet.runtime_sessions.model_dump(mode="json"),
            "limitations": list(readiness_packet.limitations),
        },
    )
    readiness_built = _run(
        [
            str(cli),
            "readiness-build",
            str(readiness_root),
            "--request-json",
            build_request_relative,
        ],
        cwd=tmp_path,
        environment=cli_environment,
        timeout=180.0,
    )
    assert readiness_built.returncode == 0, (
        readiness_built.stderr or readiness_built.stdout
    )
    readiness_build_payload = json.loads(readiness_built.stdout)
    assert readiness_build_payload["packet_digest"] == readiness_packet.packet_digest
    assert readiness_build_payload["live_status"] == "not_run"
    assert readiness_build_payload["real_inference_requests_sent"] == 0
    replay_request_relative = "requests/replay.json"
    _write_json(
        readiness_root / replay_request_relative,
        {
            "schema_version": "repo-repair-harness-readiness-entry-request-v1",
            "operation": "replay",
            "generation_path": readiness_build_payload["packet_path"],
            "evaluation_contract_source_path": contract_relative,
        },
    )
    readiness_replayed = _run(
        [
            str(cli),
            "readiness-replay",
            str(readiness_root),
            "--request-json",
            replay_request_relative,
        ],
        cwd=tmp_path,
        environment=cli_environment,
        timeout=180.0,
    )
    assert readiness_replayed.returncode == 0, (
        readiness_replayed.stderr or readiness_replayed.stdout
    )
    readiness_replay_payload = json.loads(readiness_replayed.stdout)
    assert readiness_replay_payload["packet_digest"] == readiness_packet.packet_digest
    assert readiness_replay_payload["packet_path"] == readiness_build_payload["packet_path"]
    assert "P1-SEALED-CANARY" not in (
        readiness_built.stdout + readiness_replayed.stdout
    )
    assert not docker_marker.exists()
