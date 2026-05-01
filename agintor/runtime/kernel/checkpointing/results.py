from __future__ import annotations

import time
from typing import Any, Mapping, Sequence
from ....core.exceptions import BranchCancelled, HardInvalidation, ProviderExhaustedError, ResumeRecoveryError
from ....tracing import resolve_trace_session_id, trace_grouping_key, trace_session_dir_name
from ...api import (
    AgentFrame,
    PolicyContext,
    RuntimeBudget,
    RuntimeState,
    compile_execution_plan_from_task,
    get_plan_node_descriptor,
    normalize_benchmark_request_id,
)
from ....contracts import (
    CHECKPOINT_ENVELOPE_SCHEMA_VERSION,
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
    EnvironmentFingerprint,
    ExecutionPlan,
    FingerprintDelta,
    MemoryNode,
    OpenAITraceContext,
    PlanNode,
    QueuedAgentSnapshot,
    QueuedFrameSnapshot,
    RecoveryFailureKind,
    RecoveryAttempt,
    ReceiptReconciliationRecord,
    ReplayAllocation,
    RunResult,
    SideEffectReceipt,
    TraceCursorSnapshot,
    VerifiedFactRef,
    WorkingMemorySnapshot,
    capability_scope_allows,
    plan_node_requires_default_provider,
    service_action_transport_compatibility,
    is_terminal_receipt,
    terminalize_receipt,
)
from ....utils import count_tokens_rough, ensure_directory, merge_provider_usage, now_ts, stable_hash

class CheckpointResultMixin:
    def _build_run_result(
        self,
        task: BenchmarkTask,
        plan: ExecutionPlan,
        seed: int,
        artifact: Any,
        verifier_score: float,
        faults: int,
        start: float,
        budget: RuntimeBudget,
        state: RuntimeState,
        trace: list[dict[str, Any]],
        hard_invalid: bool,
        invalid_reason: str | None,
        failure_kind: str | None,
        *,
        provider_usage: Mapping[str, Any] | None,
    ) -> RunResult:
        canonical_trace = [
            event.trace_row()
            for event in self.shell.load_runtime_events(
                request_id=plan.request_id,
                after_sequence_no=state.event_sequence_start,
            )
        ]
        persisted_trace = canonical_trace or [dict(row) for row in trace]
        trace_path = self.shell.save_trace(task.task_id, seed, persisted_trace)
        latest_checkpoint_ref = state.latest_checkpoint_ref or self.shell.latest_checkpoint_ref(
            getattr(self.shell, "run_id", "") or plan.request_id
        )
        run_lifecycle_state = "completed"
        if state.execution_state == "cancelled":
            run_lifecycle_state = "cancelled"
        elif state.execution_state == "failed":
            run_lifecycle_state = "paused" if latest_checkpoint_ref else "failed"
        elif hard_invalid and not latest_checkpoint_ref:
            run_lifecycle_state = "failed"
        elif hard_invalid and latest_checkpoint_ref:
            run_lifecycle_state = "paused"
        return RunResult(
            request_id=plan.request_id,
            plan_id=plan.plan_id,
            run_id=getattr(self.shell, "run_id", ""),
            run_root=str(getattr(self.shell, "run_root", self.shell.workspace)),
            attempt_id=getattr(self.shell, "attempt_id", ""),
            runtime_hash=self.runtime.runtime_hash,
            runtime_backend=self.runtime_backend,
            latest_checkpoint_ref=latest_checkpoint_ref,
            run_lifecycle_state=run_lifecycle_state,
            run_resumable=bool(latest_checkpoint_ref),
            run_prune_eligible=bool(run_lifecycle_state == "failed" and not latest_checkpoint_ref),
            task_id=task.task_id,
            seed=seed,
            artifact=artifact,
            verifier_score=verifier_score,
            cost=budget.cost,
            latency=time.perf_counter() - start,
            faults=faults,
            trace=persisted_trace,
            trace_context=plan.trace_context,
            trace_path=str(trace_path) if trace_path is not None else None,
            checkpoint_ref=latest_checkpoint_ref,
            hard_invalid=hard_invalid,
            invalid_reason=invalid_reason,
            failure_kind=failure_kind if hard_invalid else None,
            mode=state.mode,
            lifecycle_state=state.execution_state,
            created_tools=state.created_tools,
            promoted_nodes=state.promoted_nodes,
            checks_used=state.checks_used,
            model_calls=budget.calls,
            tokens_used=budget.tokens,
            input_tokens=budget.input_tokens,
            output_tokens=budget.output_tokens,
            provider_usage=dict(provider_usage or {}),
        )
