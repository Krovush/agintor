from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from types import SimpleNamespace

import pytest

from agintor.artifacts import ArtifactMode
from agintor import state_store
from agintor.exceptions import HardInvalidation, PromptAdaptationError, ResumeRecoveryError
from agintor.project import init_runtime
from agintor.providers import LocalDeterministicProvider, ReplayProvider, clone_provider
from agintor.runtime_api import (
    AgentFrame,
    PolicyContext,
    RuntimeBudget,
    RuntimeState,
    batch_evaluation_unit_key,
    compile_execution_plan_from_solve_request,
    compile_execution_plan_from_task,
    execution_plan_requirements,
    execution_plan_requires_default_provider,
    load_solve_request,
    reduce_grouped_run_results,
    solve_request_from_resume_checkpoint,
    runtime_solve_request_for_task,
    runtime_solve_request_for_user_request,
)
from agintor.runtime_host import RuntimeHost
from agintor.run_store import RunStore
from agintor.runtime_loader import load_runtime
from agintor.runtime_profile import load_runtime_profile
from agintor.runner import TaskRuntime
from agintor.schemas import (
    AgentTemplate,
    AsyncHandle,
    BenchmarkTask,
    BranchBudget,
    BranchPlan,
    BranchResult,
    BranchResumeSnapshot,
    BranchState,
    CancellationRecord,
    Checkpoint,
    CheckpointEnvelope,
    ChildSpec,
    ExecutionUnitRequestEnvelope,
    OpenAITraceContext,
    ModelResponse,
    OperationSpec,
    QueuedAgentSnapshot,
    QueuedFrameSnapshot,
    ReplayAllocation,
    ResumeRequest,
    RunResult,
    RuntimeTaskInvocation,
    SideEffectReceipt,
    capability_scope_allows,
    capability_scope_service_transports,
    service_action_transport_compatibility,
)
from agintor.shell import FixedShell
from agintor.tool_runtime import _AsyncProcessRecord
from agintor.utils import now_ts, stable_hash
from agintor.versioning import RUNTIME_CONTRACT_VERSION


class ReconcilingReplayProvider(ReplayProvider):
    def __init__(self, rows, *, reconciled=None, coordinator=None):
        super().__init__(rows, coordinator=coordinator)
        self.reconciled = dict(reconciled or {})
        self.generate_calls = 0
        self.reconcile_calls = []

    def _spawn_clone(self, coordinator):
        return ReconcilingReplayProvider([], reconciled=self.reconciled, coordinator=coordinator)

    def generate(self, request):
        self.generate_calls += 1
        return super().generate(request)

    def reconcile_request(self, idempotency_key, receipt):
        self.reconcile_calls.append(idempotency_key)
        return self.reconciled.get(idempotency_key)


class CapturingProvider(ReplayProvider):
    def __init__(self, *, response_text: str):
        super().__init__([])
        self.response_text = response_text
        self.prompts: list[str] = []

    def generate(self, request):
        self.prompts.append(str(request.prompt))
        return ModelResponse(text=self.response_text, model_name="capture/small")


class DelayingReplayProvider(ReplayProvider):
    def __init__(self, rows, *, delays_by_worker=None, coordinator=None):
        super().__init__(rows, coordinator=coordinator)
        self.delays_by_worker = dict(delays_by_worker or {})

    def _spawn_clone(self, coordinator):
        return DelayingReplayProvider([], delays_by_worker=self.delays_by_worker, coordinator=coordinator)

    def generate(self, request):
        trace_context = dict(getattr(request, "metadata", {}).get("trace_context", {}))
        worker_id = str(trace_context.get("worker_id", "") or "")
        time.sleep(float(self.delays_by_worker.get(worker_id, 0.0) or 0.0))
        return super().generate(request)


def _make_direct_response_task(task_id: str) -> BenchmarkTask:
    return BenchmarkTask(
        task_id=task_id,
        family="top",
        prompt="Say hello.",
        task_type="structured_ops",
        symbolic_seeds=[],
        file_paths=[],
        allowed_tool_categories=[],
        context_items=[],
        operations=[
            OperationSpec(
                op_id="respond",
                kind="direct_response",
                output_key="response",
                description="Say hello.",
                args={},
                externally_visible=True,
            )
        ],
        expected=None,
        verifier_type="none",
        externally_visible=True,
        verification_required=False,
        allow_best_effort=True,
    )


def _make_builtin_sum_task(task_id: str) -> BenchmarkTask:
    return BenchmarkTask(
        task_id=task_id,
        family="tool",
        prompt="Compute the sum.",
        task_type="structured_ops",
        symbolic_seeds=[],
        file_paths=[],
        allowed_tool_categories=["math/basic"],
        context_items=[],
        operations=[
            OperationSpec(
                op_id="sum",
                kind="builtin",
                output_key="sum",
                description="Compute sum of numbers.",
                tool_hint="math/basic/sum_numbers",
                args={"numbers": [2, 3, 5]},
                externally_visible=True,
            )
        ],
        expected=None,
        verifier_type="none",
        externally_visible=True,
        verification_required=False,
        allow_best_effort=True,
    )


def _make_service_action_task(
    task_id: str,
    *,
    url: str = "https://service.example.test/status",
    method: str = "GET",
) -> BenchmarkTask:
    return BenchmarkTask(
        task_id=task_id,
        family="e2e",
        prompt=f"{method} {url}",
        task_type="bounded_service_action",
        symbolic_seeds=[],
        file_paths=[],
        allowed_tool_categories=["service/http"],
        context_items=[],
        operations=[
            OperationSpec(
                op_id="service_call",
                kind="service_action",
                output_key="service_result",
                description=f"{method} {url}",
                args={
                    "url": url,
                    "method": method,
                    "headers": {},
                    "body": None,
                    "timeout_s": 10.0,
                    "service_transport": "http",
                },
                externally_visible=True,
            )
        ],
        expected=None,
        verifier_type="none",
        externally_visible=True,
        verification_required=False,
        allow_best_effort=True,
    )


def _make_exact_direct_response_task(
    task_id: str,
    *,
    expected,
    verifier_type: str,
) -> BenchmarkTask:
    return BenchmarkTask(
        task_id=task_id,
        family="top",
        prompt="Return the expected answer.",
        task_type="structured_ops",
        symbolic_seeds=[],
        file_paths=[],
        allowed_tool_categories=[],
        context_items=[],
        operations=[
            OperationSpec(
                op_id="respond",
                kind="direct_response",
                output_key="answer",
                description="Return the expected answer.",
                args={},
                externally_visible=True,
            )
        ],
        expected=expected,
        verifier_type=verifier_type,
        externally_visible=True,
        verification_required=True,
        allow_best_effort=False,
    )


def _make_parallel_direct_response_task(task_id: str) -> BenchmarkTask:
    return BenchmarkTask(
        task_id=task_id,
        family="top",
        prompt="Say hello.",
        task_type="structured_ops",
        symbolic_seeds=[],
        file_paths=[],
        allowed_tool_categories=[],
        context_items=[],
        operations=[
            OperationSpec(
                op_id="respond_a",
                kind="direct_response",
                output_key="response_a",
                description="Say hello from branch A.",
                args={},
                externally_visible=True,
            ),
            OperationSpec(
                op_id="respond_b",
                kind="direct_response",
                output_key="response_b",
                description="Say hello from branch B.",
                args={},
                externally_visible=True,
            ),
        ],
        expected=None,
        verifier_type="none",
        externally_visible=True,
        verification_required=False,
        allow_best_effort=True,
    )


def test_compile_execution_plan_rejects_duplicate_output_keys(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    task = BenchmarkTask(
        task_id="duplicate.outputs",
        family="top",
        prompt="Exercise duplicate output validation.",
        task_type="structured_ops",
        symbolic_seeds=[],
        file_paths=[],
        allowed_tool_categories=["math/basic"],
        context_items=[],
        operations=[
            OperationSpec(op_id="a", kind="builtin", output_key="same", description="first", args={"numbers": [1, 2]}),
            OperationSpec(op_id="b", kind="builtin", output_key="same", description="second", args={"numbers": [3, 4]}),
        ],
        expected=None,
        verifier_type="none",
        verification_required=False,
        allow_best_effort=True,
    )

    with pytest.raises(ValueError, match="duplicate execution plan output_key"):
        compile_execution_plan_from_task(
            task,
            request_id="duplicate.outputs.request",
            seed=0,
            runtime_hash=runtime.runtime_hash,
            runtime_dir=str(runtime.runtime_dir),
        )


def _build_mixed_frontier_context(tmp_path: Path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    runner = TaskRuntime(runtime, shell, ReplayProvider([]))
    task = BenchmarkTask(
        task_id="frontier.order",
        family="top",
        prompt="Exercise mixed runnable frontier ordering.",
        task_type="structured_ops",
        symbolic_seeds=[],
        file_paths=[],
        allowed_tool_categories=[],
        context_items=[],
        operations=[
            OperationSpec(op_id="dep_a", kind="builtin", output_key="dep_a", description="dep a", args={}),
            OperationSpec(op_id="dep_b", kind="builtin", output_key="dep_b", description="dep b", args={}),
            OperationSpec(
                op_id="a",
                kind="builtin",
                output_key="a",
                description="singleton after dep_a",
                args={},
                dependencies=["dep_a"],
            ),
            OperationSpec(
                op_id="b",
                kind="builtin",
                output_key="b",
                description="grouped after dep_b",
                args={},
                dependencies=["dep_b"],
            ),
            OperationSpec(
                op_id="c",
                kind="builtin",
                output_key="c",
                description="grouped after dep_b",
                args={},
                dependencies=["dep_b"],
            ),
        ],
        expected=None,
        verifier_type="none",
        externally_visible=False,
        verification_required=False,
        allow_best_effort=True,
    )
    plan = compile_execution_plan_from_task(
        task,
        request_id="frontier.order.request",
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    state = RuntimeState(
        request_id=plan.request_id,
        plan_id=plan.plan_id,
        execution_state="running",
    )
    root_merge_node = next(
        node
        for node in plan.nodes
        if str(node.node_kind) == "merge" and list(node.dependencies) == ["dep_a", "dep_b"]
    )
    for node_id in ["dep_a", "dep_b", root_merge_node.node_id]:
        state.plan_node_status[node_id] = "completed"
    context = PolicyContext(
        runtime_dir=runtime.runtime_dir,
        shell=shell,
        task=task,
        request_id=plan.request_id,
        plan=plan,
        trace_context=plan.trace_context,
        provider=ReplayProvider([]),
        seed=0,
        state=state,
        budget=RuntimeBudget(),
        trace=[],
        objective=plan.objective,
    )
    grouped_frontier_id = next(
        str(node.branch_group_id)
        for node in plan.nodes
        if node.node_id == "b" and node.branch_group_id
    )
    return runner, plan, context, grouped_frontier_id


def _branch_result(
    branch_plan: BranchPlan,
    *,
    status: str,
    artifact=None,
    failure_kind: str | None = None,
    cancellation_reason: str | None = None,
) -> BranchResult:
    branch_state_kwargs = {
        "branch_id": branch_plan.branch_id,
        "status": status,
        "parent_frame_id": branch_plan.parent_frame_id,
        "assigned_node_ids": list(branch_plan.assigned_node_ids),
        "merge_priority": branch_plan.merge_priority,
        "predicted_solve": branch_plan.predicted_solve,
        "reserved_budget": branch_plan.reserved_budget,
    }
    if status == "failed":
        branch_state_kwargs.update(
            failure_kind=failure_kind or "protocol_failure",
            failure_details={"source": "test"},
            error=f"{branch_plan.branch_id} failed",
        )
    elif status == "cancelled":
        branch_state_kwargs.update(
            cancellation_record=CancellationRecord(
                reason=cancellation_reason or "fatal_branch_fault",
                details={},
                created_at=now_ts(),
            )
        )
    return BranchResult(
        branch_plan=branch_plan,
        branch_state=BranchState(**branch_state_kwargs),
        artifact=artifact,
        verifier_support=0.0,
        unresolved_critical=0 if status == "completed" else len(branch_plan.assigned_node_ids),
    )


def _canonical_root_snapshot() -> QueuedAgentSnapshot:
    return QueuedAgentSnapshot(
        restore_mode="canonical_clone",
        canonical_agent_id="root",
        agent_payload=AgentTemplate(
            agent_id="root",
            description="General root coordinator",
            capability_set=["plan", "merge", "verify"],
            symbol_set=[],
            default_tool_scope=[],
            success_stats={"global": 0.5},
            staleness_clock=0,
            model_policy_tag="medium",
        ),
    )


def _force_horizontal(monkeypatch, runtime, worker_ids):
    monkeypatch.setattr(runtime.topology, "select_mode", lambda ctx, frame, operations: "horizontal")

    def _workers(ctx, frame, operations):
        op_ids = [operation.node_id for operation in operations]
        ranked = []
        for index, worker_id in enumerate(worker_ids):
            ranked.append(
                {
                    "worker_id": worker_id,
                    "instruction": f"worker-{worker_id}",
                    "op_ids": op_ids,
                    "predicted_solve": 0.9 - (0.1 * index),
                    "tool_scope": ctx.state.visible_tool_names,
                    "agent_id": "root",
                }
            )
        return ranked

    monkeypatch.setattr(runtime.topology, "select_workers", _workers)


def _checkpoint_for_boundary(shell: FixedShell, request_id: str, boundary: str) -> CheckpointEnvelope:
    index_path = shell.workspace / "checkpoints" / request_id / "index.json"
    rows = json.loads(index_path.read_text(encoding="utf-8"))
    target = max(
        (row for row in rows if row["boundary"] == boundary),
        key=lambda row: int(row.get("sequence_no", 0) or 0),
    )
    return shell.load_checkpoint_envelope(checkpoint_ref=target["checkpoint_ref"])


def _pending_provider_launch_envelope(
    runtime,
    shell: FixedShell,
    *,
    task_id: str,
) -> CheckpointEnvelope:
    task = _make_direct_response_task(task_id)
    plan = compile_execution_plan_from_task(
        task,
        request_id=task_id,
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    queued_frame = QueuedFrameSnapshot(
        frame_id="frame-root",
        request_id=plan.request_id,
        plan_id=plan.plan_id,
        objective=plan.objective,
        operation_ids=["respond"],
        depth=0,
        role="root",
        trace_context=plan.trace_context,
        tool_scope=[],
        agent_snapshot=_canonical_root_snapshot(),
    )
    return CheckpointEnvelope(
        checkpoint_id=f"checkpoint.{task_id}.0001",
        runtime_contract_version=runtime.kernel_manifest.runtime_contract_version,
        runtime_hash=runtime.runtime_hash,
        request_id=plan.request_id,
        plan_id=plan.plan_id,
        task_id=task.task_id,
        seed=0,
        sequence_no=1,
        boundary="after_provider_launch",
        created_at=now_ts(),
        plan_snapshot=(plan).model_dump(),
        task_payload=(task).model_dump(),
        runtime_state_snapshot={
            "request_id": plan.request_id,
            "plan_id": plan.plan_id,
            "execution_state": "running",
            "checkpoint_sequence_no": 1,
            "queued_frames": [(queued_frame).model_dump()],
            "visible_tool_names": sorted(shell.tool_registry.tools),
            "plan_node_status": {"respond": "running"},
        },
        shell_state_snapshot=(shell.snapshot_checkpoint_shell_state()).model_dump(),
        side_effect_ledger={
            "receipts": [
                SideEffectReceipt(
                    side_effect_id="provider-request.pending",
                    action_fingerprint="provider-request.pending",
                    idempotency_key="provider-request.pending",
                    action_kind="provider_request",
                    request_id=plan.request_id,
                    plan_id=plan.plan_id,
                    frame_id="frame-root",
                    node_id="respond",
                    request_digest="provider-request.pending",
                    backend="local",
                    status="launched",
                    trace_context=OpenAITraceContext(
                        request_id=plan.request_id,
                        task_id=task.task_id,
                        seed=0,
                        op_id="respond",
                    ),
                    result_ref={"request": {"prompt": task.prompt, "model_class": "medium"}},
                    created_at=now_ts(),
                )
            ]
        },
    )


def _pending_sync_tool_launch_envelope(
    runtime,
    shell: FixedShell,
    *,
    task_id: str,
) -> CheckpointEnvelope:
    task = _make_builtin_sum_task(task_id)
    plan = compile_execution_plan_from_task(
        task,
        request_id=task_id,
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    queued_frame = QueuedFrameSnapshot(
        frame_id="frame-root",
        request_id=plan.request_id,
        plan_id=plan.plan_id,
        objective=plan.objective,
        operation_ids=["sum"],
        depth=0,
        role="root",
        trace_context=plan.trace_context,
        tool_scope=[],
        agent_snapshot=_canonical_root_snapshot(),
    )
    tool_name = "math/basic/sum_numbers"
    idempotency_key = stable_hash(plan.request_id, "sum", tool_name, {"numbers": [2, 3, 5]})
    return CheckpointEnvelope(
        checkpoint_id=f"checkpoint.{task_id}.0001",
        runtime_contract_version=runtime.kernel_manifest.runtime_contract_version,
        runtime_hash=runtime.runtime_hash,
        request_id=plan.request_id,
        plan_id=plan.plan_id,
        task_id=task.task_id,
        seed=0,
        sequence_no=1,
        boundary="after_tool_launch",
        created_at=now_ts(),
        plan_snapshot=(plan).model_dump(),
        task_payload=(task).model_dump(),
        runtime_state_snapshot={
            "request_id": plan.request_id,
            "plan_id": plan.plan_id,
            "execution_state": "running",
            "checkpoint_sequence_no": 1,
            "queued_frames": [(queued_frame).model_dump()],
            "visible_tool_names": sorted(shell.tool_registry.tools),
            "plan_node_status": {"sum": "running"},
        },
        shell_state_snapshot=(shell.snapshot_checkpoint_shell_state()).model_dump(),
        side_effect_ledger={
            "receipts": [
                SideEffectReceipt(
                    side_effect_id=f"tool-launch.{idempotency_key[:12]}",
                    action_fingerprint=idempotency_key,
                    idempotency_key=idempotency_key,
                    action_kind="tool_launch",
                    request_id=plan.request_id,
                    plan_id=plan.plan_id,
                    frame_id="frame-root",
                    node_id="sum",
                    request_digest=idempotency_key,
                    backend="local",
                    status="launched",
                    trace_context=OpenAITraceContext(
                        request_id=plan.request_id,
                        task_id=task.task_id,
                        seed=0,
                        op_id="sum",
                    ),
                    result_ref={"tool_name": tool_name, "launch_mode": "sync"},
                    created_at=now_ts(),
                )
            ]
        },
    )


def _pending_async_tool_launch_envelope(
    runtime,
    shell: FixedShell,
    *,
    task_id: str,
) -> CheckpointEnvelope:
    task = _make_builtin_sum_task(task_id)
    plan = compile_execution_plan_from_task(
        task,
        request_id=task_id,
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    queued_frame = QueuedFrameSnapshot(
        frame_id="frame-root",
        request_id=plan.request_id,
        plan_id=plan.plan_id,
        objective=plan.objective,
        operation_ids=["sum"],
        depth=0,
        role="root",
        trace_context=plan.trace_context,
        tool_scope=[],
        agent_snapshot=_canonical_root_snapshot(),
    )
    tool_name = "math/basic/sum_numbers"
    idempotency_key = stable_hash(plan.request_id, "sum", tool_name, {"numbers": [2, 3, 5]})
    handle = AsyncHandle(
        handle_id="handle.async.pending",
        tool_name=tool_name,
        sandbox_hash="sandbox-hash",
        working_directory=str(shell.workspace),
        launch_time=now_ts(),
        timeout=60.0,
        stdout_path=str(shell.workspace / "async.stdout"),
        stderr_path=str(shell.workspace / "async.stderr"),
        state="running",
        artifact_refs=[str(shell.workspace / "async.result.json")],
        process_pid=999999,
    )
    shell.open_handles.add(handle)
    return CheckpointEnvelope(
        checkpoint_id=f"checkpoint.{task_id}.0001",
        runtime_contract_version=runtime.kernel_manifest.runtime_contract_version,
        runtime_hash=runtime.runtime_hash,
        request_id=plan.request_id,
        plan_id=plan.plan_id,
        task_id=task.task_id,
        seed=0,
        sequence_no=1,
        boundary="after_tool_launch",
        created_at=now_ts(),
        plan_snapshot=(plan).model_dump(),
        task_payload=(task).model_dump(),
        runtime_state_snapshot={
            "request_id": plan.request_id,
            "plan_id": plan.plan_id,
            "execution_state": "running",
            "checkpoint_sequence_no": 1,
            "queued_frames": [(queued_frame).model_dump()],
            "visible_tool_names": sorted(shell.tool_registry.tools),
            "plan_node_status": {"sum": "running"},
            "open_handle_ids": [handle.handle_id],
        },
        shell_state_snapshot=(shell.snapshot_checkpoint_shell_state()).model_dump(),
        side_effect_ledger={
            "receipts": [
                SideEffectReceipt(
                    side_effect_id=f"tool-launch.{idempotency_key[:12]}",
                    action_fingerprint=idempotency_key,
                    idempotency_key=idempotency_key,
                    action_kind="tool_launch",
                    request_id=plan.request_id,
                    plan_id=plan.plan_id,
                    frame_id="frame-root",
                    node_id="sum",
                    request_digest=idempotency_key,
                    backend="local",
                    status="launched",
                    trace_context=OpenAITraceContext(
                        request_id=plan.request_id,
                        task_id=task.task_id,
                        seed=0,
                        op_id="sum",
                    ),
                    result_ref={
                        "tool_name": tool_name,
                        "launch_mode": "async",
                        "handle_id": handle.handle_id,
                    },
                    created_at=now_ts(),
                )
            ]
        },
    )


def _pending_service_action_launch_envelope(
    runtime,
    shell: FixedShell,
    *,
    task_id: str,
    url: str = "https://service.example.test/status",
    method: str = "GET",
) -> CheckpointEnvelope:
    task = _make_service_action_task(task_id, url=url, method=method)
    plan = compile_execution_plan_from_task(
        task,
        request_id=task_id,
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    queued_frame = QueuedFrameSnapshot(
        frame_id="frame-root",
        request_id=plan.request_id,
        plan_id=plan.plan_id,
        objective=plan.objective,
        operation_ids=["service_call"],
        depth=0,
        role="root",
        trace_context=plan.trace_context,
        tool_scope=[],
        agent_snapshot=_canonical_root_snapshot(),
    )
    request_payload = {
        "service_transport": "http",
        "url": url,
        "method": method,
        "headers": {},
        "body": None,
        "timeout_s": 10.0,
    }
    request_digest = stable_hash(
        plan.request_id,
        "service_call",
        "http",
        url,
        method,
        {},
        None,
        10.0,
    )
    return CheckpointEnvelope(
        checkpoint_id=f"checkpoint.{task_id}.0001",
        runtime_contract_version=runtime.kernel_manifest.runtime_contract_version,
        runtime_hash=runtime.runtime_hash,
        request_id=plan.request_id,
        plan_id=plan.plan_id,
        task_id=task.task_id,
        seed=0,
        sequence_no=1,
        boundary="after_service_action_launch",
        created_at=now_ts(),
        plan_snapshot=(plan).model_dump(),
        task_payload=(task).model_dump(),
        runtime_state_snapshot={
            "request_id": plan.request_id,
            "plan_id": plan.plan_id,
            "execution_state": "running",
            "checkpoint_sequence_no": 1,
            "queued_frames": [(queued_frame).model_dump()],
            "visible_tool_names": sorted(shell.tool_registry.tools),
            "plan_node_status": {"service_call": "running"},
        },
        shell_state_snapshot=(shell.snapshot_checkpoint_shell_state()).model_dump(),
        side_effect_ledger={
            "receipts": [
                SideEffectReceipt(
                    side_effect_id=f"service-action.launch.{request_digest[:12]}",
                    action_fingerprint=stable_hash("service_action", "http", url, method, {}, None, 10.0),
                    idempotency_key=request_digest,
                    action_kind="service_action",
                    request_id=plan.request_id,
                    plan_id=plan.plan_id,
                    frame_id="frame-root",
                    node_id="service_call",
                    request_digest=request_digest,
                    backend="local",
                    status="launched",
                    trace_context=OpenAITraceContext(
                        request_id=plan.request_id,
                        task_id=task.task_id,
                        seed=0,
                        op_id="service_call",
                    ),
                    result_ref={"request": request_payload},
                    created_at=now_ts(),
                )
            ]
        },
    )


def _branch_resume_checkpoint_envelope(
    runtime,
    shell: FixedShell,
    *,
    task_id: str,
    left_snapshot_kind: str,
    right_snapshot_kind: str,
) -> tuple[CheckpointEnvelope, dict[str, str]]:
    task = _make_parallel_direct_response_task(task_id)
    plan = compile_execution_plan_from_task(
        task,
        request_id=f"{task_id}.request",
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    root_frame_id = "frame-root"
    root_frame = QueuedFrameSnapshot(
        frame_id=root_frame_id,
        request_id=plan.request_id,
        plan_id=plan.plan_id,
        objective=plan.objective,
        operation_ids=[node.node_id for node in plan.nodes],
        depth=0,
        role="root",
        trace_context=plan.trace_context,
        tool_scope=sorted(shell.tool_registry.tools),
        model_class="medium",
        agent_snapshot=_canonical_root_snapshot(),
    )
    branch_nodes = {
        node.output_key: node
        for node in plan.nodes
        if node.node_id in {"respond_a", "respond_b"}
    }

    def make_branch_snapshot(
        branch_id: str,
        node_id: str,
        output_key: str,
        merge_priority: int,
        snapshot_kind: str,
    ) -> tuple[dict[str, object], dict[str, object], str | None]:
        branch_shell = shell.fork_branch(branch_id)
        branch_frame = QueuedFrameSnapshot(
            frame_id=f"frame-{branch_id}",
            request_id=plan.request_id,
            plan_id=plan.plan_id,
            objective=plan.objective,
            operation_ids=[node_id],
            depth=1,
            role="worker",
            worker_id=branch_id,
            tool_scope=sorted(shell.tool_registry.tools),
            model_class="small",
            trace_context=OpenAITraceContext(
                request_id=plan.request_id,
                task_id=task.task_id,
                seed=0,
                worker_id=branch_id,
                op_id=node_id,
            ),
            agent_snapshot=_canonical_root_snapshot(),
        )
        branch_plan = BranchPlan(
            branch_id=branch_id,
            parent_frame_id=root_frame_id,
            request_id=plan.request_id,
            trace_context=branch_frame.trace_context,
            assigned_node_ids=[node_id],
            merge_priority=merge_priority,
            predicted_solve=1.0 - (0.1 * merge_priority),
            reserved_budget=BranchBudget(model_calls_max=1, checks_max=0, latency_max=5.0),
        )
        branch_state = BranchState(
            branch_id=branch_id,
            status="running",
            parent_frame_id=root_frame_id,
            assigned_node_ids=[node_id],
            merge_priority=merge_priority,
            predicted_solve=branch_plan.predicted_solve,
            reserved_budget=branch_plan.reserved_budget,
        )
        snapshot_payload = {
            "branch_plan": (branch_plan).model_dump(),
            "execution_state": "branching",
            "active_frame": (branch_frame).model_dump(),
            "queued_frames": [],
            "visible_tool_names": sorted(branch_shell.tool_registry.tools),
            "artifacts": {},
            "open_handle_ids": [],
            "plan_node_status": {},
            "branch_publications": [],
            "side_effect_receipts": [],
            "budget_totals": {"normalized": {}, "cost": 0.0, "latency": 0.0, "calls": 0, "checks": 0, "tokens": 0},
            "shell_state_snapshot": (branch_shell.snapshot_checkpoint_shell_state()).model_dump(),
            "created_tools": 0,
            "promoted_nodes": 0,
            "checks_used": 0,
        }
        receipt_key: str | None = None
        if snapshot_kind == "node_completed":
            snapshot_payload["artifacts"] = {output_key: f"{branch_id}-value"}
            snapshot_payload["plan_node_status"] = {node_id: "completed"}
        elif snapshot_kind == "provider_launch":
            receipt_key = f"{branch_id}.provider.pending"
            snapshot_payload["side_effect_receipts"] = [
                (SideEffectReceipt(
                        side_effect_id=f"provider-request.{branch_id}",
                        action_fingerprint=receipt_key,
                        idempotency_key=receipt_key,
                        action_kind="provider_request",
                        request_id=plan.request_id,
                        plan_id=plan.plan_id,
                        frame_id=branch_frame.frame_id,
                        node_id=node_id,
                        branch_id=branch_id,
                        trace_context=branch_frame.trace_context,
                        request_digest=receipt_key,
                        backend="local",
                        status="launched",
                        result_ref={"request": {"prompt": task.prompt, "model_class": "small"}},
                        created_at=now_ts(),
                    )).model_dump()
            ]
            snapshot_payload["plan_node_status"] = {node_id: "running"}
        else:
            snapshot_payload["plan_node_status"] = {}
        return (branch_state).model_dump(), snapshot_payload, receipt_key

    left_state, left_snapshot, left_receipt_key = make_branch_snapshot(
        "w0",
        "respond_a",
        branch_nodes["response_a"].output_key,
        0,
        left_snapshot_kind,
    )
    right_state, right_snapshot, right_receipt_key = make_branch_snapshot(
        "w1",
        "respond_b",
        branch_nodes["response_b"].output_key,
        1,
        right_snapshot_kind,
    )
    envelope = CheckpointEnvelope(
        checkpoint_id=f"checkpoint.{task_id}.0001",
        runtime_contract_version=runtime.kernel_manifest.runtime_contract_version,
        runtime_hash=runtime.runtime_hash,
        request_id=plan.request_id,
        plan_id=plan.plan_id,
        task_id=task.task_id,
        seed=0,
        sequence_no=1,
        boundary="after_branch_node_completion",
        created_at=now_ts(),
        plan_snapshot=(plan).model_dump(),
        task_payload=(task).model_dump(),
        runtime_state_snapshot={
            "request_id": plan.request_id,
            "plan_id": plan.plan_id,
            "execution_state": "running",
            "checkpoint_sequence_no": 1,
            "queued_frames": [(root_frame).model_dump()],
            "visible_tool_names": sorted(shell.tool_registry.tools),
            "plan_node_status": {},
            "branch_states": {"w0": left_state, "w1": right_state},
            "branch_publications": [],
            "branch_resume_snapshots": {
                "w0": (BranchResumeSnapshot(**left_snapshot)).model_dump(),
                "w1": (BranchResumeSnapshot(**right_snapshot)).model_dump(),
            },
        },
        shell_state_snapshot=(shell.snapshot_checkpoint_shell_state()).model_dump(),
    )
    receipt_keys = {
        key: value
        for key, value in {
            "w0": left_receipt_key,
            "w1": right_receipt_key,
        }.items()
        if value is not None
    }
    return envelope, receipt_keys


def _branch_owned_terminal_provider_receipt_envelope(
    runtime,
    shell: FixedShell,
    *,
    task_id: str,
    branch_id: str = "w0",
    text: str = "branch-owned",
) -> CheckpointEnvelope:
    task = _make_direct_response_task(task_id)
    plan = compile_execution_plan_from_task(
        task,
        request_id=task_id,
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    queued_frame = QueuedFrameSnapshot(
        frame_id="frame-root",
        request_id=plan.request_id,
        plan_id=plan.plan_id,
        objective=plan.objective,
        operation_ids=["respond"],
        depth=0,
        role="root",
        trace_context=plan.trace_context,
        tool_scope=[],
        agent_snapshot=_canonical_root_snapshot(),
    )
    return CheckpointEnvelope(
        checkpoint_id=f"checkpoint.{task_id}.0001",
        runtime_contract_version=runtime.kernel_manifest.runtime_contract_version,
        runtime_hash=runtime.runtime_hash,
        request_id=plan.request_id,
        plan_id=plan.plan_id,
        task_id=task.task_id,
        seed=0,
        sequence_no=1,
        boundary="after_provider_completion",
        created_at=now_ts(),
        plan_snapshot=(plan).model_dump(),
        task_payload=(task).model_dump(),
        runtime_state_snapshot={
            "request_id": plan.request_id,
            "plan_id": plan.plan_id,
            "execution_state": "running",
            "checkpoint_sequence_no": 1,
            "queued_frames": [(queued_frame).model_dump()],
            "visible_tool_names": sorted(shell.tool_registry.tools),
            "plan_node_status": {"respond": "running"},
        },
        shell_state_snapshot=(shell.snapshot_checkpoint_shell_state()).model_dump(),
        side_effect_ledger={
            "receipts": [
                SideEffectReceipt(
                    side_effect_id="provider-completion.branch-owned",
                    action_fingerprint="provider-completion.branch-owned",
                    idempotency_key="provider-completion.branch-owned",
                    action_kind="provider_completion",
                    request_id=plan.request_id,
                    plan_id=plan.plan_id,
                    frame_id="frame-worker",
                    node_id="respond",
                    branch_id=branch_id,
                    request_digest="provider-completion.branch-owned",
                    backend="local",
                    status="completed",
                    trace_context=OpenAITraceContext(
                        request_id=plan.request_id,
                        task_id=task.task_id,
                        seed=0,
                        op_id="respond",
                        worker_id=branch_id,
                    ),
                    result_ref={"text": text, "model_name": "replay/small"},
                    created_at=now_ts(),
                )
            ]
        },
    )


def _make_branch_cleanup_context(tmp_path: Path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    parent_shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    parent_runner = TaskRuntime(runtime, parent_shell, ReplayProvider([]))
    task = _make_parallel_direct_response_task("horizontal.cleanup")
    plan = compile_execution_plan_from_task(
        task,
        request_id="horizontal.cleanup.request",
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    branch_plan = BranchPlan(
        branch_id="w0",
        parent_frame_id="frame-root",
        request_id=plan.request_id,
        trace_context=OpenAITraceContext(request_id=plan.request_id, task_id=task.task_id, seed=0, worker_id="w0"),
        assigned_node_ids=["respond_a"],
        merge_priority=0,
        reserved_budget=BranchBudget(model_calls_max=1, checks_max=1, latency_max=5.0),
    )
    branch_shell = parent_shell.fork_branch(branch_plan.branch_id)
    branch_state = RuntimeState(
        request_id=plan.request_id,
        plan_id=plan.plan_id,
        execution_state="branching",
        visible_tool_names=sorted(branch_shell.tool_registry.tools),
    )
    branch_budget = RuntimeBudget()
    branch_context = PolicyContext(
        runtime_dir=runtime.runtime_dir,
        shell=branch_shell,
        task=task,
        request_id=plan.request_id,
        plan=plan,
        trace_context=branch_plan.trace_context,
        provider=ReplayProvider([]),
        seed=0,
        state=branch_state,
        budget=branch_budget,
        trace=[],
        objective=plan.objective,
        runtime_backend="local",
        cancellation_event=Event(),
    )
    branch_context.active_frame = AgentFrame(
        frame_id="frame-worker",
        agent=branch_shell.agent_pool.clone("root"),
        request_id=plan.request_id,
        plan_id=plan.plan_id,
        trace_context=branch_plan.trace_context,
        objective=plan.objective,
        operation_ids=["respond_a"],
        depth=1,
        role="worker",
        worker_id=branch_plan.branch_id,
        tool_scope=[],
        model_class="small",
    )
    return parent_runner, branch_plan, branch_context


def test_compile_execution_plan_from_solve_request_preserves_user_request_origin_and_plan_constants(tmp_path):
    solve_request = load_solve_request(
        prompt="Return a greeting as JSON.",
    )
    solve_request.output_schema = {"type": "object", "properties": {"message": {"type": "string"}}}

    task, plan = compile_execution_plan_from_solve_request(
        solve_request,
        seed=7,
        runtime_hash="runtime-hash",
        runtime_dir=str(tmp_path / "runtime"),
    )

    assert task.prompt == solve_request.prompt
    assert plan.origin.origin_kind == "user_request"
    assert plan.origin.source_request_id == solve_request.request_id
    assert plan.plan_constants["respond.request_id"] == solve_request.request_id
    assert plan.plan_constants["respond.output_schema"] == solve_request.output_schema
    assert {binding.source_ref for binding in plan.nodes[0].input_bindings if binding.source_kind == "plan_constant"} == {
        "respond.request_id",
        "respond.output_schema",
    }


def test_compile_execution_plan_from_solve_request_enriches_runtime_identity(tmp_path):
    solve_request = load_solve_request(prompt="Return a greeting as JSON.")

    task, plan = compile_execution_plan_from_solve_request(
        solve_request,
        seed=7,
        runtime_hash="runtime-hash",
        runtime_dir=str(tmp_path / "runtime"),
        trace_context=OpenAITraceContext(
            session_id="session-1",
            build_id="build-1",
            request_id="stale-request",
            task_id="stale-task",
            seed=999,
        ),
    )

    assert plan.trace_context.session_id == "session-1"
    assert plan.trace_context.build_id == "build-1"
    assert plan.trace_context.provider_role == "runtime"
    assert plan.trace_context.request_id == solve_request.request_id
    assert plan.trace_context.task_id == task.task_id
    assert plan.trace_context.seed == 7
    assert plan.trace_context.runtime_hash == "runtime-hash"
    assert plan.trace_context.runtime_dir == str(tmp_path / "runtime")


def test_execution_plan_digest_ignores_trace_provenance(monkeypatch, tmp_path):
    solve_request = load_solve_request(prompt="Return a greeting as JSON.")

    monkeypatch.setenv("AGINTOR_OPENAI_TRACE_SESSION_ID", "session.digest-one")
    _, first_plan = compile_execution_plan_from_solve_request(
        solve_request,
        seed=7,
        runtime_hash="runtime-hash",
        runtime_dir=str(tmp_path / "runtime"),
    )

    monkeypatch.setenv("AGINTOR_OPENAI_TRACE_SESSION_ID", "session.digest-two")
    _, second_plan = compile_execution_plan_from_solve_request(
        solve_request,
        seed=7,
        runtime_hash="runtime-hash",
        runtime_dir=str(tmp_path / "other-runtime"),
    )

    assert first_plan.trace_context.session_id == "session.digest-one"
    assert second_plan.trace_context.session_id == "session.digest-two"
    assert first_plan.trace_context.runtime_dir != second_plan.trace_context.runtime_dir
    assert first_plan.plan_digest == second_plan.plan_digest


def test_compile_execution_plan_from_solve_request_builds_file_inspection_template(tmp_path):
    inspected_file = tmp_path / "Folder With Spaces" / "notes file.txt"
    inspected_file.parent.mkdir(parents=True, exist_ok=True)
    inspected_file.write_text("important runtime note\n", encoding="utf-8")
    solve_request = load_solve_request(prompt=f"Inspect {inspected_file} and summarize it.")

    task, plan = compile_execution_plan_from_solve_request(
        solve_request,
        seed=3,
        runtime_hash="runtime-hash",
        runtime_dir=str(tmp_path / "runtime"),
    )

    assert task.task_type == "file_inspection"
    assert plan.file_ref_specs[0].source_path == str(inspected_file)
    assert plan.file_ref_specs[0].runtime_path == str(inspected_file)
    read_node = next(node for node in plan.nodes if node.node_id == "read_file_0")
    respond_node = next(node for node in plan.nodes if node.node_id == "respond")
    assert read_node.node_kind == "tool_call"
    assert respond_node.node_kind == "direct_response"
    assert any(
        binding.source_kind == "request_file" and binding.source_ref == str(inspected_file)
        for binding in read_node.input_bindings
    )
    assert any(
        binding.source_kind == "upstream_output" and binding.source_ref == "read_file_0"
        for binding in respond_node.input_bindings
    )
    assert execution_plan_requires_default_provider(plan) is True


def test_file_inspection_prompt_reads_file_contents_before_direct_response(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    inspected_file = tmp_path / "Folder With Spaces" / "notes file.txt"
    inspected_file.parent.mkdir(parents=True, exist_ok=True)
    inspected_file.write_text("important runtime note\n", encoding="utf-8")
    solve_request = load_solve_request(prompt=f"Inspect {inspected_file} and summarize it.")
    task, plan = compile_execution_plan_from_solve_request(
        solve_request,
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    provider = CapturingProvider(response_text="inspection-complete")

    result = TaskRuntime(runtime, shell, provider).run_task(
        task,
        0,
        request_id=solve_request.request_id,
        plan=plan,
    )

    assert result.hard_invalid is False
    assert result.artifact == "inspection-complete"
    assert provider.prompts
    assert "important runtime note" in provider.prompts[0]
    assert str(inspected_file) in provider.prompts[0]


def test_file_inspection_prompt_accepts_filesystem_family_scope_and_executes_read_tool(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    inspected_file = tmp_path / "notes.txt"
    inspected_file.write_text("important runtime note\n", encoding="utf-8")
    solve_request = load_solve_request(prompt="Inspect the supplied file and summarize it.")
    solve_request.file_paths = [str(inspected_file)]
    solve_request.allowed_tool_categories = ["filesystem/*"]

    task, plan = compile_execution_plan_from_solve_request(
        solve_request,
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    provider = CapturingProvider(response_text="inspection-complete")

    result = TaskRuntime(runtime, shell, provider).run_task(
        task,
        0,
        request_id=solve_request.request_id,
        plan=plan,
    )

    read_node = next(node for node in plan.nodes if node.node_id == "read_file_0")
    assert task.allowed_tool_categories == ["filesystem/*"]
    assert any(
        binding.source_kind == "request_file" and binding.source_ref == str(inspected_file)
        for binding in read_node.input_bindings
    )
    assert result.hard_invalid is False
    assert result.artifact == "inspection-complete"
    assert "important runtime note" in provider.prompts[0]


def test_compile_execution_plan_from_solve_request_builds_repo_patch_template(tmp_path):
    target_file = tmp_path / "Folder With Spaces" / "app file.py"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("value = 'foo'\n", encoding="utf-8")
    solve_request = load_solve_request(prompt=f"Update {target_file} to replace foo with bar.")

    task, plan = compile_execution_plan_from_solve_request(
        solve_request,
        seed=5,
        runtime_hash="runtime-hash",
        runtime_dir=str(tmp_path / "runtime"),
    )

    assert task.task_type == "bounded_repo_patch"
    patch_node = next(node for node in plan.nodes if node.node_id == "apply_patch")
    assert patch_node.node_kind == "repo_patch"
    assert execution_plan_requires_default_provider(plan) is True


def test_compile_execution_plan_from_solve_request_builds_repo_patch_template_for_new_absolute_host_target(tmp_path):
    target_file = tmp_path / "Folder With Spaces" / "new file.py"
    solve_request = load_solve_request(prompt=f"Update {target_file} to add a hello world implementation.")

    task, plan = compile_execution_plan_from_solve_request(
        solve_request,
        seed=5,
        runtime_hash="runtime-hash",
        runtime_dir=str(tmp_path / "runtime"),
    )

    assert task.task_type == "bounded_repo_patch"
    assert plan.file_ref_specs[0].source_path == str(target_file)
    assert plan.file_ref_specs[0].runtime_path == str(target_file.resolve())
    assert plan.file_ref_specs[0].host_path == str(target_file.resolve())


def test_repo_patch_prompt_updates_target_file(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    target_file = tmp_path / "Folder With Spaces" / "app file.py"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("value = 'foo'\n", encoding="utf-8")
    solve_request = load_solve_request(prompt=f"Update {target_file} to replace foo with bar.")
    task, plan = compile_execution_plan_from_solve_request(
        solve_request,
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    provider = CapturingProvider(
        response_text=json.dumps(
            {
                "summary": "Updated the target file.",
                "files": [
                    {
                        "path": str(target_file),
                        "updated_content": "value = 'bar'\n",
                    }
                ],
            }
        )
    )

    result = TaskRuntime(runtime, shell, provider).run_task(
        task,
        0,
        request_id=solve_request.request_id,
        plan=plan,
    )

    assert result.hard_invalid is False
    assert target_file.read_text(encoding="utf-8") == "value = 'bar'\n"
    assert result.artifact["applied"] is True
    assert result.artifact["updated_files"][0]["path"] == str(target_file)
    assert "-value = 'foo'" in result.artifact["updated_files"][0]["diff"]
    assert "+value = 'bar'" in result.artifact["updated_files"][0]["diff"]


def test_repo_patch_prompt_can_create_new_absolute_host_target(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    target_file = tmp_path / "Folder With Spaces" / "new file.py"
    solve_request = load_solve_request(prompt=f"Update {target_file} to add hello world code.")
    task, plan = compile_execution_plan_from_solve_request(
        solve_request,
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    provider = CapturingProvider(
        response_text=json.dumps(
            {
                "summary": "Created the target file.",
                "files": [
                    {
                        "path": str(target_file.resolve()),
                        "updated_content": "print('hello world')\n",
                    }
                ],
            }
        )
    )

    result = TaskRuntime(runtime, shell, provider).run_task(
        task,
        0,
        request_id=solve_request.request_id,
        plan=plan,
    )

    assert result.hard_invalid is False
    assert target_file.read_text(encoding="utf-8") == "print('hello world')\n"
    assert result.artifact["applied"] is True


def test_repo_patch_publishes_prewrite_filesystem_checkpoint_and_completion_receipts(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    target_file = tmp_path / "app.py"
    target_file.write_text("value = 'foo'\n", encoding="utf-8")
    solve_request = load_solve_request(prompt=f"Update {target_file} to replace foo with bar.")
    task, plan = compile_execution_plan_from_solve_request(
        solve_request,
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    provider = CapturingProvider(
        response_text=json.dumps(
            {
                "summary": "Updated the target file.",
                "files": [
                    {
                        "path": str(target_file),
                        "updated_content": "value = 'bar'\n",
                    }
                ],
            }
        )
    )

    result = TaskRuntime(runtime, shell, provider).run_task(
        task,
        0,
        request_id=solve_request.request_id,
        plan=plan,
    )
    launch_envelope = _checkpoint_for_boundary(shell, solve_request.request_id, "before_filesystem_write")
    completion_envelope = _checkpoint_for_boundary(shell, solve_request.request_id, "after_filesystem_write")
    launch_receipts = [
        receipt
        for receipt in launch_envelope.side_effect_ledger["receipts"]
        if receipt.action_kind == "filesystem_write"
    ]
    completion_receipts = [
        receipt
        for receipt in completion_envelope.side_effect_ledger["receipts"]
        if receipt.action_kind == "filesystem_write"
    ]

    assert result.hard_invalid is False
    assert target_file.read_text(encoding="utf-8") == "value = 'bar'\n"
    assert [receipt.status for receipt in launch_receipts] == ["launched"]
    assert {receipt.status for receipt in completion_receipts} == {"launched", "completed"}
    assert completion_receipts[-1].result_ref["output"]["applied"] is True


def test_resume_from_before_filesystem_write_reuses_cached_patch_without_provider_reissue(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    target_file = tmp_path / "app.py"
    target_file.write_text("value = 'foo'\n", encoding="utf-8")
    solve_request = load_solve_request(prompt=f"Update {target_file} to replace foo with bar.")
    task, plan = compile_execution_plan_from_solve_request(
        solve_request,
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    first_provider = CapturingProvider(
        response_text=json.dumps(
            {
                "summary": "Updated the target file.",
                "files": [
                    {
                        "path": str(target_file),
                        "updated_content": "value = 'bar'\n",
                    }
                ],
            }
        )
    )
    first_run = TaskRuntime(runtime, shell, first_provider).run_task(
        task,
        0,
        request_id=solve_request.request_id,
        plan=plan,
    )
    launch_envelope = _checkpoint_for_boundary(shell, solve_request.request_id, "before_filesystem_write")
    target_file.write_text("value = 'foo'\n", encoding="utf-8")
    resume_provider = CapturingProvider(response_text="should-not-be-used")

    resumed_run = TaskRuntime(runtime, shell, resume_provider).resume_from_checkpoint(launch_envelope)

    assert first_run.hard_invalid is False
    assert resumed_run.hard_invalid is False
    assert not resume_provider.prompts
    assert target_file.read_text(encoding="utf-8") == "value = 'bar'\n"
    assert resumed_run.artifact["applied"] is True
    assert any(
        row.get("event") == "side_effect_reconciled"
        and row.get("reconciliation_status") == "filesystem_prewrite_state_intact"
        for row in resumed_run.trace_rows()
    )


def test_resume_strict_fails_closed_on_ambiguous_filesystem_write_launch_without_provider_reissue(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    target_file = tmp_path / "app.py"
    target_file.write_text("value = 'foo'\n", encoding="utf-8")
    solve_request = load_solve_request(prompt=f"Update {target_file} to replace foo with bar.")
    task, plan = compile_execution_plan_from_solve_request(
        solve_request,
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    first_provider = CapturingProvider(
        response_text=json.dumps(
            {
                "summary": "Updated the target file.",
                "files": [
                    {
                        "path": str(target_file),
                        "updated_content": "value = 'bar'\n",
                    }
                ],
            }
        )
    )
    first_run = TaskRuntime(runtime, shell, first_provider).run_task(
        task,
        0,
        request_id=solve_request.request_id,
        plan=plan,
    )
    launch_envelope = _checkpoint_for_boundary(shell, solve_request.request_id, "before_filesystem_write")
    target_file.write_text("value = 'partial'\n", encoding="utf-8")
    resume_provider = CapturingProvider(response_text="should-not-be-used")

    resumed_run = TaskRuntime(runtime, shell, resume_provider).resume_from_checkpoint(
        launch_envelope,
        reconciliation_policy="strict",
    )

    assert first_run.hard_invalid is False
    assert resumed_run.hard_invalid is True
    assert resumed_run.failure_kind == "receipt_reconciliation_failed"
    assert not resume_provider.prompts
    assert target_file.read_text(encoding="utf-8") == "value = 'partial'\n"


def test_request_file_relative_paths_resolve_against_runtime_workspace_not_process_cwd(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    runtime.deployment_contract.runtime_isolation_policy.workspace_root = "repo"
    workspace_file = shell.workspace / "repo" / "app.py"
    workspace_file.parent.mkdir(parents=True, exist_ok=True)
    workspace_file.write_text("workspace-version\n", encoding="utf-8")
    other_cwd = tmp_path / "other-cwd"
    other_file = other_cwd / "app.py"
    other_file.parent.mkdir(parents=True, exist_ok=True)
    other_file.write_text("cwd-version\n", encoding="utf-8")
    monkeypatch.chdir(other_cwd)

    solve_request = load_solve_request(prompt="Inspect the repo file and summarize it.")
    solve_request.file_paths = ["app.py"]
    solve_request.request_file_refs = []
    task, plan = compile_execution_plan_from_solve_request(
        solve_request,
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    provider = CapturingProvider(response_text="inspection-complete")

    result = TaskRuntime(runtime, shell, provider).run_task(
        task,
        0,
        request_id=solve_request.request_id,
        plan=plan,
    )

    assert result.hard_invalid is False
    assert "workspace-version" in provider.prompts[0]
    assert "cwd-version" not in provider.prompts[0]


def test_repo_patch_prompt_rejects_relative_path_escape_from_runtime_workspace(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    repo_dir = shell.workspace / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    target_file = repo_dir / "app.py"
    target_file.write_text("value = 'foo'\n", encoding="utf-8")
    solve_request = load_solve_request(prompt="Update the target file to replace foo with bar.")
    solve_request.file_paths = ["repo/app.py"]
    task, plan = compile_execution_plan_from_solve_request(
        solve_request,
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    provider = CapturingProvider(
        response_text=json.dumps(
            {
                "summary": "Attempted to escape the workspace.",
                "files": [
                    {
                        "path": "../escape.py",
                        "updated_content": "value = 'bad'\n",
                    }
                ],
            }
        )
    )

    result = TaskRuntime(runtime, shell, provider).run_task(
        task,
        0,
        request_id=solve_request.request_id,
        plan=plan,
    )

    assert result.hard_invalid is True
    assert target_file.read_text(encoding="utf-8") == "value = 'foo'\n"
    assert not (shell.workspace.parent / "escape.py").exists()


def test_compile_execution_plan_from_solve_request_builds_service_action_template(tmp_path):
    solve_request = load_solve_request(prompt="GET https://service.example.test/status")
    solve_request.allowed_tool_categories = ["service/*"]

    task, plan = compile_execution_plan_from_solve_request(
        solve_request,
        seed=1,
        runtime_hash="runtime-hash",
        runtime_dir=str(tmp_path / "runtime"),
    )

    requirements = execution_plan_requirements(plan)
    assert task.task_type == "bounded_service_action"
    assert task.allowed_tool_categories == ["service/*"]
    service_node = next(node for node in plan.nodes if node.node_id == "service_call")
    assert service_node.node_kind == "service_action"
    assert service_node.metadata["service_transport"] == "http"
    assert plan.plan_constants["service_call.service_transport"] == "http"
    assert requirements.required_tool_categories == ["service/http"]
    assert requirements.required_network_transports == ["http"]
    assert execution_plan_requires_default_provider(plan) is False


def test_compile_execution_plan_from_solve_request_rejects_service_action_when_service_tools_are_disallowed(tmp_path):
    solve_request = load_solve_request(prompt="GET https://service.example.test/status")
    solve_request.allowed_tool_categories = ["filesystem/read"]

    with pytest.raises(PromptAdaptationError, match="service/\\* allowed_tool_categories capability"):
        compile_execution_plan_from_solve_request(
            solve_request,
            seed=1,
            runtime_hash="runtime-hash",
            runtime_dir=str(tmp_path / "runtime"),
        )


def test_capability_scope_helpers_expand_family_scopes():
    assert capability_scope_allows(["filesystem/*"], "filesystem/read") is True
    assert capability_scope_allows(["filesystem/*"], "filesystem/patch") is True
    assert capability_scope_allows(["service/*"], "service/http") is True
    assert capability_scope_service_transports(["service/*"]) == ["http"]

    compatibility = service_action_transport_compatibility(
        url="https://service.example.test/status",
        allowed_tool_categories=["service/*"],
    )

    assert compatibility.transport == "http"
    assert compatibility.allowed_schemes == ("http", "https")


def test_compile_execution_plan_stamps_explicit_capability_intent_metadata(tmp_path):
    target_file = tmp_path / "app.py"
    target_file.write_text("value = 'foo'\n", encoding="utf-8")
    repo_request = load_solve_request(prompt=f"Update {target_file} to replace foo with bar.")
    _, repo_plan = compile_execution_plan_from_solve_request(
        repo_request,
        seed=5,
        runtime_hash="runtime-hash",
        runtime_dir=str(tmp_path / "runtime"),
    )
    service_request = load_solve_request(prompt="GET https://service.example.test/status")
    _, service_plan = compile_execution_plan_from_solve_request(
        service_request,
        seed=6,
        runtime_hash="runtime-hash",
        runtime_dir=str(tmp_path / "runtime"),
    )

    patch_node = next(node for node in repo_plan.nodes if node.node_id == "apply_patch")
    service_node = next(node for node in service_plan.nodes if node.node_id == "service_call")

    assert patch_node.metadata["capability_intent"]["requires_default_provider"] is True
    assert patch_node.metadata["capability_intent"]["requires_filesystem_write"] is True
    assert "filesystem/patch" in patch_node.metadata["capability_intent"]["required_tool_categories"]
    assert service_node.metadata["capability_intent"]["requires_network_access"] is True
    assert service_node.metadata["capability_intent"]["network_transports"] == ["http"]
    assert any(
        category.startswith("service/")
        for category in service_node.metadata["capability_intent"]["required_tool_categories"]
    )
    requirements = execution_plan_requirements(service_plan)
    assert requirements.requires_network_access is True
    assert requirements.required_network_transports == ["http"]
    assert requirements.network_transport_nodes == {"http": ["service_call"]}


def test_compile_execution_plan_from_solve_request_rejects_non_http_service_action_url(tmp_path):
    solve_request = load_solve_request(prompt="GET file:///tmp/secret.txt")

    with pytest.raises(PromptAdaptationError, match="only permits URL schemes"):
        compile_execution_plan_from_solve_request(
            solve_request,
            seed=1,
            runtime_hash="runtime-hash",
            runtime_dir=str(tmp_path / "runtime"),
        )


def test_compile_execution_plan_from_task_rejects_service_action_urls_outside_http_transport(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    task = _make_service_action_task(
        "service.invalid-scheme",
        url="file:///tmp/secret.txt",
    )

    with pytest.raises(ValueError, match="only permits URL schemes"):
        compile_execution_plan_from_task(
            task,
            request_id="service.invalid-scheme.request",
            seed=0,
            runtime_hash=runtime.runtime_hash,
            runtime_dir=str(runtime.runtime_dir),
        )


def test_service_action_node_executes_bounded_http_request(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    runtime.deployment_contract.network_policy = "open"
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    solve_request = load_solve_request(prompt="GET https://service.example.test/status")
    solve_request.allowed_tool_categories = ["service/*"]
    task, plan = compile_execution_plan_from_solve_request(
        solve_request,
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    captured: dict[str, object] = {}

    class _FakeResponse:
        def __init__(self):
            self.status = 200
            self.headers = {"Content-Type": "application/json"}

        def read(self):
            return b'{"ok": true}'

        def getcode(self):
            return self.status

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr("agintor.runner.urllib_request.urlopen", fake_urlopen)

    result = TaskRuntime(runtime, shell, ReplayProvider([])).run_task(
        task,
        0,
        request_id=solve_request.request_id,
        plan=plan,
    )

    assert result.hard_invalid is False
    assert captured == {
        "url": "https://service.example.test/status",
        "method": "GET",
        "timeout": 10.0,
    }
    assert result.artifact["status_code"] == 200
    assert result.artifact["body"] == {"ok": True}
    assert result.provider_usage.get("calls", 0) == 0


def test_service_action_executor_rejects_non_http_scheme_before_dispatch(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    runtime.deployment_contract.network_policy = "open"
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_service_action_task("service.executor.scheme-guard")
    plan = compile_execution_plan_from_task(
        task,
        request_id="service.executor.scheme-guard.request",
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    runner = TaskRuntime(runtime, shell, ReplayProvider([]))
    state = RuntimeState(
        request_id=plan.request_id,
        plan_id=plan.plan_id,
        execution_state="running",
        visible_tool_names=sorted(shell.tool_registry.tools),
    )
    context = PolicyContext(
        runtime_dir=runtime.runtime_dir,
        shell=shell,
        task=task,
        request_id=plan.request_id,
        plan=plan,
        trace_context=plan.trace_context,
        provider=ReplayProvider([]),
        seed=0,
        state=state,
        budget=RuntimeBudget(),
        trace=[],
        objective=plan.objective,
        runtime_backend="local",
    )
    operation = next(node for node in plan.nodes if node.node_id == "service_call")
    dispatch_calls = {"count": 0}

    def fail_if_called(*args, **kwargs):
        dispatch_calls["count"] += 1
        raise AssertionError("urlopen should not be called for incompatible service_action schemes")

    monkeypatch.setattr("agintor.runner.urllib_request.urlopen", fail_if_called)

    with pytest.raises(HardInvalidation, match="only permits URL schemes"):
        runner._execute_service_action_node(
            context,
            operation,
            {
                "url": "file:///tmp/secret.txt",
                "method": "GET",
                "headers": {},
                "body": None,
                "timeout_s": 10.0,
                "service_transport": "http",
            },
            plan.trace_context,
        )

    assert dispatch_calls["count"] == 0


def test_service_action_publishes_launch_receipt_before_dispatch_and_completion_after_success(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    runtime.deployment_contract.network_policy = "open"
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_service_action_task("service.receipts")
    plan = compile_execution_plan_from_task(
        task,
        request_id="service.receipts.request",
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )

    class _FakeResponse:
        def __init__(self):
            self.status = 200
            self.headers = {"Content-Type": "application/json"}

        def read(self):
            return b'{"ok": true}'

        def getcode(self):
            return self.status

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("agintor.runner.urllib_request.urlopen", lambda request, timeout=0: _FakeResponse())

    result = TaskRuntime(runtime, shell, ReplayProvider([])).run_task(
        task,
        0,
        request_id=plan.request_id,
        plan=plan,
    )
    launch_envelope = _checkpoint_for_boundary(shell, plan.request_id, "after_service_action_launch")
    completion_envelope = _checkpoint_for_boundary(
        shell,
        plan.request_id,
        "after_service_action_completion",
    )
    launch_receipts = [
        receipt
        for receipt in launch_envelope.side_effect_ledger["receipts"]
        if receipt.action_kind == "service_action"
    ]
    completion_receipts = [
        receipt
        for receipt in completion_envelope.side_effect_ledger["receipts"]
        if receipt.action_kind == "service_action"
    ]

    assert result.hard_invalid is False
    assert [receipt.status for receipt in launch_receipts] == ["launched"]
    assert {receipt.status for receipt in completion_receipts} == {"launched", "completed"}
    assert any(receipt.result_ref.get("output", {}).get("status_code") == 200 for receipt in completion_receipts)


def test_resume_strict_fails_closed_on_unreconciled_service_action_launch_without_reissue(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    runtime.deployment_contract.network_policy = "open"
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    envelope = _pending_service_action_launch_envelope(
        runtime,
        shell,
        task_id="resume.strict-service-action-launch",
    )
    dispatch_calls = {"count": 0}

    def fail_if_called(*args, **kwargs):
        dispatch_calls["count"] += 1
        raise AssertionError("service action should not be reissued during strict resume")

    monkeypatch.setattr("agintor.runner.urllib_request.urlopen", fail_if_called)

    resumed = TaskRuntime(runtime, shell, ReplayProvider([])).resume_from_checkpoint(
        envelope,
        reconciliation_policy="strict",
    )

    assert resumed.hard_invalid is True
    assert resumed.failure_kind == "receipt_reconciliation_failed"
    assert dispatch_calls["count"] == 0


def test_resume_best_effort_blocks_unreconciled_service_action_launch_without_reissue(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    runtime.deployment_contract.network_policy = "open"
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    envelope = _pending_service_action_launch_envelope(
        runtime,
        shell,
        task_id="resume.best-effort-service-action-launch",
    )
    dispatch_calls = {"count": 0}

    def fail_if_called(*args, **kwargs):
        dispatch_calls["count"] += 1
        raise AssertionError("service action should not be reissued during best-effort resume")

    monkeypatch.setattr("agintor.runner.urllib_request.urlopen", fail_if_called)

    resumed = TaskRuntime(runtime, shell, ReplayProvider([])).resume_from_checkpoint(
        envelope,
        reconciliation_policy="best_effort",
    )

    assert resumed.hard_invalid is False
    assert resumed.artifact["error"] == "recovery_blocked"
    assert resumed.artifact["node_id"] == "service_call"
    assert dispatch_calls["count"] == 0


def test_runtime_bundle_includes_run_store_module(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")

    bundled_run_store = runtime_dir / "runtime_sdk" / "agintor_runtime" / "run_store.py"
    bundled_state_store = runtime_dir / "runtime_sdk" / "agintor_runtime" / "state_store.py"

    assert bundled_run_store.exists()
    assert bundled_state_store.exists()


def test_run_store_canonicalizes_relative_workspace_run_roots_and_checkpoint_refs(tmp_path, monkeypatch):
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    store = RunStore("relative-workspace")
    manifest = store.create_run(
        request_id="req.relative",
        evaluation_unit_id="req.relative",
        request_mode="user_request",
        runtime_backend="local",
    )
    envelope = CheckpointEnvelope(
        checkpoint_id="checkpoint.req.relative.0001",
        runtime_contract_version=RUNTIME_CONTRACT_VERSION,
        runtime_hash="runtime-hash",
        run_id=manifest.run_id,
        run_root=manifest.run_root,
        request_id="req.relative",
        plan_id="plan.relative",
        task_id="task.relative",
        seed=0,
    )

    checkpoint_ref = store.write_checkpoint(envelope)
    reloaded_manifest = store.load_run_manifest(manifest.run_id)

    assert store.workspace == (workdir / "relative-workspace").resolve()
    assert Path(reloaded_manifest.run_root).is_absolute()
    assert reloaded_manifest.run_root == str((store.workspace / "runs" / manifest.run_id).resolve())
    assert checkpoint_ref.ref == str(Path(checkpoint_ref.ref).resolve())
    assert store.latest_checkpoint_ref(manifest.run_id) == checkpoint_ref.ref
    indexed_latest = state_store.open_state_store(reloaded_manifest.run_root).latest_usable_checkpoint(
        run_id=manifest.run_id
    )
    assert indexed_latest is not None
    assert indexed_latest["checkpoint_id"] == envelope.checkpoint_id


def test_fixed_shell_checkpoint_lookup_can_resume_from_external_store_with_container_refs(tmp_path):
    store_shell = FixedShell(tmp_path / "store-workspace", artifact_mode=ArtifactMode.ALWAYS)
    envelope_one = CheckpointEnvelope(
        checkpoint_id="checkpoint.req.0001",
        runtime_contract_version=RUNTIME_CONTRACT_VERSION,
        runtime_hash="runtime-hash",
        request_id="req",
        plan_id="plan",
        task_id="task",
        seed=1,
        sequence_no=1,
        boundary="after_provider_completion",
        created_at=1.0,
    )
    envelope_two = envelope_one.model_copy(
        update={
            "checkpoint_id": "checkpoint.req.0002",
            "sequence_no": 2,
            "boundary": "after_branch_completion",
            "created_at": 2.0,
        }
    )

    ref_one = store_shell.save_checkpoint_envelope(envelope_one)
    ref_two = store_shell.save_checkpoint_envelope(envelope_two)

    request_dir = tmp_path / "store-workspace" / "checkpoints" / "req"
    index_path = request_dir / "index.json"
    latest_path = request_dir / "LATEST.json"
    index_rows = json.loads(index_path.read_text(encoding="utf-8"))
    for row in index_rows:
        row["checkpoint_ref"] = f"/mnt/workspace/seed_1/checkpoints/req/{row['checkpoint_id']}.json"
    index_path.write_text(json.dumps(index_rows, indent=2, sort_keys=True), encoding="utf-8")
    latest_payload = json.loads(latest_path.read_text(encoding="utf-8"))
    latest_payload["checkpoint_ref"] = f"/mnt/workspace/seed_1/checkpoints/req/{latest_payload['checkpoint_id']}.json"
    latest_path.write_text(json.dumps(latest_payload, indent=2, sort_keys=True), encoding="utf-8")

    resume_shell = FixedShell(tmp_path / "resume-workspace", artifact_mode=ArtifactMode.ALWAYS)
    resume_shell.configure_resume_checkpoint_store(str((tmp_path / "store-workspace" / "checkpoints").resolve()))

    assert ref_one.sequence_no == 1
    assert ref_two.sequence_no == 2
    assert resume_shell.latest_checkpoint_ref("req") == ref_two.ref
    restored = resume_shell.load_checkpoint_envelope(request_id="req")
    assert restored.checkpoint_id == "checkpoint.req.0002"


def test_fixed_shell_no_longer_exposes_save_checkpoints():
    assert not hasattr(FixedShell, "save_checkpoints")


def test_batch_evaluation_unit_key_scopes_transfer_runs_by_episode_and_seed():
    invocations = [
        RuntimeTaskInvocation(
            request_id="benchmark.episode.step1.seed_1",
            seed=1,
            task=BenchmarkTask(
                task_id="episode.step1",
                family="top",
                prompt="Step one",
                task_type="structured_ops",
                operations=[],
                expected={},
                transfer_scored=True,
                episode_id="episode-alpha",
                episode_order=0,
            ),
        ),
        RuntimeTaskInvocation(
            request_id="benchmark.episode.step2.seed_1",
            seed=1,
            task=BenchmarkTask(
                task_id="episode.step2",
                family="top",
                prompt="Step two",
                task_type="structured_ops",
                operations=[],
                expected={},
                transfer_scored=True,
                episode_id="episode-alpha",
                episode_order=1,
            ),
        ),
        RuntimeTaskInvocation(
            request_id="benchmark.episode.step1.seed_2",
            seed=2,
            task=BenchmarkTask(
                task_id="episode.step1",
                family="top",
                prompt="Step one",
                task_type="structured_ops",
                operations=[],
                expected={},
                transfer_scored=True,
                episode_id="episode-alpha",
                episode_order=0,
            ),
        ),
    ]

    assert batch_evaluation_unit_key(invocations[0]) == batch_evaluation_unit_key(invocations[1])
    assert batch_evaluation_unit_key(invocations[0]) != batch_evaluation_unit_key(invocations[2])


def test_batch_evaluation_unit_key_keeps_benchmark_duplicates_separate_even_if_transport_is_shared():
    task = _make_direct_response_task("duplicate.transport")
    first = RuntimeTaskInvocation(
        request_id="benchmark.duplicate.transport.seed_1",
        evaluation_unit_id="shared.transport.unit",
        seed=1,
        task=task,
    )
    second = RuntimeTaskInvocation(
        request_id="benchmark.duplicate.transport.seed_1.dup_01",
        evaluation_unit_id="shared.transport.unit",
        seed=1,
        task=task,
    )

    assert first.episode_kind is None
    assert second.episode_kind is None
    assert batch_evaluation_unit_key(first) == first.request_id
    assert batch_evaluation_unit_key(second) == second.request_id
    assert batch_evaluation_unit_key(first) != batch_evaluation_unit_key(second)


def test_compile_transfer_plan_fills_missing_episode_step_from_task_order():
    task = BenchmarkTask(
        task_id="episode.partial-trace.step3",
        family="top",
        prompt="Step three",
        task_type="structured_ops",
        operations=[],
        expected={},
        transfer_scored=True,
        episode_id="episode-partial-trace",
        episode_order=3,
    )

    plan = compile_execution_plan_from_task(
        task,
        request_id="benchmark.episode.partial-trace.step3.seed_5",
        seed=5,
        runtime_hash="runtime-hash",
        runtime_dir="runtime-dir",
        trace_context=OpenAITraceContext(session_id="session.partial"),
    )

    assert plan.trace_context.episode_kind == "transfer_episode"
    assert plan.trace_context.episode_step_index == 3


def test_state_store_indexes_execution_unit_members_from_request_envelope(tmp_path):
    store = RunStore(tmp_path / "runs")
    manifest = store.create_run(
        request_id="episode.ledger.seed_9",
        evaluation_unit_id="episode.ledger.seed_9",
        request_mode="batch",
        runtime_backend="local",
        runtime_contract_version=RUNTIME_CONTRACT_VERSION,
    )
    first_task = BenchmarkTask(
        task_id="episode.ledger.step1",
        family="top",
        prompt="Step one",
        task_type="structured_ops",
        operations=[],
        expected={},
        transfer_scored=True,
        episode_id="episode-ledger",
        episode_order=0,
    )
    second_task = BenchmarkTask(
        task_id="episode.ledger.step2",
        family="top",
        prompt="Step two",
        task_type="structured_ops",
        operations=[],
        expected={},
        transfer_scored=True,
        episode_id="episode-ledger",
        episode_order=1,
    )
    invocations = [
        RuntimeTaskInvocation(
            request_id="benchmark.episode.ledger.step1.seed_9",
            evaluation_unit_id=manifest.evaluation_unit_id,
            episode_kind="transfer_episode",
            episode_step_index=0,
            runtime_backend="local",
            seed=9,
            task=first_task,
            trace_context=OpenAITraceContext(
                request_id="benchmark.episode.ledger.step1.seed_9",
                evaluation_unit_id=manifest.evaluation_unit_id,
                episode_kind="transfer_episode",
                episode_step_index=0,
            ),
        ),
        RuntimeTaskInvocation(
            request_id="benchmark.episode.ledger.step2.seed_9",
            evaluation_unit_id=manifest.evaluation_unit_id,
            episode_kind="transfer_episode",
            episode_step_index=1,
            runtime_backend="local",
            seed=9,
            task=second_task,
            trace_context=OpenAITraceContext(
                request_id="benchmark.episode.ledger.step2.seed_9",
                evaluation_unit_id=manifest.evaluation_unit_id,
                episode_kind="transfer_episode",
                episode_step_index=1,
            ),
        ),
    ]
    envelope = ExecutionUnitRequestEnvelope(
        request_kind="runtime_task_invocation_group",
        request_mode="batch",
        request_id=manifest.request_id,
        evaluation_unit_id=manifest.evaluation_unit_id,
        payload=(invocations[0]).model_dump(),
        member_invocations=invocations,
    )

    store.write_request_bundle(manifest, request_envelope=(envelope).model_dump())

    with state_store.open_state_store(manifest.run_root)._connection() as conn:
        episodes = conn.execute(
            "SELECT request_id, episode_kind, episode_step_index, task_id FROM episodes ORDER BY episode_step_index"
        ).fetchall()
        tasks = conn.execute("SELECT task_id, request_id, evaluation_unit_id FROM tasks ORDER BY task_id").fetchall()

    assert [dict(row) for row in episodes] == [
        {
            "request_id": "benchmark.episode.ledger.step1.seed_9",
            "episode_kind": "transfer_episode",
            "episode_step_index": 0,
            "task_id": "episode.ledger.step1",
        },
        {
            "request_id": "benchmark.episode.ledger.step2.seed_9",
            "episode_kind": "transfer_episode",
            "episode_step_index": 1,
            "task_id": "episode.ledger.step2",
        },
    ]
    assert {row["task_id"] for row in tasks} == {"episode.ledger.step1", "episode.ledger.step2"}
    assert {row["evaluation_unit_id"] for row in tasks} == {manifest.evaluation_unit_id}


def test_trace_cursor_links_persisted_model_call_ids(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_direct_response_task("trace.call.cursor")
    plan = compile_execution_plan_from_task(
        task,
        request_id="benchmark.trace.call.cursor.seed_0",
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    context = PolicyContext(
        runtime_dir=runtime.runtime_dir,
        shell=shell,
        task=task,
        request_id=plan.request_id,
        plan=plan,
        trace_context=plan.trace_context,
        provider=LocalDeterministicProvider(),
        seed=0,
        state=RuntimeState(request_id=plan.request_id, plan_id=plan.plan_id),
        budget=RuntimeBudget(),
        trace=[],
        objective=plan.objective,
    )
    response = ModelResponse(
        text="ok",
        model_name="hosted/small",
        trace_call_id="20260424T000000Z__pid1__call0001__user_request__create__model",
    )

    context.consume_model_response(response, purpose="user_request")
    cursor = TaskRuntime(
        runtime,
        shell,
        LocalDeterministicProvider(),
    )._build_trace_cursor_snapshot(context, task, 0)

    assert cursor.linked_call_ids == ["20260424T000000Z__pid1__call0001__user_request__create__model"]


def test_default_trace_session_is_persisted_to_checkpoint_cursor(tmp_path, monkeypatch):
    monkeypatch.setenv("AGINTOR_OPENAI_TRACE_SESSION_ID", "session.default-checkpoint")
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_direct_response_task("trace.default-session.cursor")

    result = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([{"text": "hello with default trace session", "model_name": "replay/small"}]),
    ).run_task(task, 0)
    envelope = shell.load_checkpoint_envelope(checkpoint_ref=result.checkpoint_ref)

    assert result.trace_context.session_id == "session.default-checkpoint"
    assert envelope.plan_snapshot["trace_context"]["session_id"] == "session.default-checkpoint"
    assert envelope.trace_cursor.last_session_id == "session.default-checkpoint"
    assert envelope.trace_cursor.last_runtime_task_key == f"{task.task_id}|seed_0|{runtime.runtime_hash}|{result.request_id}"
    assert envelope.trace_cursor.materialization_state_ref == (
        "openai_api_traces/sessions/session.default-checkpoint/materialization_state.json"
    )


def test_trace_cursor_materialization_ref_uses_sanitized_session_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AGINTOR_OPENAI_TRACE_SESSION_ID", "session default/checkpoint")
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_direct_response_task("trace.sanitized-session.cursor")

    result = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([{"text": "hello with unsafe trace session", "model_name": "replay/small"}]),
    ).run_task(task, 0)
    envelope = shell.load_checkpoint_envelope(checkpoint_ref=result.checkpoint_ref)

    assert result.trace_context.session_id == "session default/checkpoint"
    assert envelope.trace_cursor.last_session_id == "session default/checkpoint"
    assert envelope.trace_cursor.materialization_state_ref == (
        "openai_api_traces/sessions/session_default_checkpoint/materialization_state.json"
    )


def test_resume_from_checkpoint_restores_budget_state_and_reuses_completed_receipts(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    first_provider = ReplayProvider(
        [
            {
                "text": "hello from ws2",
                "model_name": "replay/small",
                "input_tokens": 5,
                "output_tokens": 4,
                "token_estimate": 9,
                "latency_s": 0.25,
                "dollar_cost": 0.01,
            }
        ]
    )
    task = _make_direct_response_task("resume.direct-response")
    first_runner = TaskRuntime(runtime, shell, first_provider)
    first_run = first_runner.run_task(task, 0)

    assert first_run.model_calls == 1
    assert first_run.checkpoint_ref
    envelope = shell.load_checkpoint_envelope(checkpoint_ref=first_run.checkpoint_ref)
    assert envelope.runtime_state_snapshot.budget_totals.calls == 1

    resume_provider = ReplayProvider([])
    resume_runner = TaskRuntime(runtime, shell, resume_provider)
    resumed_run = resume_runner.resume_from_checkpoint(envelope)

    assert resumed_run.hard_invalid is False
    assert resumed_run.model_calls == 1
    assert resumed_run.artifact == first_run.artifact


def test_provider_receipt_replay_ignores_resume_trace_provenance(tmp_path):
    task = _make_direct_response_task("receipt.trace-provenance")
    plan = compile_execution_plan_from_task(
        task,
        request_id="request.original",
        seed=0,
        runtime_hash="runtime.hash",
        runtime_dir="/mnt/runtime",
    )
    base_trace_context = plan.trace_context.model_copy(update={"op_id": "respond", "run_node_id": "node.original"})

    def make_context(
        trace_context: OpenAITraceContext,
        provider: ReplayProvider,
        receipts: list[dict[str, object]] | None = None,
    ) -> PolicyContext:
        return PolicyContext(
            runtime_dir=Path(trace_context.runtime_dir or tmp_path),
            shell=SimpleNamespace(),
            task=task,
            request_id=trace_context.request_id or plan.request_id,
            plan=plan,
            trace_context=trace_context,
            provider=provider,
            seed=0,
            state=RuntimeState(
                request_id=trace_context.request_id or plan.request_id,
                plan_id=plan.plan_id,
                side_effect_receipts=list(receipts or []),
            ),
            budget=RuntimeBudget(),
            trace=[],
            objective=plan.objective,
            runtime_backend="docker",
        )

    first_provider = ReconcilingReplayProvider([{"text": "cached answer", "model_name": "replay/small"}])
    first_context = make_context(base_trace_context, first_provider)
    first_response = first_context.run_model_request(
        instructions="instructions",
        prompt="prompt",
        model_class="medium",
        purpose="direct_response",
        payload={"node": "respond"},
    )
    resumed_trace_context = base_trace_context.model_copy(
        update={
            "request_id": "request.resume",
            "runtime_dir": str((tmp_path / "host-runtime").resolve()),
            "session_id": "session.resume",
            "run_node_id": "node.resume",
        }
    )
    resume_provider = ReconcilingReplayProvider([])
    resume_context = make_context(
        resumed_trace_context,
        resume_provider,
        receipts=first_context.state.side_effect_receipts,
    )

    replayed_response = resume_context.run_model_request(
        instructions="instructions",
        prompt="prompt",
        model_class="medium",
        purpose="direct_response",
        payload={"node": "respond"},
    )

    assert first_response.text == "cached answer"
    assert first_provider.generate_calls == 1
    assert resume_provider.generate_calls == 0
    assert replayed_response.text == "cached answer"
    assert replayed_response.raw["replayed_from_receipt"].startswith("provider-completion.")


def test_direct_resume_uses_loaded_checkpoint_ref_as_source_for_followup_checkpoints(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    envelope = _pending_provider_launch_envelope(
        runtime,
        shell,
        task_id="resume.direct-loaded-source",
    ).model_copy(
        update={
            "checkpoint_id": "checkpoint.resume.direct-loaded-source.0001",
            "boundary": "before_provider_launch",
            "side_effect_ledger": {"receipts": []},
        },
        deep=True,
    )
    checkpoint_ref = shell.save_checkpoint_envelope(envelope).ref
    loaded_envelope = shell.load_checkpoint_envelope(checkpoint_ref=checkpoint_ref)

    resumed_run = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([{"text": "hello after direct resume", "model_name": "replay/small"}]),
    ).resume_from_checkpoint(loaded_envelope)
    resumed_launch_checkpoint = _checkpoint_for_boundary(
        shell,
        loaded_envelope.request_id,
        "after_provider_launch",
    )
    resumed_checkpoint = _checkpoint_for_boundary(
        shell,
        loaded_envelope.request_id,
        "after_provider_completion",
    )

    assert Path(loaded_envelope.source_checkpoint_ref).resolve() == Path(checkpoint_ref).resolve()
    assert Path(resumed_launch_checkpoint.source_checkpoint_ref).resolve() == Path(checkpoint_ref).resolve()
    assert Path(resumed_launch_checkpoint.working_state.selected_checkpoint_refs[0]).resolve() == Path(checkpoint_ref).resolve()
    assert Path(resumed_checkpoint.source_checkpoint_ref).resolve() == Path(checkpoint_ref).resolve()
    assert resumed_run.hard_invalid is False


def test_repeated_resume_uses_selected_checkpoint_ref_instead_of_prior_lineage(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    prior_lineage_ref = "checkpoint://older-resume-source"
    envelope = _pending_provider_launch_envelope(
        runtime,
        shell,
        task_id="resume.direct-selected-source",
    ).model_copy(
        update={
            "checkpoint_id": "checkpoint.resume.direct-selected-source.0001",
            "boundary": "before_provider_launch",
            "source_checkpoint_ref": prior_lineage_ref,
            "side_effect_ledger": {"receipts": []},
        },
        deep=True,
    )
    checkpoint_ref = shell.save_checkpoint_envelope(envelope).ref
    loaded_envelope = shell.load_checkpoint_envelope(checkpoint_ref=checkpoint_ref)

    resumed_run = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([{"text": "hello after repeated resume", "model_name": "replay/small"}]),
    ).resume_from_checkpoint(loaded_envelope)
    resumed_checkpoint = _checkpoint_for_boundary(
        shell,
        loaded_envelope.request_id,
        "after_provider_completion",
    )

    assert loaded_envelope.source_checkpoint_ref == prior_lineage_ref
    assert Path(loaded_envelope.selected_checkpoint_ref).resolve() == Path(checkpoint_ref).resolve()
    assert Path(resumed_checkpoint.source_checkpoint_ref).resolve() == Path(checkpoint_ref).resolve()
    assert resumed_run.hard_invalid is False


def test_reduce_grouped_run_results_treats_paused_run_as_non_terminal():
    checkpoint_ref = "checkpoint://external/ref.json"
    reduction = reduce_grouped_run_results(
        [
            RunResult(
                request_id="group.paused",
                plan_id="plan.paused",
                run_id="run.paused",
                run_root="root",
                attempt_id="attempt_0001",
                runtime_hash="hash",
                task_id="task.paused",
                seed=0,
                artifact={"status": "waiting"},
                verifier_score=0.0,
                cost=0.0,
                latency=0.0,
                faults=0,
                hard_invalid=False,
                checkpoint_ref=checkpoint_ref,
                latest_checkpoint_ref=checkpoint_ref,
                run_lifecycle_state="paused",
                lifecycle_state="paused",
            )
        ]
    )

    assert reduction["lifecycle_state"] == "paused"
    assert reduction["latest_checkpoint_ref"] == checkpoint_ref
    assert reduction["resumable"] is True
    assert reduction["prune_eligible"] is False


def test_reduce_grouped_run_results_does_not_treat_payload_error_field_as_failure():
    reduction = reduce_grouped_run_results(
        [
            RunResult(
                request_id="group.payload-error",
                plan_id="plan.payload-error",
                run_id="run.payload-error",
                run_root="root",
                attempt_id="attempt_0001",
                runtime_hash="hash",
                task_id="task.payload-error",
                seed=0,
                artifact={"error": "none", "status": "ok"},
                verifier_score=1.0,
                cost=0.0,
                latency=0.0,
                faults=0,
                lifecycle_state="completed",
            )
        ]
    )

    assert reduction["lifecycle_state"] == "completed"
    assert reduction["failure_kind"] is None


def test_root_side_effect_receipts_are_not_duplicated_in_checkpoint_envelopes(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_direct_response_task("receipts.root.dedupe")
    run = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([{"text": "hello", "model_name": "replay/small"}]),
    ).run_task(task, 0)
    envelope = shell.load_checkpoint_envelope(checkpoint_ref=run.checkpoint_ref)

    request_receipts = [
        receipt
        for receipt in envelope.side_effect_ledger["receipts"]
        if receipt.action_kind == "provider_request"
    ]
    completion_receipts = [
        receipt
        for receipt in envelope.side_effect_ledger["receipts"]
        if receipt.action_kind == "provider_completion"
    ]

    assert len(request_receipts) == 1
    assert len(completion_receipts) == 1
    assert len({receipt.side_effect_id for receipt in envelope.side_effect_ledger["receipts"]}) == len(
        envelope.side_effect_ledger["receipts"]
    )


def test_sync_tool_path_publishes_launch_and_completion_receipts(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_builtin_sum_task("receipts.sync-tool")

    run = TaskRuntime(runtime, shell, ReplayProvider([])).run_task(task, 0)
    envelope = shell.load_checkpoint_envelope(checkpoint_ref=run.checkpoint_ref)

    launch_receipts = [
        receipt for receipt in envelope.side_effect_ledger["receipts"] if receipt.action_kind == "tool_launch"
    ]
    completion_receipts = [
        receipt for receipt in envelope.side_effect_ledger["receipts"] if receipt.action_kind == "tool_completion"
    ]

    assert len(launch_receipts) == 1
    assert len(completion_receipts) == 1
    assert launch_receipts[0].idempotency_key == completion_receipts[0].idempotency_key
    assert launch_receipts[0].result_ref["launch_mode"] == "sync"
    assert completion_receipts[0].result_ref["launch_mode"] == "sync"
    assert len(
        {
            (receipt.action_kind, receipt.idempotency_key)
            for receipt in envelope.side_effect_ledger["receipts"]
            if receipt.action_kind in {"tool_launch", "tool_completion"}
        }
    ) == 2


def test_resume_from_after_provider_completion_restores_completed_root_node_without_restart(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_direct_response_task("resume.after-provider-completion")
    first_run = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([{"text": "hello from checkpoint", "model_name": "replay/small"}]),
    ).run_task(task, 0)
    envelope = _checkpoint_for_boundary(shell, first_run.request_id, "after_provider_completion")

    assert envelope.runtime_state_snapshot.artifacts == {}
    assert envelope.runtime_state_snapshot.plan_node_status["respond"] == "running"

    resume_provider = ReconcilingReplayProvider([])
    resumed_run = TaskRuntime(runtime, shell, resume_provider).resume_from_checkpoint(envelope)
    trace_rows = resumed_run.trace_rows()

    assert resume_provider.generate_calls == 0
    assert resumed_run.hard_invalid is False
    assert resumed_run.artifact == first_run.artifact
    assert sum(
        1
        for row in trace_rows
        if row.get("event") == "node_started" and row.get("node_id") == "respond"
    ) == 0
    assert sum(
        1
        for row in trace_rows
        if row.get("event") == "node_reused_from_checkpoint" and row.get("node_id") == "respond"
    ) == 0
    assert [row["event"] for row in trace_rows if row.get("event") in {"terminal_emitted", "run_failed"}] == [
        "terminal_emitted"
    ]


def test_resume_from_after_tool_completion_restores_completed_root_node_without_restart(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_builtin_sum_task("resume.after-tool-completion")
    run_tool_calls = 0
    original_run_tool = shell.tool_executor.run_tool

    def counting_run_tool(tool_name, args, task_id):
        nonlocal run_tool_calls
        run_tool_calls += 1
        return original_run_tool(tool_name, args, task_id)

    monkeypatch.setattr(shell.tool_executor, "run_tool", counting_run_tool)

    first_run = TaskRuntime(runtime, shell, ReplayProvider([])).run_task(task, 0)
    envelope = _checkpoint_for_boundary(shell, first_run.request_id, "after_tool_completion")

    assert envelope.runtime_state_snapshot.artifacts == {}
    assert envelope.runtime_state_snapshot.plan_node_status["sum"] == "running"

    resumed_run = TaskRuntime(runtime, shell, ReplayProvider([])).resume_from_checkpoint(envelope)
    trace_rows = resumed_run.trace_rows()
    launch_receipts = [
        receipt for receipt in envelope.side_effect_ledger["receipts"] if receipt.action_kind == "tool_launch"
    ]
    completion_receipts = [
        receipt for receipt in envelope.side_effect_ledger["receipts"] if receipt.action_kind == "tool_completion"
    ]

    assert resumed_run.hard_invalid is False
    assert resumed_run.artifact == first_run.artifact
    assert run_tool_calls == 1
    assert len(launch_receipts) == 1
    assert len(completion_receipts) == 1
    assert launch_receipts[0].idempotency_key == completion_receipts[0].idempotency_key
    assert launch_receipts[0].result_ref["launch_mode"] == "sync"
    assert completion_receipts[0].result_ref["launch_mode"] == "sync"
    assert sum(
        1
        for row in trace_rows
        if row.get("event") == "node_started" and row.get("node_id") == "sum"
    ) == 0
    assert sum(
        1
        for row in trace_rows
        if row.get("event") == "node_reused_from_checkpoint" and row.get("node_id") == "sum"
    ) == 0
    assert [row["event"] for row in trace_rows if row.get("event") in {"terminal_emitted", "run_failed"}] == [
        "terminal_emitted"
    ]


def test_run_store_resolve_resume_target_accepts_external_checkpoint_ref(tmp_path):
    store = RunStore(tmp_path / "store")
    manifest = store.create_run(
        request_id="resume.external",
        evaluation_unit_id="resume.external",
        request_mode="user_request",
        runtime_backend="local",
    )
    checkpoint_dir = tmp_path / "external-checkpoints"
    checkpoint_dir.mkdir(parents=True)
    checkpoint_path = checkpoint_dir / "checkpoint.resume.external.json"
    checkpoint_envelope = CheckpointEnvelope(
        checkpoint_id="checkpoint.resume.external",
        runtime_contract_version=RUNTIME_CONTRACT_VERSION,
        runtime_hash="runtime-hash",
        run_id=manifest.run_id,
        run_root=manifest.run_root,
        request_id="resume.external",
        plan_id="plan.external",
        task_id="task.external",
        seed=0,
    )
    checkpoint_path.write_text(json.dumps((checkpoint_envelope).model_dump(), indent=2, sort_keys=True), encoding="utf-8")

    target = store.resolve_resume_target(checkpoint_ref=str(checkpoint_path))

    assert target.run_manifest.run_id == manifest.run_id
    assert target.checkpoint_path == checkpoint_path.resolve()
    assert target.checkpoint_store_dir == checkpoint_dir.resolve()


def test_run_store_resolve_resume_target_uses_manifest_external_latest_checkpoint_ref_for_run_ref(tmp_path):
    store = RunStore(tmp_path / "store")
    manifest = store.create_run(
        request_id="resume.manifest.latest-external",
        evaluation_unit_id="resume.manifest.latest-external",
        request_mode="user_request",
        runtime_backend="local",
    )
    checkpoint_dir = tmp_path / "external-checkpoints"
    checkpoint_dir.mkdir(parents=True)
    checkpoint_path = checkpoint_dir / "checkpoint.resume.manifest.latest-external.json"
    checkpoint_envelope = CheckpointEnvelope(
        checkpoint_id="checkpoint.resume.manifest.latest-external",
        runtime_contract_version=RUNTIME_CONTRACT_VERSION,
        runtime_hash="runtime-hash",
        run_id=manifest.run_id,
        run_root=manifest.run_root,
        request_id="resume.manifest.latest-external",
        plan_id="plan.manifest.latest-external",
        task_id="task.manifest.latest-external",
        seed=0,
    )
    checkpoint_path.write_text(json.dumps((checkpoint_envelope).model_dump(), indent=2, sort_keys=True), encoding="utf-8")
    store.write_run_manifest(
        manifest.model_copy(update={"latest_checkpoint_ref": str(checkpoint_path.resolve()), "resumable": True})
    )

    assert store.latest_usable_checkpoint_ref(manifest.run_id) == str(checkpoint_path.resolve())
    target = store.resolve_resume_target(run_ref=manifest.run_id)

    assert target.run_manifest.run_id == manifest.run_id
    assert target.checkpoint_path == checkpoint_path.resolve()
    assert target.checkpoint_store_dir == checkpoint_dir.resolve()


def test_run_store_resolve_resume_target_falls_back_from_stale_run_root_to_run_id(tmp_path):
    store = RunStore(tmp_path / "store")
    manifest = store.create_run(
        request_id="resume.external.stale-root",
        evaluation_unit_id="resume.external.stale-root",
        request_mode="user_request",
        runtime_backend="local",
    )
    checkpoint_dir = tmp_path / "external-checkpoints"
    checkpoint_dir.mkdir(parents=True)
    checkpoint_path = checkpoint_dir / "checkpoint.resume.external.stale-root.json"
    checkpoint_envelope = CheckpointEnvelope(
        checkpoint_id="checkpoint.resume.external.stale-root",
        runtime_contract_version=RUNTIME_CONTRACT_VERSION,
        runtime_hash="runtime-hash",
        run_id=manifest.run_id,
        run_root="/mnt/runs/run.resume.external.stale-root",
        request_id="resume.external.stale-root",
        plan_id="plan.external.stale-root",
        task_id="task.external.stale-root",
        seed=0,
    )
    checkpoint_path.write_text(json.dumps((checkpoint_envelope).model_dump(), indent=2, sort_keys=True), encoding="utf-8")

    target = store.resolve_resume_target(checkpoint_ref=str(checkpoint_path))

    assert target.run_manifest.run_id == manifest.run_id
    assert target.checkpoint_path == checkpoint_path.resolve()
    assert target.checkpoint_store_dir == checkpoint_dir.resolve()


def test_run_store_resolve_resume_target_prefers_matching_run_id_over_mismatched_local_run_root(tmp_path):
    store = RunStore(tmp_path / "store")
    expected_manifest = store.create_run(
        request_id="resume.external.correct-manifest",
        evaluation_unit_id="resume.external.correct-manifest",
        request_mode="user_request",
        runtime_backend="local",
    )
    mismatched_manifest = store.create_run(
        request_id="resume.external.wrong-manifest",
        evaluation_unit_id="resume.external.wrong-manifest",
        request_mode="user_request",
        runtime_backend="local",
    )
    checkpoint_dir = tmp_path / "external-checkpoints"
    checkpoint_dir.mkdir(parents=True)
    checkpoint_path = checkpoint_dir / "checkpoint.resume.external.mismatched-root.json"
    checkpoint_envelope = CheckpointEnvelope(
        checkpoint_id="checkpoint.resume.external.mismatched-root",
        runtime_contract_version=RUNTIME_CONTRACT_VERSION,
        runtime_hash="runtime-hash",
        run_id=expected_manifest.run_id,
        run_root=mismatched_manifest.run_root,
        request_id="resume.external.mismatched-root",
        plan_id="plan.external.mismatched-root",
        task_id="task.external.mismatched-root",
        seed=0,
    )
    checkpoint_path.write_text(json.dumps((checkpoint_envelope).model_dump(), indent=2, sort_keys=True), encoding="utf-8")

    target = store.resolve_resume_target(checkpoint_ref=str(checkpoint_path))

    assert target.run_manifest.run_id == expected_manifest.run_id
    assert target.run_manifest.run_root == expected_manifest.run_root
    assert target.checkpoint_path == checkpoint_path.resolve()
    assert target.checkpoint_store_dir == checkpoint_dir.resolve()


def test_resume_rebinds_request_identity_and_carries_forward_resume_provenance(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_direct_response_task("resume.rebind.identity")
    plan = compile_execution_plan_from_task(
        task,
        request_id="benchmark.resume.rebind.identity.seed_0",
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    queued_frame = QueuedFrameSnapshot(
        frame_id="frame-root",
        request_id=plan.request_id,
        plan_id=plan.plan_id,
        objective=plan.objective,
        operation_ids=["respond"],
        depth=0,
        role="root",
        tool_scope=sorted(shell.tool_registry.tools),
        model_class="medium",
        trace_context=plan.trace_context.model_copy(update={"op_id": "checkpoint-op", "run_node_id": "checkpoint-node"}),
        agent_snapshot=_canonical_root_snapshot(),
    )
    checkpoint_envelope = CheckpointEnvelope(
        checkpoint_id="checkpoint.resume.rebind.identity.0001",
        runtime_contract_version=runtime.kernel_manifest.runtime_contract_version,
        runtime_hash=runtime.runtime_hash,
        run_id="run.resume.rebind.identity",
        run_root=str(shell.workspace.resolve()),
        attempt_id="attempt_0001",
        runtime_backend="local",
        request_id=plan.request_id,
        plan_id=plan.plan_id,
        task_id=task.task_id,
        seed=0,
        sequence_no=1,
        boundary="before_resume",
        created_at=now_ts(),
        plan_snapshot=(plan).model_dump(),
        task_payload=(task).model_dump(),
        runtime_state_snapshot={
            "request_id": plan.request_id,
            "plan_id": plan.plan_id,
            "execution_state": "running",
            "checkpoint_sequence_no": 1,
            "queued_frames": [(queued_frame).model_dump()],
            "visible_tool_names": sorted(shell.tool_registry.tools),
            "plan_node_status": {},
            "branch_states": {},
            "branch_publications": [],
            "latest_checkpoint_ref": "checkpoint://run-latest",
        },
        working_state={"current_objective": task.prompt, "selected_checkpoint_refs": [plan.request_id]},
        trace_cursor={"last_solve_request_id": plan.request_id, "latest_runtime_event_sequence_no": 0},
    )

    solve_request, rebound_envelope, effective_request_id = solve_request_from_resume_checkpoint(
        checkpoint_envelope,
        request_id_override="resume.identity.override",
        source_checkpoint_ref="checkpoint://resume-source",
        trace_context=OpenAITraceContext(
            request_id="ignored-by-rebind",
            runtime_dir=str((tmp_path / "active-runtime").resolve()),
            runtime_session_id="sess.active",
            runtime_message_id="msg.active",
            runtime_message_index=3,
            op_id="override-op",
            run_node_id="override-node",
        ),
    )

    assert solve_request.request_id == "resume.identity.override"
    assert effective_request_id == "resume.identity.override"
    assert rebound_envelope.request_id == effective_request_id
    assert rebound_envelope.plan_snapshot["request_id"] == effective_request_id
    assert rebound_envelope.plan_snapshot["trace_context"]["request_id"] == effective_request_id
    assert rebound_envelope.plan_snapshot["trace_context"]["runtime_dir"] == str((tmp_path / "active-runtime").resolve())
    assert rebound_envelope.plan_snapshot["trace_context"]["runtime_session_id"] == "sess.active"
    assert rebound_envelope.plan_snapshot["trace_context"]["runtime_message_id"] == "msg.active"
    assert rebound_envelope.plan_snapshot["trace_context"]["runtime_message_index"] == 3
    assert rebound_envelope.runtime_state_snapshot.request_id == effective_request_id
    assert rebound_envelope.runtime_state_snapshot.latest_checkpoint_ref == "checkpoint://resume-source"
    assert rebound_envelope.selected_checkpoint_ref == "checkpoint://resume-source"
    assert rebound_envelope.runtime_state_snapshot.queued_frames[0].request_id == effective_request_id
    assert rebound_envelope.runtime_state_snapshot.queued_frames[0].trace_context.request_id == effective_request_id
    assert rebound_envelope.runtime_state_snapshot.queued_frames[0].trace_context.runtime_dir == str(
        (tmp_path / "active-runtime").resolve()
    )
    assert rebound_envelope.runtime_state_snapshot.queued_frames[0].trace_context.runtime_session_id == "sess.active"
    assert rebound_envelope.runtime_state_snapshot.queued_frames[0].trace_context.runtime_message_id == "msg.active"
    assert rebound_envelope.runtime_state_snapshot.queued_frames[0].trace_context.runtime_message_index == 3
    assert rebound_envelope.runtime_state_snapshot.queued_frames[0].trace_context.op_id == "checkpoint-op"
    assert rebound_envelope.runtime_state_snapshot.queued_frames[0].trace_context.run_node_id == "checkpoint-node"
    assert rebound_envelope.working_state.selected_checkpoint_refs == [plan.request_id]
    assert rebound_envelope.trace_cursor.last_solve_request_id == effective_request_id
    assert rebound_envelope.origin_request_id == plan.request_id
    assert rebound_envelope.source_checkpoint_ref == "checkpoint://resume-source"
    assert rebound_envelope.plan_id == plan.plan_id
    assert rebound_envelope.plan_snapshot["plan_digest"] == plan.plan_digest

    resumed_run = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([{"text": "hello after resume", "model_name": "replay/small"}]),
    ).resume_from_checkpoint(rebound_envelope)

    assert resumed_run.request_id == effective_request_id
    assert {row.get("request_id") for row in resumed_run.trace_rows() if row.get("request_id")} == {
        effective_request_id
    }
    resumed_checkpoint = shell.load_checkpoint_envelope(
        checkpoint_ref=resumed_run.latest_checkpoint_ref or resumed_run.checkpoint_ref
    )
    assert resumed_checkpoint.request_id == effective_request_id
    assert resumed_checkpoint.origin_request_id == plan.request_id
    assert resumed_checkpoint.source_checkpoint_ref == "checkpoint://resume-source"
    assert resumed_checkpoint.plan_id == plan.plan_id
    assert resumed_checkpoint.plan_snapshot["plan_digest"] == plan.plan_digest
    assert resumed_checkpoint.runtime_hash == runtime.runtime_hash


def test_direct_response_run_reports_execution_scoped_provider_usage_and_selected_backend(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    runtime.deployment_contract.supported_backends = ["docker", "local"]
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    provider = ReplayProvider(
        [
            {
                "text": "hello from usage",
                "model_name": "replay/small",
                "input_tokens": 7,
                "output_tokens": 5,
                "token_estimate": 12,
                "latency_s": 0.2,
                "dollar_cost": 0.02,
            }
        ]
    )

    result = TaskRuntime(runtime, shell, provider, runtime_backend="local").run_task(
        _make_direct_response_task("usage.direct"),
        0,
    )
    envelope = shell.load_checkpoint_envelope(checkpoint_ref=result.checkpoint_ref)

    assert result.provider_usage["calls"] == 1
    assert result.provider_usage["input_tokens"] == 7
    assert result.provider_usage["output_tokens"] == 5
    assert result.provider_usage["total_tokens"] == 12
    assert result.provider_usage["dollar_cost"] == pytest.approx(0.02)
    assert result.runtime_backend == "local"
    assert envelope.runtime_backend == "local"
    assert {receipt.backend for receipt in envelope.side_effect_ledger["receipts"]} == {"local"}
    assert {row["runtime_backend"] for row in result.trace_rows() if "runtime_backend" in row} == {"local"}


def test_resume_strict_fails_closed_on_unreconciled_provider_launch_without_reissue(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    envelope = _pending_provider_launch_envelope(
        runtime,
        shell,
        task_id="resume.strict-provider-launch",
    )

    provider = ReconcilingReplayProvider([])
    resumed = TaskRuntime(runtime, shell, provider).resume_from_checkpoint(envelope, reconciliation_policy="strict")

    assert resumed.hard_invalid is True
    assert resumed.failure_kind == "receipt_reconciliation_failed"
    assert provider.generate_calls == 0


def test_resume_strict_fails_closed_on_unreconciled_sync_tool_launch_without_reissue(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    envelope = _pending_sync_tool_launch_envelope(
        runtime,
        shell,
        task_id="resume.strict-sync-tool-launch",
    )

    run_tool_calls = 0
    original_run_tool = shell.tool_executor.run_tool

    def counting_run_tool(tool_name, args, task_id):
        nonlocal run_tool_calls
        run_tool_calls += 1
        return original_run_tool(tool_name, args, task_id)

    monkeypatch.setattr(shell.tool_executor, "run_tool", counting_run_tool)

    resumed = TaskRuntime(runtime, shell, ReplayProvider([])).resume_from_checkpoint(envelope, reconciliation_policy="strict")

    assert resumed.hard_invalid is True
    assert resumed.failure_kind == "receipt_reconciliation_failed"
    assert run_tool_calls == 0


def test_resume_best_effort_blocks_unreconciled_provider_launch_without_reissue(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    envelope = _pending_provider_launch_envelope(
        runtime,
        shell,
        task_id="resume.best-effort-provider-launch",
    )

    provider = ReconcilingReplayProvider([])
    resumed = TaskRuntime(runtime, shell, provider).resume_from_checkpoint(envelope, reconciliation_policy="best_effort")

    assert provider.generate_calls == 0
    assert resumed.hard_invalid is False
    assert resumed.artifact["error"] == "recovery_blocked"


def test_resume_best_effort_blocks_unreconciled_sync_tool_launch_without_reissue(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    envelope = _pending_sync_tool_launch_envelope(
        runtime,
        shell,
        task_id="resume.best-effort-sync-tool-launch",
    )

    run_tool_calls = 0
    original_run_tool = shell.tool_executor.run_tool

    def counting_run_tool(tool_name, args, task_id):
        nonlocal run_tool_calls
        run_tool_calls += 1
        return original_run_tool(tool_name, args, task_id)

    monkeypatch.setattr(shell.tool_executor, "run_tool", counting_run_tool)

    resumed = TaskRuntime(runtime, shell, ReplayProvider([])).resume_from_checkpoint(envelope, reconciliation_policy="best_effort")

    assert run_tool_calls == 0
    assert resumed.hard_invalid is False
    assert resumed.artifact["error"] == "recovery_blocked"
    assert resumed.artifact["node_id"] == "sum"
    assert any(
        row.get("event") == "node_recovery_blocked" and row.get("node_id") == "sum"
        for row in resumed.trace_rows()
    )


def test_resume_strict_fails_closed_on_unreconciled_async_tool_launch_without_fabricated_failure(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    envelope = _pending_async_tool_launch_envelope(
        runtime,
        shell,
        task_id="resume.strict-async-tool-launch",
    )
    handle_id = envelope.runtime_state_snapshot.open_handle_ids[0]

    resumed = TaskRuntime(runtime, shell, ReplayProvider([])).resume_from_checkpoint(envelope, reconciliation_policy="strict")

    assert resumed.hard_invalid is True
    assert resumed.failure_kind == "receipt_reconciliation_failed"
    assert shell.open_handles.get(handle_id).state == "running"


def test_resume_best_effort_blocks_unreconciled_async_tool_launch_without_terminalizing_handle(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    envelope = _pending_async_tool_launch_envelope(
        runtime,
        shell,
        task_id="resume.best-effort-async-tool-launch",
    )
    handle_id = envelope.runtime_state_snapshot.open_handle_ids[0]

    resumed = TaskRuntime(runtime, shell, ReplayProvider([])).resume_from_checkpoint(envelope, reconciliation_policy="best_effort")

    assert resumed.hard_invalid is False
    assert resumed.artifact["error"] == "recovery_blocked"
    assert shell.open_handles.get(handle_id).state == "running"
    assert resumed.checkpoint_ref is None


def test_reconciled_provider_launch_restores_completed_root_node_without_restart(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    envelope = _pending_provider_launch_envelope(
        runtime,
        shell,
        task_id="resume.reconciled-provider-launch",
    )

    provider = ReconcilingReplayProvider(
        [],
        reconciled={
            "provider-request.pending": {
                "text": "hello from reconcile",
                "model_name": "replay/small",
                "input_tokens": 5,
                "output_tokens": 4,
                "token_estimate": 9,
            }
        },
    )
    resumed = TaskRuntime(runtime, shell, provider).resume_from_checkpoint(envelope)
    trace_rows = resumed.trace_rows()

    assert provider.reconcile_calls == ["provider-request.pending"]
    assert provider.generate_calls == 0
    assert resumed.hard_invalid is False
    assert resumed.artifact == "hello from reconcile"
    assert sum(
        1
        for row in trace_rows
        if row.get("event") == "node_started" and row.get("node_id") == "respond"
    ) == 0
    assert sum(
        1
        for row in trace_rows
        if row.get("event") == "node_reused_from_checkpoint" and row.get("node_id") == "respond"
    ) == 0
    assert [row["event"] for row in trace_rows if row.get("event") in {"terminal_emitted", "run_failed"}] == [
        "terminal_emitted"
    ]


def test_branch_owned_terminal_receipt_is_not_projected_into_parent_artifacts(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    envelope = _branch_owned_terminal_provider_receipt_envelope(
        runtime,
        shell,
        task_id="resume.branch-owned-guard",
    )

    resumed = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([{"text": "root-owned", "model_name": "replay/small"}]),
    ).resume_from_checkpoint(envelope)
    trace_rows = resumed.trace_rows()

    assert envelope.side_effect_ledger["receipts"][0].branch_id == "w0"
    assert resumed.hard_invalid is False
    assert resumed.artifact == "root-owned"
    assert sum(
        1
        for row in trace_rows
        if row.get("event") == "node_started" and row.get("node_id") == "respond"
    ) == 1
    assert sum(
        1
        for row in trace_rows
        if row.get("event") == "node_reused_from_checkpoint" and row.get("node_id") == "respond"
    ) == 0


def test_clone_provider_shares_replay_coordinator_but_keeps_usage_local():
    request = SimpleNamespace(model_class="small")
    provider = ReplayProvider(
        [
            {"text": "first"},
            {"text": "second"},
            {"text": "third"},
        ]
    )
    cloned = clone_provider(provider)
    assert provider.generate(request).text == "first"
    assert cloned.generate(request).text == "second"
    assert provider.generate(request).text == "third"
    assert provider.usage_summary()["calls"] == 2
    assert cloned.usage_summary()["calls"] == 1


def test_replay_provider_reserved_clones_consume_only_their_window_and_keep_usage_local():
    request = SimpleNamespace(model_class="small")
    provider = ReplayProvider(
        [
            {"text": "w0-0"},
            {"text": "w0-1"},
            {"text": "w1-0"},
            {"text": "w1-1"},
        ]
    )
    left = provider.clone_for_allocation(provider.reserve_rows(2, allocation_key="request:w0"))
    right = provider.clone_for_allocation(provider.reserve_rows(2, allocation_key="request:w1"))

    assert left.generate(request).text == "w0-0"
    assert right.generate(request).text == "w1-0"
    assert left.generate(request).text == "w0-1"
    assert right.generate(request).text == "w1-1"
    assert provider.usage_summary()["calls"] == 0
    assert left.usage_summary()["calls"] == 2
    assert right.usage_summary()["calls"] == 2


def test_replay_provider_reserved_clones_are_stable_under_thread_timing_variation():
    request = SimpleNamespace(model_class="small")

    def run_with_delays(left_delay: float, right_delay: float):
        provider = ReplayProvider(
            [
                {"text": "w0-0"},
                {"text": "w0-1"},
                {"text": "w1-0"},
                {"text": "w1-1"},
            ]
        )
        left = provider.clone_for_allocation(provider.reserve_rows(2, allocation_key="request:w0"))
        right = provider.clone_for_allocation(provider.reserve_rows(2, allocation_key="request:w1"))

        def consume_two(clone, delay):
            time.sleep(delay)
            return [clone.generate(request).text, clone.generate(request).text]

        with ThreadPoolExecutor(max_workers=2) as executor:
            left_future = executor.submit(consume_two, left, left_delay)
            right_future = executor.submit(consume_two, right, right_delay)
            return left_future.result(), right_future.result()

    assert run_with_delays(0.0, 0.05) == (["w0-0", "w0-1"], ["w1-0", "w1-1"])
    assert run_with_delays(0.05, 0.0) == (["w0-0", "w0-1"], ["w1-0", "w1-1"])


def test_host_resume_applies_request_id_override_for_user_request(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    request = runtime_solve_request_for_user_request(
        runtime_backend="local",
        seed=0,
        solve_request=load_solve_request(prompt="Say hello."),
    )
    first = host.solve(
        runtime_dir,
        request,
        provider=ReplayProvider([{"text": "hello", "model_name": "replay/small"}]),
    )

    resumed = host.resume(
        runtime_dir,
        request=ResumeRequest(
            run_ref=first.solve_result.run_id,
            request_id="resume.user.override",
        ),
        provider=ReplayProvider([]),
    )

    assert resumed.solve_result.request_id == "resume.user.override"
    assert resumed.solve_result.mode == "user_request"


def test_host_resume_applies_request_id_override_for_benchmark_request(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    task = _make_direct_response_task("resume.override.benchmark")
    request = runtime_solve_request_for_task(runtime_backend="local", seed=0, task=task)
    first = host.solve(
        runtime_dir,
        request,
        provider=ReplayProvider([{"text": "hello", "model_name": "replay/small"}]),
    )

    resumed = host.resume(
        runtime_dir,
        request=ResumeRequest(
            run_ref=first.solve_result.run_id,
            request_id="resume.benchmark.override",
        ),
        provider=ReplayProvider([]),
    )

    assert resumed.solve_result.request_id == "resume.benchmark.override"
    assert resumed.solve_result.mode == "benchmark"


def test_host_run_batch_reports_sum_of_run_result_provider_usage(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    tasks = [
        (_make_direct_response_task("batch.usage.one"), 0),
        (_make_direct_response_task("batch.usage.two"), 0),
    ]
    response = host.run_batch(
        runtime_dir,
        tasks,
        provider=ReplayProvider(
            [
                {"text": "one", "model_name": "replay/small", "input_tokens": 3, "output_tokens": 2, "token_estimate": 5, "dollar_cost": 0.01},
                {"text": "two", "model_name": "replay/small", "input_tokens": 4, "output_tokens": 1, "token_estimate": 5, "dollar_cost": 0.02},
            ]
        ),
    )

    summed_usage: dict[str, float | int] = {}
    for run in response.run_results:
        for key, value in run.provider_usage.items():
            summed_usage[key] = summed_usage.get(key, 0) + value

    assert response.provider_usage == summed_usage


def test_host_run_batch_scopes_grouped_episode_trace_rows_to_each_invocation(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    task_one = _make_direct_response_task("episode.trace.one").model_copy(
        update={"transfer_scored": True, "episode_id": "episode-trace", "episode_order": 0}
    )
    task_two = _make_direct_response_task("episode.trace.two").model_copy(
        update={"transfer_scored": True, "episode_id": "episode-trace", "episode_order": 1}
    )

    response = host.run_batch(
        runtime_dir,
        [(task_one, 0), (task_two, 0)],
        provider=ReplayProvider(
            [
                {"text": "first", "model_name": "replay/small"},
                {"text": "second", "model_name": "replay/small"},
            ]
        ),
    )

    for run in response.run_results:
        request_ids = {row.get("request_id") for row in run.trace_rows() if row.get("request_id")}
        assert request_ids == {run.request_id}


def test_vertical_mode_executes_explicit_merge_and_verify_plan_nodes(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_parallel_direct_response_task("vertical.plan").model_copy(
        update={
            "verifier_type": "trace_event",
            "expected": "merge_completed",
            "verification_required": True,
        }
    )
    plan = compile_execution_plan_from_task(
        task,
        request_id="vertical.plan.request",
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    merge_node_ids = [node.node_id for node in plan.nodes if str(node.node_kind) == "merge"]
    verify_node_ids = [node.node_id for node in plan.nodes if str(node.node_kind) == "verify"]
    assert merge_node_ids
    assert verify_node_ids

    monkeypatch.setattr(runtime.topology, "select_mode", lambda ctx, frame, operations: "vertical" if len(operations) > 1 else "single")

    def _children(ctx, frame, operations):
        children = []
        for index, operation in enumerate(operations):
            children.append(
                ChildSpec(
                    child_id=f"child-{index}",
                    role="child",
                    instruction=operation.instruction,
                    tool_scope=[],
                    model_class="small",
                    required_capabilities=["plan"],
                    required_permissions=["local"],
                    dependency_ids=list(operation.dependencies),
                    comm_mode="summary_only",
                    resume_policy="checkpoint",
                    init_summary={"op_id": operation.node_id, "output_key": operation.output_key},
                )
            )
        return children

    monkeypatch.setattr(runtime.topology, "propose_children", _children)

    result = TaskRuntime(
        runtime,
        shell,
        ReplayProvider(
            [
                {"text": "branch-a", "model_name": "replay/small"},
                {"text": "branch-b", "model_name": "replay/small"},
            ]
        ),
    ).run_task(task, 0)

    completed_ids = {row.get("node_id") for row in result.trace_rows() if row.get("event") == "node_completed"}
    assert set(merge_node_ids).issubset(completed_ids)
    assert set(verify_node_ids).issubset(completed_ids)
    assert any(row.get("event") == "merge_completed" for row in result.trace_rows())
    assert result.verifier_score == 1.0


def test_root_empty_frontier_does_not_reschedule_completed_plan_nodes(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_parallel_direct_response_task("vertical.plan.empty-frontier").model_copy(
        update={
            "verifier_type": "trace_event",
            "expected": "merge_completed",
            "verification_required": True,
        }
    )
    plan = compile_execution_plan_from_task(
        task,
        request_id="vertical.plan.empty-frontier.request",
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    merge_node = next(node for node in plan.nodes if str(node.node_kind) == "merge")
    verify_node = next(node for node in plan.nodes if str(node.node_kind) == "verify")
    queued_frame = QueuedFrameSnapshot(
        frame_id="frame-root-continuation",
        request_id=plan.request_id,
        plan_id=plan.plan_id,
        objective=plan.objective,
        operation_ids=[node.node_id for node in plan.nodes],
        depth=0,
        role="root",
        trace_context=plan.trace_context,
        tool_scope=sorted(shell.tool_registry.tools),
        model_class="medium",
        metadata={"run_node_id": "root-run-node"},
        agent_snapshot=_canonical_root_snapshot(),
    )
    envelope = CheckpointEnvelope(
        checkpoint_id="checkpoint.vertical.plan.empty-frontier.0001",
        runtime_contract_version=runtime.kernel_manifest.runtime_contract_version,
        runtime_hash=runtime.runtime_hash,
        request_id=plan.request_id,
        plan_id=plan.plan_id,
        task_id=task.task_id,
        seed=0,
        sequence_no=1,
        boundary="before_terminal_result",
        created_at=now_ts(),
        plan_snapshot=(plan).model_dump(),
        task_payload=(task).model_dump(),
        runtime_state_snapshot={
            "request_id": plan.request_id,
            "plan_id": plan.plan_id,
            "execution_state": "running",
            "checkpoint_sequence_no": 1,
            "queued_frames": [(queued_frame).model_dump()],
            "visible_tool_names": sorted(shell.tool_registry.tools),
            "plan_node_status": {node.node_id: "completed" for node in plan.nodes},
            "artifacts": {
                "response_a": "branch-a",
                "response_b": "branch-b",
                verify_node.output_key: {"verifier_score": 1.0, "verified": True},
            },
            "branch_states": {},
            "branch_publications": [],
        },
        shell_state_snapshot=(shell.snapshot_checkpoint_shell_state()).model_dump(),
    )

    select_mode_calls = []
    propose_children_calls = []

    def _unexpected_select_mode(ctx, frame, operations):
        select_mode_calls.append([node.node_id for node in operations])
        raise AssertionError("empty frontier should terminate before topology selection")

    def _unexpected_propose_children(ctx, frame, operations):
        propose_children_calls.append([node.node_id for node in operations])
        raise AssertionError("completed plan nodes must not respawn children")

    monkeypatch.setattr(runtime.topology, "select_mode", _unexpected_select_mode)
    monkeypatch.setattr(runtime.topology, "propose_children", _unexpected_propose_children)

    result = TaskRuntime(runtime, shell, ReplayProvider([])).resume_from_checkpoint(envelope)
    trace_rows = result.trace_rows()

    assert result.hard_invalid is False
    assert result.run_lifecycle_state == "completed"
    assert result.lifecycle_state == "completed"
    assert result.artifact == {"response_a": "branch-a", "response_b": "branch-b"}
    assert result.verifier_score == 1.0
    assert select_mode_calls == []
    assert propose_children_calls == []
    assert [row["event"] for row in trace_rows if row.get("event") in {"terminal_emitted", "run_failed"}] == [
        "terminal_emitted"
    ]
    assert sum(
        1
        for row in trace_rows
        if row.get("event") == "node_reused_from_checkpoint" and row.get("node_id") == merge_node.node_id
    ) == 0
    assert sum(
        1
        for row in trace_rows
        if row.get("event") == "node_reused_from_checkpoint" and row.get("node_id") == verify_node.node_id
    ) == 0


def test_horizontal_mode_executes_explicit_verify_node_after_merge(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_parallel_direct_response_task("horizontal.plan.verify").model_copy(
        update={
            "expected": {"response_a": "left", "response_b": "right"},
            "verifier_type": "json_exact",
            "verification_required": True,
            "allow_best_effort": False,
        }
    )
    _force_horizontal(monkeypatch, runtime, ["w0", "w1"])
    plan = compile_execution_plan_from_task(
        task,
        request_id="horizontal.plan.verify.request",
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    verify_node = next(node for node in plan.nodes if str(node.node_kind) == "verify")

    result = TaskRuntime(
        runtime,
        shell,
        ReplayProvider(
            [
                {"text": "left", "model_name": "replay/small"},
                {"text": "right", "model_name": "replay/small"},
                {"text": "unused", "model_name": "replay/small"},
                {"text": "unused", "model_name": "replay/small"},
            ]
        ),
        budget_overrides={"M_max": 4, "Q_max": 1},
    ).run_task(task, 0, plan=plan)
    trace_rows = result.trace_rows()

    assert result.hard_invalid is False
    assert any(
        row.get("event") == "node_completed" and row.get("node_id") == verify_node.node_id
        for row in trace_rows
    )
    verify_start_index = next(
        index
        for index, row in enumerate(trace_rows)
        if row.get("event") == "node_started" and row.get("node_id") == verify_node.node_id
    )
    merge_index = next(
        index
        for index, row in enumerate(trace_rows)
        if row.get("event") == "merge_completed"
    )
    assert merge_index < verify_start_index


def test_merge_ensemble_prefers_verifier_support_before_merge_priority(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    merged = runtime.topology.merge_ensemble(
        None,
        [
            {
                "branch_id": "w0",
                "merge_priority": 0,
                "verifier_support": 0.25,
                "predicted_solve": 0.95,
                "unresolved_critical": 0,
                "artifact": {"winner": "priority-first"},
            },
            {
                "branch_id": "w1",
                "merge_priority": 2,
                "verifier_support": 1.0,
                "predicted_solve": 0.10,
                "unresolved_critical": 0,
                "artifact": {"winner": "verified"},
            },
        ],
    )
    assert merged == {"winner": "verified"}


def test_horizontal_branch_reservations_respect_remaining_model_calls(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    _force_horizontal(monkeypatch, runtime, ["w0", "w1", "w2"])
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_parallel_direct_response_task("horizontal.budget")
    runner = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([{"text": "same"}, {"text": "same"}]),
        budget_overrides={"M_max": 2, "Q_max": 1},
    )
    result = runner.run_task(task, 0)
    envelope = _checkpoint_for_boundary(shell, result.request_id, "after_branch_completion")

    assert result.hard_invalid is False
    assert result.model_calls == 2
    assert (
        sum(
            int((branch_state).model_dump()["reserved_budget"]["model_calls_max"])
            for branch_state in envelope.runtime_state_snapshot.branch_states.values()
        )
        <= 2
    )


def test_horizontal_branch_admission_skips_fanout_when_latency_floor_is_infeasible(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    _force_horizontal(monkeypatch, runtime, ["w0", "w1", "w2"])
    profile = load_runtime_profile(runtime_dir)
    profile.execution.branch_latency_floor_s = 1.0
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_parallel_direct_response_task("horizontal.latency")
    runner = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([{"text": "left"}, {"text": "right"}]),
        budget_overrides={"L_max": 1.5, "M_max": 8, "Q_max": 1},
        runtime_profile=profile,
    )

    result = runner.run_task(task, 0)
    branch_skip_events = [row for row in result.trace_rows() if row.get("event") == "branch_skipped"]

    assert result.hard_invalid is False
    assert result.artifact == {"response_a": "left", "response_b": "right"}
    assert len(branch_skip_events) == 1
    assert branch_skip_events[0]["reason"] == "joint_budget_infeasible"
    assert not any(row.get("event") == "branch_started" for row in result.trace_rows())
    assert not any(row.get("event") == "merge_started" for row in result.trace_rows())


def test_horizontal_replay_branch_allocation_is_stable_under_completion_order_variation(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_parallel_direct_response_task("horizontal.replay.order")
    plan = compile_execution_plan_from_task(
        task,
        request_id="horizontal.replay.order.request",
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )

    monkeypatch.setattr(runtime.topology, "select_mode", lambda ctx, frame, operations: "horizontal")
    monkeypatch.setattr(
        runtime.topology,
        "select_workers",
        lambda ctx, frame, operations: [
            {
                "worker_id": "w0",
                "instruction": "worker-w0",
                "op_ids": ["respond_a"],
                "predicted_solve": 0.9,
                "tool_scope": ctx.state.visible_tool_names,
                "agent_id": "root",
            },
            {
                "worker_id": "w1",
                "instruction": "worker-w1",
                "op_ids": ["respond_b"],
                "predicted_solve": 0.8,
                "tool_scope": ctx.state.visible_tool_names,
                "agent_id": "root",
            },
        ],
    )
    monkeypatch.setattr(
        runtime.topology,
        "merge_ensemble",
        lambda ctx, worker_outputs: {
            "response_a": next(item["artifact"] for item in worker_outputs if item["branch_id"] == "w0"),
            "response_b": next(item["artifact"] for item in worker_outputs if item["branch_id"] == "w1"),
        },
    )

    def run_with_delays(delays_by_worker):
        provider = DelayingReplayProvider(
            [
                {"text": "branch-a", "model_name": "replay/small"},
                {"text": "branch-b", "model_name": "replay/small"},
            ],
            delays_by_worker=delays_by_worker,
        )
        return TaskRuntime(runtime, shell, provider, budget_overrides={"M_max": 2, "Q_max": 1}).run_task(
            task,
            0,
            plan=plan,
        )

    slower_left = run_with_delays({"w0": 0.05, "w1": 0.0})
    slower_right = run_with_delays({"w0": 0.0, "w1": 0.05})

    assert slower_left.hard_invalid is False
    assert slower_right.hard_invalid is False
    assert slower_left.artifact == {"response_a": "branch-a", "response_b": "branch-b"}
    assert slower_right.artifact == {"response_a": "branch-a", "response_b": "branch-b"}


def test_horizontal_branch_run_reports_sum_of_branch_provider_usage(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    _force_horizontal(monkeypatch, runtime, ["w0", "w1"])
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_parallel_direct_response_task("horizontal.usage")
    runner = TaskRuntime(
        runtime,
        shell,
        ReplayProvider(
            [
                {"text": "branch-a", "model_name": "replay/small", "input_tokens": 3, "output_tokens": 2, "token_estimate": 5, "dollar_cost": 0.01},
                {"text": "branch-b", "model_name": "replay/small", "input_tokens": 4, "output_tokens": 1, "token_estimate": 5, "dollar_cost": 0.02},
                {"text": "branch-c", "model_name": "replay/small", "input_tokens": 5, "output_tokens": 3, "token_estimate": 8, "dollar_cost": 0.03},
                {"text": "branch-d", "model_name": "replay/small", "input_tokens": 6, "output_tokens": 2, "token_estimate": 8, "dollar_cost": 0.04},
            ]
        ),
        budget_overrides={"M_max": 4, "Q_max": 1},
    )

    result = runner.run_task(task, 0)

    assert result.provider_usage["calls"] == 4
    assert result.provider_usage["input_tokens"] == 18
    assert result.provider_usage["output_tokens"] == 8
    assert result.provider_usage["total_tokens"] == 26
    assert result.provider_usage["dollar_cost"] == pytest.approx(0.10)


def test_after_branch_completion_checkpoint_carries_branch_receipts(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    _force_horizontal(monkeypatch, runtime, ["w0", "w1"])
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_parallel_direct_response_task("horizontal.receipts")
    runner = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([{"text": "same"}] * 4),
        budget_overrides={"M_max": 4, "Q_max": 1},
    )
    result = runner.run_task(task, 0)
    envelope = _checkpoint_for_boundary(shell, result.request_id, "after_branch_completion")

    completion_receipts = [
        receipt
        for receipt in envelope.side_effect_ledger["receipts"]
        if receipt.action_kind == "provider_completion"
    ]
    assert {receipt.branch_id for receipt in completion_receipts} == {"w0", "w1"}
    assert {receipt.result_ref.get("text") for receipt in completion_receipts} == {"same"}


def test_resume_from_after_branch_completion_reuses_saved_branch_frontier(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    _force_horizontal(monkeypatch, runtime, ["w0", "w1"])
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_parallel_direct_response_task("horizontal.resume")
    first_runner = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([{"text": "same"}] * 4),
        budget_overrides={"M_max": 4, "Q_max": 1},
    )
    first_run = first_runner.run_task(task, 0)
    envelope = _checkpoint_for_boundary(shell, first_run.request_id, "after_branch_completion")

    resume_runner = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([]),
        budget_overrides={"M_max": 4, "Q_max": 1},
    )
    resumed_run = resume_runner.resume_from_checkpoint(envelope)

    assert resumed_run.hard_invalid is False
    assert resumed_run.model_calls == first_run.model_calls
    assert resumed_run.artifact == first_run.artifact


def test_resume_from_branch_side_effect_checkpoint_reconstructs_in_flight_branch_workers(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    monkeypatch.setattr(runtime.topology, "select_mode", lambda ctx, frame, operations: "horizontal")
    monkeypatch.setattr(
        runtime.topology,
        "merge_ensemble",
        lambda ctx, worker_outputs: {
            "response_a": next(item["artifact"] for item in worker_outputs if item["branch_id"] == "w0"),
            "response_b": next(item["artifact"] for item in worker_outputs if item["branch_id"] == "w1"),
        },
    )
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    envelope, receipt_keys = _branch_resume_checkpoint_envelope(
        runtime,
        shell,
        task_id="horizontal.resume.branch-side-effect",
        left_snapshot_kind="provider_launch",
        right_snapshot_kind="node_completed",
    )
    provider = ReconcilingReplayProvider(
        [],
        reconciled={
            receipt_keys["w0"]: {
                "text": "w0-value",
                "model_name": "replay/small",
                "input_tokens": 3,
                "output_tokens": 2,
                "token_estimate": 5,
            }
        },
    )
    monkeypatch.setattr("agintor.runner.clone_provider", lambda provider, provider_profile=None: provider)

    resumed_run = TaskRuntime(runtime, shell, provider).resume_from_checkpoint(envelope)

    assert resumed_run.hard_invalid is False
    assert resumed_run.artifact == {"response_a": "w0-value", "response_b": "w1-value"}
    assert provider.generate_calls == 0
    assert provider.reconcile_calls == [receipt_keys["w0"]]


def test_resume_from_branch_node_checkpoint_reconstructs_without_refanout(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    monkeypatch.setattr(runtime.topology, "select_mode", lambda ctx, frame, operations: "horizontal")
    monkeypatch.setattr(
        runtime.topology,
        "merge_ensemble",
        lambda ctx, worker_outputs: {
            "response_a": next(item["artifact"] for item in worker_outputs if item["branch_id"] == "w0"),
            "response_b": next(item["artifact"] for item in worker_outputs if item["branch_id"] == "w1"),
        },
    )
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    envelope, _ = _branch_resume_checkpoint_envelope(
        runtime,
        shell,
        task_id="horizontal.resume.branch-node",
        left_snapshot_kind="node_completed",
        right_snapshot_kind="pending",
    )
    provider = ReplayProvider([{"text": "w1-value", "model_name": "replay/small"}])
    monkeypatch.setattr("agintor.runner.clone_provider", lambda provider, provider_profile=None: provider)

    resumed_run = TaskRuntime(runtime, shell, provider).resume_from_checkpoint(envelope)

    assert resumed_run.hard_invalid is False
    assert resumed_run.artifact == {"response_a": "w0-value", "response_b": "w1-value"}
    assert resumed_run.provider_usage["calls"] == 1


def test_failed_branch_completion_checkpoint_is_not_advertised_as_resumable(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    _force_horizontal(monkeypatch, runtime, ["w0", "w1"])
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_parallel_direct_response_task("horizontal.failed.checkpoint")
    runner = TaskRuntime(
        runtime,
        shell,
        LocalDeterministicProvider(),
        budget_overrides={"M_max": 4, "Q_max": 1},
    )
    sibling_started = Event()

    def fake_run_branch_plan(self, parent_context, task, plan, branch_plan, cancellation_event, persist_lock, *args, **kwargs):
        if branch_plan.branch_id == "w1":
            sibling_started.set()
            if cancellation_event.wait(1.0):
                return _branch_result(
                    branch_plan,
                    status="cancelled",
                    cancellation_reason=str(getattr(cancellation_event, "reason", "fatal_branch_fault")),
                )
            return _branch_result(
                branch_plan,
                status="completed",
                artifact={"response_a": "unexpected", "response_b": "unexpected"},
            )
        assert sibling_started.wait(1.0)
        return _branch_result(branch_plan, status="failed", failure_kind="protocol_failure")

    monkeypatch.setattr(TaskRuntime, "_run_branch_plan", fake_run_branch_plan)

    result = runner.run_task(task, 0)
    after_branch_completion = _checkpoint_for_boundary(shell, result.request_id, "after_branch_completion")
    advertised_checkpoint = shell.load_checkpoint_envelope(checkpoint_ref=result.checkpoint_ref)

    assert result.hard_invalid is True
    assert after_branch_completion.resume_eligible is False
    assert after_branch_completion.resume_ineligibility_reason == "failed_branch_group"
    assert advertised_checkpoint.resume_eligible is True
    assert advertised_checkpoint.checkpoint_id != after_branch_completion.checkpoint_id


def test_tool_executor_cancel_async_handle_terminates_process_and_marks_handle(tmp_path):
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    handle = AsyncHandle(
        handle_id="handle.cancel.1",
        tool_name="dummy-tool",
        sandbox_hash="sandbox-hash",
        working_directory=str(tmp_path),
        launch_time=now_ts(),
        timeout=60,
        stdout_path=str(tmp_path / "stdout.txt"),
        stderr_path=str(tmp_path / "stderr.txt"),
        state="running",
        artifact_refs=[str(tmp_path / "result.json")],
        process_pid=process.pid,
    )
    shell.open_handles.add(handle)
    shell.tool_executor._async_processes[handle.handle_id] = _AsyncProcessRecord(
        process=process,
        state={"stdout": "", "stderr": "", "output": None, "artifact_refs": []},
    )

    cancelled = shell.tool_executor.cancel_async_handle(handle.handle_id, shell.open_handles)

    assert cancelled["state"] == "cancelled"
    assert shell.open_handles.get(handle.handle_id).state == "cancelled"
    assert handle.handle_id not in shell.tool_executor._async_processes
    assert process.poll() is not None


def test_cancelled_branch_cleanup_emits_only_cleanup_and_reconciliation_records(tmp_path, monkeypatch):
    runner, branch_plan, branch_context = _make_branch_cleanup_context(tmp_path)
    branch_context.state.branch_publications.append(
        {
            "publication_id": "publication.old",
            "publication_kind": "candidate_artifact",
            "logical_key": "w0.old",
            "sequence_no": 0,
            "accepted": True,
            "branch_id": "w0",
            "payload": {"artifact": {"stale": True}},
        }
    )
    handle = AsyncHandle(
        handle_id="handle.branch.cancel",
        tool_name="dummy-tool",
        sandbox_hash="sandbox-hash",
        working_directory=str(tmp_path),
        launch_time=now_ts(),
        timeout=60,
        state="running",
        artifact_refs=[],
    )
    branch_context.shell.open_handles.add(handle)
    branch_context.state.open_handle_ids.append(handle.handle_id)
    branch_context.state.side_effect_receipts.append(
        (SideEffectReceipt(
                side_effect_id="tool-launch.branch",
                action_fingerprint="tool-launch.branch",
                idempotency_key="tool-launch.branch",
                action_kind="tool_launch",
                request_id=branch_context.request_id,
                plan_id=branch_context.plan.plan_id,
                frame_id=branch_context.active_frame.frame_id,
                node_id="respond_a",
                branch_id="w0",
                trace_context=branch_context.trace_context,
                request_digest="tool-launch.branch",
                backend="local",
                status="launched",
                result_ref={"tool_name": handle.tool_name, "launch_mode": "async", "handle_id": handle.handle_id},
                created_at=now_ts(),
            )).model_dump()
    )

    def fake_cancel(handle_id, handle_table):
        handle_table.update_state(handle_id, "cancelled")
        return {"handle_id": handle_id, "state": "cancelled"}

    monkeypatch.setattr(branch_context.shell.tool_executor, "cancel_async_handle", fake_cancel)

    result = runner._cancelled_branch_result(
        branch_plan,
        branch_context,
        unresolved_critical=1,
        reason="parent_stop_policy",
        details={},
    )

    assert branch_context.shell.open_handles.get(handle.handle_id).state == "cancelled"
    assert all(
        publication.publication_kind in {"cleanup_record", "reconciliation_record"}
        for publication in result.branch_state.publications
    )
    assert {receipt.status for receipt in result.side_effect_receipts} == {"abandoned"}


def test_cancelled_branch_cleanup_fails_closed_when_handle_cannot_be_cleaned_up(tmp_path, monkeypatch):
    runner, branch_plan, branch_context = _make_branch_cleanup_context(tmp_path)
    handle = AsyncHandle(
        handle_id="handle.branch.failure",
        tool_name="dummy-tool",
        sandbox_hash="sandbox-hash",
        working_directory=str(tmp_path),
        launch_time=now_ts(),
        timeout=60,
        state="running",
        artifact_refs=[],
    )
    branch_context.shell.open_handles.add(handle)
    branch_context.state.open_handle_ids.append(handle.handle_id)
    branch_context.state.side_effect_receipts.append(
        (SideEffectReceipt(
                side_effect_id="tool-launch.failure",
                action_fingerprint="tool-launch.failure",
                idempotency_key="tool-launch.failure",
                action_kind="tool_launch",
                request_id=branch_context.request_id,
                plan_id=branch_context.plan.plan_id,
                frame_id=branch_context.active_frame.frame_id,
                node_id="respond_a",
                branch_id="w0",
                trace_context=branch_context.trace_context,
                request_digest="tool-launch.failure",
                backend="local",
                status="launched",
                result_ref={"tool_name": handle.tool_name, "launch_mode": "async", "handle_id": handle.handle_id},
                created_at=now_ts(),
            )).model_dump()
    )

    monkeypatch.setattr(
        branch_context.shell.tool_executor,
        "cancel_async_handle",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
    )

    with pytest.raises(Exception, match="failed to clean up handle"):
        runner._cancelled_branch_result(
            branch_plan,
            branch_context,
            unresolved_critical=1,
            reason="parent_stop_policy",
            details={},
        )


def test_cancelled_branch_fails_closed_on_unresolved_sync_tool_launch(tmp_path):
    runner, branch_plan, branch_context = _make_branch_cleanup_context(tmp_path)
    branch_context.state.side_effect_receipts.append(
        (SideEffectReceipt(
                side_effect_id="tool-launch.sync",
                action_fingerprint="tool-launch.sync",
                idempotency_key="tool-launch.sync",
                action_kind="tool_launch",
                request_id=branch_context.request_id,
                plan_id=branch_context.plan.plan_id,
                frame_id=branch_context.active_frame.frame_id,
                node_id="respond_a",
                branch_id="w0",
                trace_context=branch_context.trace_context,
                request_digest="tool-launch.sync",
                backend="local",
                status="launched",
                result_ref={"tool_name": "math/basic/sum_numbers", "launch_mode": "sync"},
                created_at=now_ts(),
            )).model_dump()
    )

    with pytest.raises(Exception, match="unresolved sync tool launch"):
        runner._cancelled_branch_result(
            branch_plan,
            branch_context,
            unresolved_critical=1,
            reason="parent_stop_policy",
            details={},
        )


def test_emit_branch_publication_starts_unaccepted(tmp_path):
    runner, branch_plan, branch_context = _make_branch_cleanup_context(tmp_path)

    publication = runner._emit_branch_publication(
        branch_context,
        publication_kind="candidate_artifact",
        logical_key="w0.artifact",
        payload={"artifact": {"ok": True}},
    )

    assert publication is not None
    assert publication.accepted is False
    assert branch_context.state.branch_publications[0]["accepted"] is False


def test_horizontal_branch_overspend_becomes_failed_branch_result(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    _force_horizontal(monkeypatch, runtime, ["w0"])
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_parallel_direct_response_task("horizontal.overspend")
    runner = TaskRuntime(runtime, shell, LocalDeterministicProvider(), budget_overrides={"M_max": 4, "Q_max": 1})
    original_execute_isolated_frame = TaskRuntime._execute_isolated_frame

    def fake_execute_isolated_frame(self, branch_context, frame, operations, isolate_runtime_state=False):
        if frame.worker_id:
            branch_context.budget.calls = branch_context.budget.M_max + 1
            checkpoint = Checkpoint(
                summary=runner.runtime.topology.make_checkpoint(branch_context, frame, {}, [], []).summary,
                artifact_refs=[],
                open_handles=[],
                unresolved_goals=["response_a"],
                budget_state=branch_context.budget.normalized(),
                verifier_state={},
                resume_constraints={},
            )
            return {"response_a": "too-much"}, 0, checkpoint
        return original_execute_isolated_frame(self, branch_context, frame, operations, isolate_runtime_state=isolate_runtime_state)

    monkeypatch.setattr(TaskRuntime, "_execute_isolated_frame", fake_execute_isolated_frame)

    result = runner.run_task(task, 0)
    envelope = _checkpoint_for_boundary(shell, result.request_id, "after_branch_completion")

    assert result.hard_invalid is True
    assert envelope.runtime_state_snapshot.branch_states["w0"].status == "failed"
    assert envelope.runtime_state_snapshot.branch_states["w0"].failure_kind == "reservation_exceeded"


def test_stop_policy_cancellation_with_unresolved_work_finishes_cancelled(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_direct_response_task("stop.cancelled")
    runner = TaskRuntime(runtime, shell, ReplayProvider([]))

    monkeypatch.setattr(runtime.control, "stop_policy", lambda *args, **kwargs: True)
    monkeypatch.setattr(runner, "_run_root_frame", lambda *args, **kwargs: (None, 0, 0.0, False))

    result = runner.run_task(task, 0)
    terminal_events = [
        row
        for row in result.trace_rows()
        if row.get("event") in {"run_cancelled", "terminal_emitted", "run_failed"}
    ]

    assert result.run_lifecycle_state == "cancelled"
    assert result.lifecycle_state == "cancelled"
    assert [row["event"] for row in terminal_events] == ["run_cancelled"]
    assert terminal_events[0]["reason"] == "parent_stop_policy"


def test_stop_policy_completion_path_with_terminal_artifact_stays_completed(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_direct_response_task("stop.completed")
    runner = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([{"text": "hello"}]),
    )

    monkeypatch.setattr(runtime.control, "stop_policy", lambda *args, **kwargs: True)

    result = runner.run_task(task, 0)
    terminal_events = [
        row
        for row in result.trace_rows()
        if row.get("event") in {"run_cancelled", "terminal_emitted", "run_failed"}
    ]

    assert result.run_lifecycle_state == "completed"
    assert result.lifecycle_state == "completed"
    assert [row["event"] for row in terminal_events] == ["terminal_emitted"]


def test_active_runnable_frontier_prefers_earlier_singleton(tmp_path):
    runner, plan, context, _ = _build_mixed_frontier_context(tmp_path)

    frontier = runner._active_runnable_frontier(context, plan)

    assert [node.node_id for node in frontier] == ["a"]


def test_active_runnable_frontier_advances_to_group_after_singleton_completion(tmp_path):
    runner, plan, context, _ = _build_mixed_frontier_context(tmp_path)
    context.state.plan_node_status["a"] = "completed"

    frontier = runner._active_runnable_frontier(context, plan)

    assert [node.node_id for node in frontier] == ["b", "c"]


def test_active_runnable_frontier_honors_explicit_branch_group_override(tmp_path):
    runner, plan, context, grouped_frontier_id = _build_mixed_frontier_context(tmp_path)

    frontier = runner._active_runnable_frontier(context, plan, branch_group_id=grouped_frontier_id)

    assert [node.node_id for node in frontier] == ["b", "c"]


def test_single_output_number_exact_verifies_raw_artifact(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_exact_direct_response_task(
        "verify.number-exact",
        expected=42,
        verifier_type="number_exact",
    )
    plan = compile_execution_plan_from_task(
        task,
        request_id="verify.number-exact.request",
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )

    result = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([{"text": "42", "model_name": "replay/small"}]),
    ).run_task(task, 0, plan=plan)

    assert result.verifier_score == 1.0
    assert result.artifact == 42


def test_single_output_string_exact_verifies_raw_artifact(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_exact_direct_response_task(
        "verify.string-exact",
        expected="hello",
        verifier_type="string_exact",
    )
    plan = compile_execution_plan_from_task(
        task,
        request_id="verify.string-exact.request",
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )

    result = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([{"text": "hello", "model_name": "replay/small"}]),
    ).run_task(task, 0, plan=plan)

    assert result.verifier_score == 1.0
    assert result.artifact == "hello"


def test_multi_output_verification_keeps_mapping_artifact(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_parallel_direct_response_task("verify.multi-output").model_copy(
        update={
            "expected": {"response_a": "left", "response_b": "right"},
            "verifier_type": "json_exact",
            "verification_required": True,
            "allow_best_effort": False,
        }
    )
    plan = compile_execution_plan_from_task(
        task,
        request_id="verify.multi-output.request",
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    monkeypatch.setattr(runtime.topology, "select_mode", lambda ctx, frame, operations: "single")

    result = TaskRuntime(
        runtime,
        shell,
        ReplayProvider(
            [
                {"text": "left", "model_name": "replay/small"},
                {"text": "right", "model_name": "replay/small"},
            ]
        ),
    ).run_task(task, 0, plan=plan)

    assert result.verifier_score == 1.0
    assert result.artifact == {"response_a": "left", "response_b": "right"}
    assert isinstance(result.artifact, dict)


def test_failed_branch_result_cancels_running_sibling(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    _force_horizontal(monkeypatch, runtime, ["w0", "w1"])
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_parallel_direct_response_task("horizontal.failed-running-sibling")
    runner = TaskRuntime(
        runtime,
        shell,
        LocalDeterministicProvider(),
        budget_overrides={"M_max": 4, "Q_max": 1},
    )
    sibling_started = Event()

    def fake_run_branch_plan(self, parent_context, task, plan, branch_plan, cancellation_event, persist_lock):
        if branch_plan.branch_id == "w1":
            sibling_started.set()
            if cancellation_event.wait(1.0):
                return _branch_result(
                    branch_plan,
                    status="cancelled",
                    cancellation_reason=str(getattr(cancellation_event, "reason", "fatal_branch_fault")),
                )
            return _branch_result(
                branch_plan,
                status="completed",
                artifact={"response_a": "unexpected", "response_b": "unexpected"},
            )
        assert sibling_started.wait(1.0)
        return _branch_result(branch_plan, status="failed", failure_kind="protocol_failure")

    monkeypatch.setattr(TaskRuntime, "_run_branch_plan", fake_run_branch_plan)

    result = runner.run_task(task, 0)
    envelope = _checkpoint_for_boundary(shell, result.request_id, "after_branch_completion")

    assert result.hard_invalid is True
    assert envelope.runtime_state_snapshot.branch_states["w0"].status == "failed"
    assert envelope.runtime_state_snapshot.branch_states["w1"].status == "cancelled"


@pytest.mark.parametrize(
    ("failure_kind", "expected_reason"),
    [
        ("verification_failure", "verification_failure"),
        ("reservation_exceeded", "budget_exhaustion"),
    ],
)
def test_failed_branch_kind_maps_to_sibling_cancellation_reason(tmp_path, monkeypatch, failure_kind, expected_reason):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    _force_horizontal(monkeypatch, runtime, ["w0", "w1"])
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_parallel_direct_response_task(f"horizontal.reason.{failure_kind}")
    runner = TaskRuntime(
        runtime,
        shell,
        LocalDeterministicProvider(),
        budget_overrides={"M_max": 4, "Q_max": 1},
    )
    sibling_started = Event()

    def fake_run_branch_plan(self, parent_context, task, plan, branch_plan, cancellation_event, persist_lock):
        if branch_plan.branch_id == "w1":
            sibling_started.set()
            if cancellation_event.wait(1.0):
                return _branch_result(
                    branch_plan,
                    status="cancelled",
                    cancellation_reason=str(getattr(cancellation_event, "reason", "parent_stop_policy")),
                )
            return _branch_result(
                branch_plan,
                status="completed",
                artifact={"response_a": "unexpected", "response_b": "unexpected"},
            )
        assert sibling_started.wait(1.0)
        return _branch_result(branch_plan, status="failed", failure_kind=failure_kind)

    monkeypatch.setattr(TaskRuntime, "_run_branch_plan", fake_run_branch_plan)

    result = runner.run_task(task, 0)
    envelope = _checkpoint_for_boundary(shell, result.request_id, "after_branch_completion")

    assert result.hard_invalid is True
    assert envelope.runtime_state_snapshot.branch_states["w1"].status == "cancelled"
    assert envelope.runtime_state_snapshot.branch_states["w1"].cancellation_record.reason == expected_reason


def test_resume_recovery_error_still_cancels_siblings(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    _force_horizontal(monkeypatch, runtime, ["w0", "w1"])
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_parallel_direct_response_task("horizontal.resume-recovery")
    runner = TaskRuntime(
        runtime,
        shell,
        LocalDeterministicProvider(),
        budget_overrides={"M_max": 4, "Q_max": 1},
    )
    sibling_started = Event()

    def fake_run_branch_plan(self, parent_context, task, plan, branch_plan, cancellation_event, persist_lock):
        if branch_plan.branch_id == "w1":
            sibling_started.set()
            if cancellation_event.wait(1.0):
                return _branch_result(
                    branch_plan,
                    status="cancelled",
                    cancellation_reason=str(getattr(cancellation_event, "reason", "fatal_branch_fault")),
                )
            return _branch_result(
                branch_plan,
                status="completed",
                artifact={"response_a": "unexpected", "response_b": "unexpected"},
            )
        assert sibling_started.wait(1.0)
        raise ResumeRecoveryError("receipt_reconciliation_failed", "resume reconciliation failed")

    monkeypatch.setattr(TaskRuntime, "_run_branch_plan", fake_run_branch_plan)

    result = runner.run_task(task, 0)
    envelope = _checkpoint_for_boundary(shell, result.request_id, "after_branch_completion")

    assert result.hard_invalid is True
    assert result.failure_kind == "receipt_reconciliation_failed"
    assert envelope.runtime_state_snapshot.branch_states["w1"].status == "cancelled"
