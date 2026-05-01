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

from agintor.storage.artifacts import ArtifactMode
from agintor.storage import state_store
from agintor.core.exceptions import HardInvalidation, PromptAdaptationError, ResumeRecoveryError
from agintor.runtime.project import init_runtime
from agintor.providers import LocalDeterministicProvider, ReplayProvider, clone_provider
from agintor.runtime.api import (
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
from agintor.runtime.host import RuntimeHost
from agintor.storage.run_store import RunStore
from agintor.runtime.loader import load_runtime
from agintor.runtime.profile import load_runtime_profile
from agintor.runtime.kernel.facade import TaskRuntime
from agintor.contracts import (
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
from agintor.runtime.kernel.shell import FixedShell
from agintor.runtime.tools.models import _AsyncProcessRecord
from agintor.utils import now_ts, stable_hash
from agintor.core.versioning import RUNTIME_CONTRACT_VERSION

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

