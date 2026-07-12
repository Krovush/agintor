from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, Callable

import pytest

from agintor.contracts.epochs import (
    PublicReproductionStep,
    TaskCeilings,
    TaskEnvelope,
    WorkspaceSnapshotRef,
)
from agintor.contracts.harness import (
    DependencyRef,
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
    ProviderCallControl,
    ProviderInvocation,
)
from agintor.runtime.kernel.composite_runtime import (
    ActorCallOutput,
    ActorCallRequest,
    ActorTerminalTurn,
    ActorToolRequest,
    CompositeRuntime,
    CompositeRuntimeError,
    ScratchWorkspaceBinding,
)
from agintor.runtime.kernel.repair_tools import (
    RepairToolLimits,
    RepairToolStatus,
    TrustedRepairToolService,
)
from agintor.utils import count_tokens_rough


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _write_source(root: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir(parents=True)
    (root / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "src" / "other.py").write_text("def helper():\n    return VALUE\n", encoding="utf-8")
    (root / "tests" / "test_public.py").write_text(
        "def test_value():\n    assert True\n",
        encoding="utf-8",
    )


def _task(source: Path, **ceiling_updates: Any) -> TaskEnvelope:
    ceilings = {
        "max_model_calls": 10,
        "max_input_tokens": 50_000,
        "max_output_tokens": 20_000,
        "max_cached_tokens": 0,
        "max_tool_calls": 20,
        "max_tool_output_bytes": 300_000,
        "max_artifact_bytes": 40_000,
        "max_patch_bytes": 20_000,
        "max_retries": 1,
        "max_wall_time_ms": 30_000,
        "provider_deadline_ms": 5_000,
        "max_known_cost_usd": 1.0,
        "max_estimated_cost_usd": 2.0,
    }
    ceilings.update(ceiling_updates)
    return TaskEnvelope(
        task_manifest_id="repair-tool-task",
        epoch_id="repo-repair-development",
        epoch_manifest_digest=_digest("epoch"),
        data_state="development",
        split_manifest_digest=_digest("split"),
        issue="Change VALUE from 1 to 2 while preserving the public tests.",
        workspace_snapshot=WorkspaceSnapshotRef(
            snapshot_id="source.clean",
            uri=str(source),
            digest=repository_snapshot_digest(source),
        ),
        public_reproduction=(
            PublicReproductionStep(
                step_id="public-test",
                argv=("python", "-m", "pytest", "-q"),
                cwd=".",
                timeout_ms=2_000,
            ),
        ),
        ceilings=TaskCeilings.model_validate(ceilings),
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


class RecordingIsolationBackend:
    def __init__(
        self,
        *,
        status: IsolatedCommandStatus = IsolatedCommandStatus.COMPLETED,
        exit_code: int | None = 0,
        callback: Callable[[IsolatedCommandRequest], None] | None = None,
    ) -> None:
        self.status = status
        self.exit_code = exit_code
        self.callback = callback
        self.requests: list[IsolatedCommandRequest] = []

    def run(self, request: IsolatedCommandRequest) -> IsolatedCommandResult:
        self.requests.append(request)
        if self.callback is not None:
            self.callback(request)
        stdout = "public output\n"
        stderr = "" if self.exit_code == 0 else "public failure\n"
        return IsolatedCommandResult(
            status=self.status,
            command=request.command,
            container_name="offline-isolation",
            exit_code=self.exit_code,
            stdout=stdout,
            stderr=stderr,
            stdout_digest=hashlib.sha256(stdout.encode()).hexdigest(),
            stderr_digest=hashlib.sha256(stderr.encode()).hexdigest(),
            duration_s=0.01,
            output_truncated=self.status is IsolatedCommandStatus.OUTPUT_LIMIT,
            failure_detail=None,
        )


class RaisingMutatingIsolationBackend(RecordingIsolationBackend):
    def run(self, request: IsolatedCommandRequest) -> IsolatedCommandResult:
        self.requests.append(request)
        if self.callback is not None:
            self.callback(request)
        raise RuntimeError("candidate command crashed after mutating protected tests")


def _workspace_service(
    tmp_path: Path,
    *,
    backend: RecordingIsolationBackend | None = None,
    limits: RepairToolLimits | None = None,
    **ceiling_updates: Any,
) -> tuple[TaskEnvelope, TaskWorkspace, TrustedRepairToolService, RecordingIsolationBackend]:
    source = tmp_path / "source"
    _write_source(source)
    task = _task(source, **ceiling_updates)
    workspace = materialize_task_workspace(task.workspace_snapshot, tmp_path / "run")
    recorder = backend or RecordingIsolationBackend()
    service = TrustedRepairToolService(
        task,
        workspace,
        recorder,
        limits=limits,
    )
    return task, workspace, service, recorder


def test_fixed_tools_are_bounded_charged_and_mutate_only_working_copy(tmp_path: Path) -> None:
    task, workspace, service, _backend = _workspace_service(tmp_path)
    ledger = AggregateBudgetLedger(task.ceilings)

    search = service.invoke(
        call_id="actor.investigator.initial",
        tool_id="repo.search",
        arguments={"query": "VALUE", "path": "src"},
        ledger=ledger,
        phase="actor_tool",
        tool_request_id="test.request.search",
        verification_step_id=None,
    )
    read = service.invoke(
        call_id="actor.investigator.initial",
        tool_id="repo.read",
        arguments={"path": "src/app.py"},
        ledger=ledger,
        phase="actor_tool",
        tool_request_id="test.request.read",
        verification_step_id=None,
    )
    edit = service.invoke(
        call_id="actor.implementer.initial",
        tool_id="repo.edit",
        arguments={"path": "src/app.py", "content": "VALUE = 2\n"},
        ledger=ledger,
        phase="actor_tool",
        tool_request_id="test.request.edit",
        verification_step_id=None,
    )
    diff = service.invoke(
        call_id="actor.implementer.initial",
        tool_id="repo.diff",
        arguments={},
        ledger=ledger,
        phase="actor_tool",
        tool_request_id="test.request.diff",
        verification_step_id=None,
    )

    assert search.succeeded and search.output["matches"][0]["path"] == "src/app.py"
    assert read.succeeded and read.output["content"] == "VALUE = 1\n"
    assert edit.succeeded
    assert diff.succeeded
    assert "-VALUE = 1" in diff.output["patch"]
    assert "+VALUE = 2" in diff.output["patch"]
    assert (workspace.working_root / "src" / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert (workspace.immutable_base_root / "src" / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert (workspace.source_root / "src" / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    receipts = service.receipts()
    assert len(receipts) == 4
    assert all(receipt.charged for receipt in receipts)
    assert ledger.snapshot().tool_calls == 4
    assert ledger.snapshot().tool_output_bytes == sum(receipt.output_bytes for receipt in receipts)
    assert service.source_snapshot_unchanged()
    assert service.immutable_base_unchanged()


@pytest.mark.parametrize(
    ("path", "error"),
    [
        ("../escape.py", "invalid_path"),
        ("/host/escape.py", "invalid_path"),
        ("C:/host/escape.py", "invalid_path"),
        ("tests/test_public.py", "protected_path"),
        (".git/config", "protected_path"),
    ],
)
def test_edit_traversal_absolute_and_protected_paths_fail_with_charged_receipts(
    tmp_path: Path,
    path: str,
    error: str,
) -> None:
    task, workspace, service, _backend = _workspace_service(tmp_path)
    ledger = AggregateBudgetLedger(task.ceilings)

    result = service.invoke(
        call_id="actor.implementer.initial",
        tool_id="repo.edit",
        arguments={"path": path, "content": "ESCAPED = True\n"},
        ledger=ledger,
        phase="actor_tool",
        tool_request_id="test.request.rejected-edit",
        verification_step_id=None,
    )

    assert result.receipt.status is RepairToolStatus.FAILED
    assert result.receipt.error_code == error
    assert result.receipt.charged is True
    assert ledger.snapshot().tool_calls == 1
    assert not (tmp_path / "escape.py").exists()
    assert (workspace.source_root / "src" / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_public_test_accepts_no_command_environment_or_network_override(tmp_path: Path) -> None:
    task, workspace, service, backend = _workspace_service(tmp_path)
    ledger = AggregateBudgetLedger(task.ceilings)

    rejected = service.invoke(
        call_id="actor.investigator.initial",
        tool_id="repo.public_test",
        arguments={
            "command": ["python", "-c", "print('escape')"],
            "environment": {"OPENAI_API_KEY": "secret"},
            "network": "open",
        },
        ledger=ledger,
        phase="actor_tool",
        tool_request_id="test.request.rejected-public",
        verification_step_id=None,
    )
    passed = service.invoke(
        call_id="actor.investigator.initial",
        tool_id="repo.public_test",
        arguments={},
        ledger=ledger,
        phase="actor_tool",
        tool_request_id="test.request.public",
        verification_step_id=None,
    )

    assert rejected.receipt.error_code == "invalid_arguments"
    assert backend.requests == [backend.requests[0]]
    request = backend.requests[0]
    step = task.public_reproduction[0]
    assert request.command == step.argv
    assert request.working_directory == step.cwd
    assert request.timeout_s == step.timeout_ms / 1000.0
    assert request.environment == {}
    assert request.workspace != workspace.working_root.resolve()
    assert not request.workspace.exists()
    assert passed.succeeded
    assert ledger.snapshot().tool_calls == 2


@pytest.mark.skipif(os.name != "posix", reason="POSIX container-user mode boundary")
def test_public_test_mount_copy_is_nonroot_accessible_without_chmodding_runtime_tree(
    tmp_path: Path,
) -> None:
    observed: list[Path] = []

    def assert_mount_permissions(request: IsolatedCommandRequest) -> None:
        observed.append(request.workspace)
        assert stat.S_IMODE(request.workspace.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(request.workspace.stat().st_mode) == 0o777
        assert stat.S_IMODE((request.workspace / "src").stat().st_mode) == 0o777
        assert stat.S_IMODE((request.workspace / "src" / "app.py").stat().st_mode) == 0o777
        assert stat.S_IMODE((request.workspace / "src" / "other.py").stat().st_mode) == 0o666

    backend = RecordingIsolationBackend(callback=assert_mount_permissions)
    task, workspace, service, _ = _workspace_service(tmp_path, backend=backend)
    working_src = workspace.working_root / "src"
    working_app = working_src / "app.py"
    working_other = working_src / "other.py"
    working_src.chmod(0o700)
    working_app.chmod(0o700)
    working_other.chmod(0o600)
    source_modes = {
        path: stat.S_IMODE(path.stat().st_mode)
        for path in (
            workspace.source_root / "src",
            workspace.source_root / "src" / "app.py",
            workspace.immutable_base_root / "src",
            workspace.immutable_base_root / "src" / "app.py",
        )
    }
    ledger = AggregateBudgetLedger(task.ceilings)

    result = service.invoke(
        call_id="actor.investigator.initial",
        tool_id="repo.public_test",
        arguments={},
        ledger=ledger,
        phase="actor_tool",
        tool_request_id="test.request.public-permissions",
        verification_step_id=None,
    )

    assert result.succeeded
    assert len(observed) == 1
    assert not observed[0].exists()
    assert stat.S_IMODE(working_src.stat().st_mode) == 0o700
    assert stat.S_IMODE(working_app.stat().st_mode) == 0o700
    assert stat.S_IMODE(working_other.stat().st_mode) == 0o600
    assert {path: stat.S_IMODE(path.stat().st_mode) for path in source_modes} == source_modes


def test_public_test_rejects_disposable_source_mutation_and_preserves_runtime_tree(
    tmp_path: Path,
) -> None:
    observed: dict[str, Any] = {}

    def mutate_disposable_copy(request: IsolatedCommandRequest) -> None:
        observed["workspace"] = request.workspace
        observed["workspace_digest_before"] = repository_snapshot_digest(
            request.workspace
        )
        (request.workspace / "src" / "app.py").write_text(
            "VALUE = 999\n",
            encoding="utf-8",
        )
        cache = request.workspace / "src" / "__pycache__"
        cache.mkdir()
        (cache / "app.cpython-312.pyc").write_bytes(b"cache")

    backend = RecordingIsolationBackend(callback=mutate_disposable_copy)
    task, workspace, service, _backend = _workspace_service(tmp_path, backend=backend)
    ledger = AggregateBudgetLedger(task.ceilings)
    before = service.current_workspace_digest()

    result = service.invoke(
        call_id="actor.implementer.initial",
        tool_id="repo.public_test",
        arguments={},
        ledger=ledger,
        phase="actor_tool",
        tool_request_id="test.request.disposable-public",
        verification_step_id=None,
    )

    assert result.receipt.status is RepairToolStatus.FAILED
    assert result.receipt.error_code == "public_test_workspace_changed"
    assert result.output["passed"] is False
    assert result.receipt.command_evidence[0].passed is False
    assert observed["workspace"] != workspace.working_root.resolve()
    assert observed["workspace_digest_before"] == before
    assert not observed["workspace"].exists()
    assert service.current_workspace_digest() == before
    assert result.receipt.workspace_digest_before == before
    assert result.receipt.workspace_digest_after == before
    assert (workspace.working_root / "src" / "app.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 1\n"
    assert not (workspace.working_root / "src" / "__pycache__").exists()


def test_public_test_allows_ignored_cache_writes_in_disposable_workspace(
    tmp_path: Path,
) -> None:
    observed: dict[str, Path] = {}

    def write_ignored_cache(request: IsolatedCommandRequest) -> None:
        observed["workspace"] = request.workspace
        cache = request.workspace / "src" / "__pycache__"
        cache.mkdir()
        (cache / "app.cpython-312.pyc").write_bytes(b"cache")

    backend = RecordingIsolationBackend(callback=write_ignored_cache)
    task, workspace, service, _backend = _workspace_service(tmp_path, backend=backend)
    ledger = AggregateBudgetLedger(task.ceilings)
    before = service.current_workspace_digest()

    result = service.invoke(
        call_id="actor.implementer.initial",
        tool_id="repo.public_test",
        arguments={},
        ledger=ledger,
        phase="actor_tool",
        tool_request_id="test.request.disposable-cache",
        verification_step_id=None,
    )

    assert result.succeeded
    assert observed["workspace"] != workspace.working_root.resolve()
    assert not observed["workspace"].exists()
    assert result.receipt.workspace_digest_before == before
    assert result.receipt.workspace_digest_after == before
    assert service.current_workspace_digest() == before
    assert not (workspace.working_root / "src" / "__pycache__").exists()


@pytest.mark.parametrize(
    ("status", "expected_status"),
    [
        (IsolatedCommandStatus.TIMED_OUT, RepairToolStatus.TIMED_OUT),
        (IsolatedCommandStatus.OUTPUT_LIMIT, RepairToolStatus.OUTPUT_LIMIT),
        (IsolatedCommandStatus.LAUNCH_FAILED, RepairToolStatus.LAUNCH_FAILED),
    ],
)
def test_public_test_isolation_failures_are_typed_and_charged(
    tmp_path: Path,
    status: IsolatedCommandStatus,
    expected_status: RepairToolStatus,
) -> None:
    backend = RecordingIsolationBackend(status=status, exit_code=None)
    task, _workspace, service, _backend = _workspace_service(tmp_path, backend=backend)
    ledger = AggregateBudgetLedger(task.ceilings)

    result = service.invoke(
        call_id="actor.implementer.initial",
        tool_id="repo.public_test",
        arguments={},
        ledger=ledger,
        phase="actor_tool",
        tool_request_id="test.request.public-failure",
        verification_step_id=None,
    )

    assert result.receipt.status is expected_status
    assert result.receipt.charged is True
    assert result.receipt.command_evidence[0].status == status.value
    assert ledger.snapshot().tool_calls == 1


@pytest.mark.parametrize("backend_raises", [False, True])
def test_public_command_mutating_disposable_tests_does_not_poison_runtime_diff(
    tmp_path: Path,
    backend_raises: bool,
) -> None:
    def tamper(request: IsolatedCommandRequest) -> None:
        (request.workspace / "tests" / "test_public.py").write_text(
            "def test_value():\n    assert False\n",
            encoding="utf-8",
        )

    backend_type = (
        RaisingMutatingIsolationBackend if backend_raises else RecordingIsolationBackend
    )
    backend = backend_type(callback=tamper)
    task, workspace, service, _backend = _workspace_service(tmp_path, backend=backend)
    ledger = AggregateBudgetLedger(task.ceilings)

    public_test = service.invoke(
        call_id="actor.implementer.initial",
        tool_id="repo.public_test",
        arguments={},
        ledger=ledger,
        phase="actor_tool",
        tool_request_id="test.request.protected-mutation",
        verification_step_id=None,
    )
    diff = service.invoke(
        call_id="actor.implementer.initial",
        tool_id="repo.diff",
        arguments={},
        ledger=ledger,
        phase="actor_tool",
        tool_request_id="test.request.diff-after-protected-mutation",
        verification_step_id=None,
    )

    assert public_test.receipt.status is RepairToolStatus.FAILED
    if backend_raises:
        assert public_test.receipt.error_code == "internal_RuntimeError"
    else:
        assert public_test.receipt.error_code == "public_test_workspace_changed"
        assert public_test.receipt.command_evidence[0].passed is False
    assert diff.succeeded
    assert diff.output["patch"] == ""
    assert service.protected_tree_unchanged() is True
    assert backend.requests[0].workspace != workspace.working_root.resolve()
    assert not backend.requests[0].workspace.exists()
    assert (workspace.source_root / "tests" / "test_public.py").read_text(
        encoding="utf-8"
    ) == "def test_value():\n    assert True\n"
    assert (workspace.immutable_base_root / "tests" / "test_public.py").read_text(
        encoding="utf-8"
    ) == "def test_value():\n    assert True\n"
    assert (workspace.working_root / "tests" / "test_public.py").read_text(
        encoding="utf-8"
    ) == "def test_value():\n    assert True\n"


class ToolLoopProvider:
    def __init__(
        self,
        *,
        forged_patch: bool = False,
        recover_from_failed_tool: bool = False,
    ) -> None:
        self.forged_patch = forged_patch
        self.recover_from_failed_tool = recover_from_failed_tool
        self.requests: list[ActorCallRequest] = []

    def invoke(
        self,
        request: Any,
        *,
        control: ProviderCallControl,
        credential_reference,
    ) -> ProviderInvocation:
        del control, credential_reference
        normalized = ActorCallRequest.model_validate(request)
        self.requests.append(normalized)
        if normalized.call_id == "actor.investigator.initial":
            if normalized.turn_index == 0:
                turn: Any = ActorToolRequest(
                    request_id="investigator.search",
                    tool_id="repo.search",
                    arguments={"query": "VALUE", "path": "src"},
                )
            else:
                turn = ActorTerminalTurn(
                    output=ActorCallOutput(
                        output_text="Located VALUE in src/app.py.",
                        artifact_payloads={
                            "artifact.investigation": "Edit src/app.py from VALUE = 1 to VALUE = 2."
                        },
                    )
                )
        else:
            if normalized.turn_index == 0:
                turn = ActorToolRequest(
                    request_id="implementer.edit",
                    tool_id="repo.edit",
                    arguments={
                        "path": "tests/test_public.py" if self.recover_from_failed_tool else "src/app.py",
                        "content": "VALUE = 2\n",
                    },
                )
            elif self.recover_from_failed_tool and normalized.turn_index == 1:
                assert normalized.tool_results[-1].status == "failed"
                assert normalized.tool_results[-1].output == {"error": "protected_path"}
                turn = ActorToolRequest(
                    request_id="implementer.edit.recovery",
                    tool_id="repo.edit",
                    arguments={"path": "src/app.py", "content": "VALUE = 2\n"},
                )
            elif normalized.turn_index == (2 if self.recover_from_failed_tool else 1):
                turn = ActorToolRequest(
                    request_id="implementer.diff",
                    tool_id="repo.diff",
                    arguments={},
                )
            else:
                patch = str(normalized.tool_results[-1].output["patch"])
                if self.forged_patch:
                    patch = patch.replace("+VALUE = 2", "+VALUE = 999")
                turn = ActorTerminalTurn(
                    output=ActorCallOutput(
                        output_text="Repair complete.",
                        final_patch=patch,
                    )
                )
        output_tokens = count_tokens_rough(json.dumps(turn.model_dump(mode="json"), sort_keys=True))
        return ProviderInvocation(
            response=turn,
            usage=ProviderUsageReport(
                usage_status=UsageStatus.KNOWN,
                input_tokens=normalized.input_token_estimate,
                output_tokens=output_tokens,
                cached_tokens=0,
                cost_status=CostStatus.KNOWN,
                cost_usd=0.0,
                response_id=f"offline.{normalized.call_id}.{normalized.turn_index}",
            ),
        )


def _compiled_runtime(
    task: TaskEnvelope,
    workspace: TaskWorkspace,
    service: TrustedRepairToolService,
    provider: ToolLoopProvider,
) -> CompositeRuntime:
    plan = compile_composite_run_plan(
        task,
        load_canonical_harness_seed().protocol,
        _dependencies(task),
    )
    return CompositeRuntime(
        plan,
        task,
        ScratchWorkspaceBinding(
            workspace_id="scratch.r2",
            workspace_digest=task.workspace_snapshot.digest,
        ),
        provider,
        run_id="run.r2.offline",
        tool_interface=service,
    )


def test_actor_turn_loop_edits_diffs_and_completes_exact_public_verification(tmp_path: Path) -> None:
    def assert_fixed(request: IsolatedCommandRequest) -> None:
        text = (request.workspace / "src" / "app.py").read_text(encoding="utf-8")
        assert text == "VALUE = 2\n"

    backend = RecordingIsolationBackend(callback=assert_fixed)
    task, workspace, service, _backend = _workspace_service(tmp_path, backend=backend)
    provider = ToolLoopProvider()

    result = _compiled_runtime(task, workspace, service, provider).run()

    assert result.status == "completed"
    assert result.public_verification_status == "passed"
    assert result.public_verification.passed is True
    assert result.source_snapshot_unchanged is True
    assert result.final_patch == service.workspace_diff(max_patch_bytes=task.ceilings.max_patch_bytes)
    assert result.final_workspace_digest == service.current_workspace_digest()
    assert result.budget.model_calls == 5
    assert result.budget.tool_calls == 4
    assert result.budget.reconciled is True
    assert [len(call.provider_rounds) for call in result.actor_calls] == [2, 3]
    rounds = [round_ for call in result.actor_calls for round_ in call.provider_rounds]
    assert [round_.response_id for round_ in rounds] == [
        "offline.actor.investigator.initial.0",
        "offline.actor.investigator.initial.1",
        "offline.actor.implementer.initial.0",
        "offline.actor.implementer.initial.1",
        "offline.actor.implementer.initial.2",
    ]
    assert all(round_.usage.response_id == round_.response_id for round_ in rounds)
    assert sum(round_.usage.input_tokens or 0 for round_ in rounds) == result.budget.input_tokens
    assert len(rounds) == result.budget.model_calls
    assert [receipt.tool_id for receipt in result.tool_receipts] == [
        "repo.search",
        "repo.edit",
        "repo.diff",
        "repo.public_test",
    ]
    assert backend.requests[0].command == task.public_reproduction[0].argv
    assert backend.requests[0].environment == {}
    assert repository_snapshot_digest(workspace.source_root) == task.workspace_snapshot.digest
    assert repository_snapshot_digest(workspace.immutable_base_root) == task.workspace_snapshot.digest


def test_terminal_public_verification_mutations_cannot_drift_final_patch(
    tmp_path: Path,
) -> None:
    observed_request_digests: list[str] = []

    def mutate_verification_copy(request: IsolatedCommandRequest) -> None:
        observed_request_digests.append(repository_snapshot_digest(request.workspace))
        (request.workspace / "src" / "app.py").write_text(
            "VALUE = 999\n",
            encoding="utf-8",
        )
        cache = request.workspace / "src" / "__pycache__"
        cache.mkdir()
        (cache / "app.cpython-312.pyc").write_bytes(b"cache")

    backend = RecordingIsolationBackend(callback=mutate_verification_copy)
    task, workspace, service, _backend = _workspace_service(tmp_path, backend=backend)
    provider = ToolLoopProvider()

    result = _compiled_runtime(task, workspace, service, provider).run()

    assert result.status == "public_verification_failed"
    assert result.public_verification_status == "failed"
    assert result.public_verification.passed is False
    assert result.tool_receipts[-1].status is RepairToolStatus.FAILED
    assert result.tool_receipts[-1].error_code == "public_test_workspace_changed"
    assert result.final_patch == service.workspace_diff(
        max_patch_bytes=task.ceilings.max_patch_bytes
    )
    assert result.final_workspace_digest == service.current_workspace_digest()
    assert (workspace.working_root / "src" / "app.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 2\n"
    assert not (workspace.working_root / "src" / "__pycache__").exists()
    assert observed_request_digests == [result.final_workspace_digest]
    assert backend.requests[0].workspace != workspace.working_root.resolve()
    assert not backend.requests[0].workspace.exists()


def test_failed_tool_receipt_is_visible_to_next_provider_round_and_can_recover(tmp_path: Path) -> None:
    task, workspace, service, _backend = _workspace_service(tmp_path)
    provider = ToolLoopProvider(recover_from_failed_tool=True)

    result = _compiled_runtime(task, workspace, service, provider).run()

    assert result.status == "completed"
    implementer_requests = [
        request for request in provider.requests if request.call_id == "actor.implementer.initial"
    ]
    assert implementer_requests[1].tool_results[-1].status == "failed"
    assert implementer_requests[1].tool_results[-1].output == {"error": "protected_path"}
    assert [receipt.status.value for receipt in result.tool_receipts[:2]] == [
        "succeeded",
        "failed",
    ]
    assert result.budget.model_calls == 6
    assert result.budget.tool_calls == 5


def test_final_patch_must_exactly_match_workspace_diff_before_public_test(tmp_path: Path) -> None:
    task, workspace, service, backend = _workspace_service(tmp_path)
    provider = ToolLoopProvider(forged_patch=True)

    with pytest.raises(CompositeRuntimeError) as raised:
        _compiled_runtime(task, workspace, service, provider).run()

    assert raised.value.kind == "final_patch_workspace_mismatch"
    assert backend.requests == []
    assert repository_snapshot_digest(workspace.source_root) == task.workspace_snapshot.digest


def test_failed_public_verification_returns_honest_non_success_status(tmp_path: Path) -> None:
    backend = RecordingIsolationBackend(exit_code=1)
    task, workspace, service, _backend = _workspace_service(tmp_path, backend=backend)

    result = _compiled_runtime(task, workspace, service, ToolLoopProvider()).run()

    assert result.status == "public_verification_failed"
    assert result.public_verification_status == "failed"
    assert result.public_verification.passed is False
    assert result.tool_receipts[-1].status is RepairToolStatus.FAILED
    assert result.source_snapshot_unchanged is True


def test_public_command_tampering_with_source_is_detected(tmp_path: Path) -> None:
    source_holder: dict[str, Path] = {}

    def tamper(_request: IsolatedCommandRequest) -> None:
        (source_holder["source"] / "src" / "app.py").write_text(
            "VALUE = 999\n",
            encoding="utf-8",
        )

    backend = RecordingIsolationBackend(callback=tamper)
    task, workspace, service, _backend = _workspace_service(tmp_path, backend=backend)
    source_holder["source"] = workspace.source_root

    with pytest.raises(CompositeRuntimeError) as raised:
        _compiled_runtime(task, workspace, service, ToolLoopProvider()).run()

    assert raised.value.kind == "immutable_source_changed"


def test_read_edit_and_search_limits_fail_closed_with_receipts(tmp_path: Path) -> None:
    limits = RepairToolLimits(
        max_read_bytes=8,
        max_read_lines=2,
        max_search_files=1,
        max_search_results=1,
        max_search_output_bytes=128,
        max_edit_bytes=8,
        max_command_output_bytes=128,
        max_receipt_bytes=512,
    )
    task, _workspace, service, _backend = _workspace_service(tmp_path, limits=limits)
    ledger = AggregateBudgetLedger(task.ceilings)

    read = service.invoke(
        call_id="actor.investigator.initial",
        tool_id="repo.read",
        arguments={"path": "src/app.py"},
        ledger=ledger,
        phase="actor_tool",
        tool_request_id="test.request.limit-read",
        verification_step_id=None,
    )
    edit = service.invoke(
        call_id="actor.implementer.initial",
        tool_id="repo.edit",
        arguments={"path": "src/app.py", "content": "VALUE = 200\n"},
        ledger=ledger,
        phase="actor_tool",
        tool_request_id="test.request.limit-edit",
        verification_step_id=None,
    )
    search = service.invoke(
        call_id="actor.investigator.initial",
        tool_id="repo.search",
        arguments={"query": "VALUE"},
        ledger=ledger,
        phase="actor_tool",
        tool_request_id="test.request.limit-search",
        verification_step_id=None,
    )

    assert read.receipt.error_code == "read_byte_limit"
    assert edit.receipt.error_code == "edit_byte_limit"
    assert search.receipt.error_code == "search_file_limit"
    assert all(receipt.charged for receipt in service.receipts())
    assert ledger.snapshot().tool_calls == 3
