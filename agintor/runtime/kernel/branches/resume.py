from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from threading import Event, Lock
from typing import Any, Mapping, Sequence
from ....core.exceptions import BranchCancelled, HardInvalidation, ProviderExhaustedError, ResumeRecoveryError
from ....providers import (
    ModelProvider,
    ReplayProvider,
    clone_provider,
    known_provider_environment_names,
    provider_environment_names,
    provider_environment_names_for_instance,
)
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
    SideEffectReceipt,
    capability_scope_allows,
    plan_node_requires_default_provider,
    service_action_transport_compatibility,
    is_terminal_receipt,
    terminalize_receipt,
)
from ....utils import count_tokens_rough, ensure_directory, merge_provider_usage, now_ts, stable_hash

class BranchResumeMixin:
    def _resume_horizontal_branches(
        self,
        context: PolicyContext,
        frame: AgentFrame,
        task: BenchmarkTask,
        plan: ExecutionPlan,
        provider_usage_ledger: dict[str, Any],
        *,
        reconciliation_policy: str = "strict",
    ) -> tuple[list[dict[str, Any]], int]:
        terminal_results: list[BranchResult] = []
        for payload in context.state.branch_states.values():
            branch_state = (BranchState).model_validate(payload)
            if branch_state.parent_frame_id != frame.frame_id:
                continue
            if branch_state.status not in {"completed", "cancelled", "failed"}:
                continue
            publications = branch_state.publications or [
                (BranchPublication).model_validate(publication_payload)
                for publication_payload in context.state.branch_publications
                if str(publication_payload.get("branch_id", "") or "") == branch_state.branch_id
            ]
            branch_plan = BranchPlan(
                branch_id=branch_state.branch_id,
                parent_frame_id=branch_state.parent_frame_id,
                request_id=context.request_id,
                trace_context=(
                    publications[0].trace_context if publications else context.derive_trace_context(worker_id=branch_state.branch_id)
                ),
                assigned_node_ids=list(branch_state.assigned_node_ids),
                merge_priority=branch_state.merge_priority,
                predicted_solve=branch_state.predicted_solve,
                reserved_budget=branch_state.reserved_budget,
                cancel_on_parent_stop=True,
            )
            publication = self._candidate_artifact_publication(publications, branch_state.branch_id)
            terminal_results.append(
                BranchResult(
                    branch_plan=branch_plan,
                    branch_state=branch_state,
                    artifact=publication.payload.get("artifact") if publication is not None else None,
                    verifier_support=branch_state.verifier_support,
                    unresolved_critical=branch_state.unresolved_critical,
                    side_effect_receipts=[
                        (SideEffectReceipt).model_validate(receipt_payload)
                        for receipt_payload in context.state.side_effect_receipts
                        if str(receipt_payload.get("branch_id", "") or "") == branch_state.branch_id
                    ],
                )
            )
        branch_snapshots = self._restorable_branch_snapshots(context, frame)
        if not branch_snapshots:
            return self._apply_branch_group_results(
                context,
                frame,
                task,
                plan,
                terminal_results,
                provider_usage_ledger,
            )
        cancellation_event = Event()
        persist_lock = Lock()
        branch_results = list(terminal_results)
        propagated_resume_error: ResumeRecoveryError | None = None
        context.state.active_branch_count = len(branch_snapshots)
        prepared_branches = []
        for snapshot in branch_snapshots:
            prepared_branch_plan, branch_provider = self._prepare_branch_provider(
                plan,
                (BranchPlan).model_validate((snapshot.branch_plan).model_dump()),
                snapshot,
            )
            prepared_branches.append((snapshot, prepared_branch_plan, branch_provider))
        executor = ThreadPoolExecutor(max_workers=len(prepared_branches), thread_name_prefix=f"branch-resume-{plan.plan_id[:8]}")
        try:
            provider_overrides = self._branch_provider_overrides()
            future_map = {
                provider_overrides.__setitem__(prepared_branch_plan.branch_id, branch_provider) or
                executor.submit(
                    self._run_branch_plan,
                    context,
                    task,
                    plan,
                    prepared_branch_plan,
                    cancellation_event,
                    persist_lock,
                    snapshot,
                    reconciliation_policy,
                ): prepared_branch_plan
                for snapshot, prepared_branch_plan, branch_provider in prepared_branches
            }
            pending = set(future_map)
            sibling_cancellation_reason: str | None = None
            sibling_cancellation_details: dict[str, Any] = {}

            def cancel_pending_siblings() -> None:
                for pending_future in list(pending):
                    pending_branch_plan = future_map[pending_future]
                    if not pending_future.cancel():
                        continue
                    branch_results.append(
                        self._cancelled_branch_result(
                            pending_branch_plan,
                            None,
                            len(pending_branch_plan.assigned_node_ids),
                            reason=sibling_cancellation_reason or "fatal_branch_fault",
                            details=dict(sibling_cancellation_details),
                        )
                    )
                    pending.remove(pending_future)

            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    branch_plan = future_map[future]
                    if future.cancelled():
                        branch_results.append(
                            self._cancelled_branch_result(
                                branch_plan,
                                None,
                                len(branch_plan.assigned_node_ids),
                                reason=sibling_cancellation_reason or "fatal_branch_fault",
                                details=dict(sibling_cancellation_details),
                            )
                        )
                        continue
                    try:
                        branch_result = future.result()
                    except ResumeRecoveryError as exc:
                        if propagated_resume_error is None:
                            propagated_resume_error = exc
                            sibling_cancellation_reason = "fatal_branch_fault"
                            sibling_cancellation_details = {
                                "error": str(exc),
                                "failure_kind": exc.failure_kind,
                            }
                            setattr(cancellation_event, "reason", sibling_cancellation_reason)
                            setattr(cancellation_event, "details", dict(sibling_cancellation_details))
                            cancellation_event.set()
                            cancel_pending_siblings()
                        context.record(
                            "branch_failed",
                            branch_id=branch_plan.branch_id,
                            frame_id=frame.frame_id,
                            assigned_node_ids=list(branch_plan.assigned_node_ids),
                            error=str(exc),
                            failure_kind=exc.failure_kind,
                        )
                        continue
                    branch_results.append(branch_result)
                    if branch_result.branch_state.status == "failed" and sibling_cancellation_reason is None:
                        sibling_cancellation_reason = self._failed_branch_cancellation_reason(
                            branch_result.branch_state.failure_kind
                        )
                        sibling_cancellation_details = {
                            "failed_branch_id": branch_result.branch_plan.branch_id,
                            "failure_kind": branch_result.branch_state.failure_kind,
                        }
                        if branch_result.branch_state.error:
                            sibling_cancellation_details["error"] = branch_result.branch_state.error
                        setattr(cancellation_event, "reason", sibling_cancellation_reason)
                        setattr(cancellation_event, "details", dict(sibling_cancellation_details))
                        cancellation_event.set()
                        cancel_pending_siblings()
        finally:
            context.state.active_branch_count = 0
            provider_overrides = self._branch_provider_overrides()
            for _, prepared_branch_plan, _ in prepared_branches:
                provider_overrides.pop(prepared_branch_plan.branch_id, None)
            executor.shutdown(wait=True, cancel_futures=True)
        return self._apply_branch_group_results(
            context,
            frame,
            task,
            plan,
            branch_results,
            provider_usage_ledger,
            propagated_resume_error=propagated_resume_error,
        )
