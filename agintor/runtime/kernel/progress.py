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

class ProgressMixin:
    def _stop_policy_requires_cancellation(
        self,
        plan: ExecutionPlan,
        *,
        artifact: Any,
        unresolved: Sequence[str],
        verified_terminal: bool,
    ) -> bool:
        terminal_ready = verified_terminal or not plan.execution_flags.requires_terminal_verification
        if unresolved:
            return True
        if artifact is None:
            return True
        return not terminal_ready

    def _assert_terminal_exit_contract(self, context: PolicyContext) -> None:
        final_state = str(context.state.execution_state or "").strip()
        expected_event = {
            "completed": "terminal_emitted",
            "failed": "run_failed",
            "cancelled": "run_cancelled",
        }.get(final_state)
        if expected_event is None:
            raise HardInvalidation(f"illegal terminal execution state {final_state!r}")
        terminal_events = [
            str(row.get("event", "") or "")
            for row in context.trace
            if str(row.get("event", "") or "") in {"terminal_emitted", "run_failed", "run_cancelled"}
        ]
        if len(terminal_events) != 1 or terminal_events[0] != expected_event:
            raise HardInvalidation(
                f"terminal exit contract violated for {context.request_id}: state={final_state!r}, events={terminal_events!r}"
            )

    def _update_subgoal_progress(
        self,
        context: PolicyContext,
        unresolved: Sequence[str],
        best_optimistic: float,
        previous_best_utility: float,
        verified_terminal: bool,
    ) -> None:
        current_goal = unresolved[0] if unresolved else None
        previous_goal = context.state.last_unresolved_goal
        if previous_goal and previous_goal not in unresolved:
            context.state.subgoal_negative_steps.pop(previous_goal, None)
        if current_goal is None or verified_terminal:
            context.state.last_unresolved_goal = None
            return
        if current_goal == previous_goal and best_optimistic < previous_best_utility:
            context.state.subgoal_negative_steps[current_goal] = context.state.subgoal_negative_steps.get(current_goal, 0) + 1
        elif current_goal != previous_goal:
            context.state.subgoal_negative_steps[current_goal] = 0
        context.state.last_unresolved_goal = current_goal
