from __future__ import annotations

import time
from typing import Any, Mapping, Sequence
from ...core.exceptions import BranchCancelled, HardInvalidation, ProviderExhaustedError, ResumeRecoveryError
from ..api import (
    AgentFrame,
    PolicyContext,
    RuntimeBudget,
    RuntimeState,
    compile_execution_plan_from_task,
    get_plan_node_descriptor,
    normalize_benchmark_request_id,
)
from .memory_graph import LongTermGraph
from ...contracts import (
    AgentTemplate,
    AsyncHandle,
    BenchmarkTask,
    BranchBudget,
    BranchPlan,
    BranchPublication,
    BranchResumeSnapshot,
    BranchResult,
    BranchState,
    CancellationRecord,
    Checkpoint,
    CheckpointEnvelope,
    ChildSpec,
    ExecutionPlan,
    MemoryNode,
    OpenAITraceContext,
    PlanNode,
    QueuedAgentSnapshot,
    QueuedFrameSnapshot,
    RecoveryFailureKind,
    ReceiptReconciliationRecord,
    ReplayAllocation,
    RunResult,
    RuntimeSessionSeed,
    SideEffectReceipt,
    capability_scope_allows,
    plan_node_requires_default_provider,
    service_action_transport_compatibility,
    is_terminal_receipt,
    terminalize_receipt,
)
from ...utils import count_tokens_rough, ensure_directory, merge_provider_usage, now_ts, stable_hash

class RootFrameMixin:
    def _run_root_frame(
        self,
        context: PolicyContext,
        frame: AgentFrame,
        task: BenchmarkTask,
        plan: ExecutionPlan,
        verifier_score: float,
        verified_terminal: bool,
        provider_usage_ledger: dict[str, Any],
    ) -> tuple[Any, int, float, bool]:
        faults = 0
        artifact: Any = None
        frontier_nodes = self._active_runnable_frontier(context, plan, branch_group_id=frame.branch_group_id)
        if not frontier_nodes:
            if self._all_outputs_present(plan, context.state.artifacts):
                artifact = self._terminal_artifact(plan, context.state.artifacts)
                artifact, verifier_score, verified_terminal = self._resolve_terminal_progress(
                    context,
                    frame,
                    task,
                    plan,
                    artifact,
                    verifier_score,
                    verified_terminal,
                )
                return artifact, faults, verifier_score, verified_terminal
            return None, faults, verifier_score, verified_terminal
        candidate_nodes = frontier_nodes
        mode = self.runtime.topology.select_mode(context, frame, candidate_nodes)
        context.state.mode = mode
        context.record("mode_selected", mode=mode, plan_id=plan.plan_id)
        if mode == "single":
            _, local_faults = self._execute_operations(context, frame, self._ordered_execution_nodes(plan))
            faults += local_faults
            artifact = self._terminal_artifact(plan, context.state.artifacts)
            artifact, verifier_score, verified_terminal = self._resolve_terminal_progress(
                context,
                frame,
                task,
                plan,
                artifact,
                verifier_score,
                verified_terminal,
            )
            return artifact, faults, verifier_score, verified_terminal
        if mode == "vertical":
            context.state.execution_state = "running"
            children = self.runtime.topology.propose_children(context, frame, candidate_nodes)
            for child in children:
                agent = self._resolve_agent(context, child)
                tool_scope = self.runtime.topology.assign_scope(context, child, context.state.visible_tool_names)
                context.state.queue.append(
                    AgentFrame(
                        frame_id=stable_hash(plan.request_id, child.child_id, len(context.state.queue))[:16],
                        agent=agent,
                        request_id=plan.request_id,
                        plan_id=plan.plan_id,
                        trace_context=context.derive_trace_context(
                            agent_id=agent.agent_id,
                            frame_role=child.role,
                            worker_id=child.child_id,
                        ),
                        objective=child.instruction,
                        operation_ids=[child.init_summary.get("op_id", child.child_id)],
                        depth=frame.depth + 1,
                        parent_id=frame.agent.agent_id,
                        role=child.role,
                        tool_scope=tool_scope,
                        model_class=child.model_class,
                        metadata={"child_spec": (child).model_dump(), "parent_run_node_id": frame.metadata.get("run_node_id")},
                    )
                )
            self._schedule_root_continuation(context, frame, append=True)
            return artifact, faults, verifier_score, verified_terminal
        frontier_nodes = self._active_runnable_frontier(context, plan, branch_group_id=frame.branch_group_id)
        if len(frontier_nodes) < 2:
            _, local_faults = self._execute_operations(context, frame, frontier_nodes)
            faults += local_faults
            if self._all_outputs_present(plan, context.state.artifacts):
                final_artifact = self._terminal_artifact(plan, context.state.artifacts)
                artifact, verifier_score, verified_terminal = self._resolve_terminal_progress(
                    context,
                    frame,
                    task,
                    plan,
                    final_artifact,
                    verifier_score,
                    verified_terminal,
                )
            else:
                artifact = None
                self._queue_root_continuation(context, frame)
            return artifact, faults, verifier_score, verified_terminal
        restored_worker_outputs = self._restored_branch_frontier(context, frame)
        if restored_worker_outputs is None:
            branch_snapshots = self._restorable_branch_snapshots(context, frame)
            context.state.execution_state = "branching"
            if branch_snapshots:
                worker_outputs, local_faults = self._resume_horizontal_branches(
                    context,
                    frame,
                    task,
                    plan,
                    provider_usage_ledger,
                )
            else:
                workers = self.runtime.topology.select_workers(context, frame, frontier_nodes)
                worker_outputs, local_faults = self._execute_horizontal_branches(
                    context,
                    frame,
                    task,
                    plan,
                    workers,
                    provider_usage_ledger,
                )
            faults += local_faults
            if worker_outputs is None:
                context.state.execution_state = "running"
                _, local_faults = self._execute_operations(context, frame, frontier_nodes)
                faults += local_faults
                if self._all_outputs_present(plan, context.state.artifacts):
                    final_artifact = self._terminal_artifact(plan, context.state.artifacts)
                    artifact, verifier_score, verified_terminal = self._resolve_terminal_progress(
                        context,
                        frame,
                        task,
                        plan,
                        final_artifact,
                        verifier_score,
                        verified_terminal,
                    )
                else:
                    artifact = None
                    self._queue_root_continuation(context, frame)
                return artifact, faults, verifier_score, verified_terminal
        else:
            worker_outputs = restored_worker_outputs
            context.record(
                "branch_frontier_restored",
                parent_frame_id=frame.frame_id,
                branch_count=len(restored_worker_outputs),
            )
        merge_node = next(
            (
                node
                for node in plan.nodes
                if str(node.node_kind) == "merge"
                and str(node.metadata.get("consumes_branch_group", "") or "").strip() == str(frontier_nodes[0].branch_group_id or "")
            ),
            None,
        )
        if merge_node is None:
            raise HardInvalidation("branchable frontier is missing an explicit merge node")
        for node in frontier_nodes:
            context.state.plan_node_status[node.node_id] = "completed"
        context.state.worker_plans[merge_node.node_id] = {
            "worker_outputs": worker_outputs,
            "frontier_node_ids": [node.node_id for node in frontier_nodes],
            "parent_run_node_id": frame.metadata.get("run_node_id"),
        }
        context.state.execution_state = "running"
        self._queue_root_continuation(context, frame)
        return artifact, faults, verifier_score, verified_terminal
