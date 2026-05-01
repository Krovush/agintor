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

class BranchResultMixin:
    def _cancelled_branch_result(
        self,
        branch_plan: BranchPlan,
        branch_context: PolicyContext | None,
        unresolved_critical: int,
        *,
        reason: str,
        details: Mapping[str, Any],
    ) -> BranchResult:
        publications: list[BranchPublication] = []
        side_effect_receipts: list[SideEffectReceipt] = []
        budget_consumed: dict[str, Any] = {}
        if branch_context is not None:
            handle_updates: list[dict[str, Any]] = []
            receipt_updates: list[SideEffectReceipt] = []
            handles_by_id = branch_context.shell.open_handles.handles
            for handle_id in list(branch_context.state.open_handle_ids):
                handle = handles_by_id.get(handle_id)
                if handle is None:
                    raise HardInvalidation(
                        f"cancelled branch {branch_plan.branch_id} lost open handle {handle_id!r} during cleanup"
                    )
                if handle.state == "running":
                    if not hasattr(branch_context.shell.tool_executor, "cancel_async_handle"):
                        raise HardInvalidation(
                            f"cancelled branch {branch_plan.branch_id} cannot cancel running handle {handle_id!r}"
                        )
                    try:
                        branch_context.shell.tool_executor.cancel_async_handle(
                            handle.handle_id,
                            branch_context.shell.open_handles,
                        )
                    except Exception as exc:
                        raise HardInvalidation(
                            f"cancelled branch {branch_plan.branch_id} failed to clean up handle {handle_id!r}: {exc}"
                        ) from exc
                    handle = branch_context.shell.open_handles.get(handle.handle_id)
                if handle.state not in {"completed", "failed", "cancelled"}:
                    raise HardInvalidation(
                        f"cancelled branch {branch_plan.branch_id} left handle {handle_id!r} in non-terminal state {handle.state!r}"
                    )
                handle_updates.append(
                    {
                        "handle_id": handle.handle_id,
                        "tool_name": handle.tool_name,
                        "state": handle.state,
                    }
                )
            for receipt_payload in branch_context.state.side_effect_receipts:
                receipt = (SideEffectReceipt).model_validate(receipt_payload)
                if is_terminal_receipt(receipt):
                    receipt_updates.append(receipt)
                    continue
                if receipt.action_kind == "tool_launch":
                    result_ref = dict(receipt.result_ref or {})
                    handle_id = str(result_ref.get("handle_id", "")).strip()
                    launch_mode = str(result_ref.get("launch_mode", "")).strip().lower()
                    if not handle_id and launch_mode == "sync":
                        raise HardInvalidation(
                            f"cancelled branch {branch_plan.branch_id} cannot safely reconcile unresolved sync tool launch {receipt.side_effect_id!r}"
                        )
                    handle = handles_by_id.get(handle_id) if handle_id else None
                    if handle is None:
                        raise HardInvalidation(
                            f"cancelled branch {branch_plan.branch_id} cannot reconcile tool launch receipt {receipt.side_effect_id!r}"
                        )
                    if handle.state == "completed":
                        output = branch_context.shell.load_open_handle_output(handle)
                        receipt_updates.append(
                            terminalize_receipt(
                                receipt,
                                status="reconciled",
                                reconciliation_status="terminalized_from_handle",
                                reconciliation_source="branch_cancellation",
                                reconciliation_details={"handle_id": handle.handle_id, "handle_state": handle.state},
                                result_ref_updates={"handle_id": handle.handle_id, "output": output, "handle_state": handle.state},
                            )
                        )
                    elif handle.state == "failed":
                        receipt_updates.append(
                            terminalize_receipt(
                                receipt,
                                status="failed",
                                reconciliation_status="terminalized_from_handle",
                                reconciliation_source="branch_cancellation",
                                reconciliation_details={"handle_id": handle.handle_id, "handle_state": handle.state},
                                result_ref_updates={"handle_id": handle.handle_id, "handle_state": handle.state},
                            )
                        )
                    elif handle.state == "cancelled":
                        receipt_updates.append(
                            terminalize_receipt(
                                receipt,
                                status="abandoned",
                                reconciliation_status="abandoned_by_cancellation",
                                reconciliation_source="branch_cancellation",
                                reconciliation_details={"handle_id": handle.handle_id, "handle_state": handle.state, "reason": reason},
                                result_ref_updates={"handle_id": handle.handle_id, "handle_state": handle.state},
                            )
                        )
                    else:
                        raise HardInvalidation(
                            f"cancelled branch {branch_plan.branch_id} could not terminalize handle-backed receipt {receipt.side_effect_id!r}"
                        )
                    continue
                if receipt.action_kind == "provider_request":
                    receipt_updates.append(
                        terminalize_receipt(
                            receipt,
                            status="abandoned",
                            reconciliation_status="abandoned_by_cancellation",
                            reconciliation_source="branch_cancellation",
                            reconciliation_details={"reason": reason},
                            result_ref_updates={"abandoned_reason": reason},
                        )
                    )
                    continue
                if receipt.action_kind == "service_action":
                    receipt_updates.append(
                        terminalize_receipt(
                            receipt,
                            status="abandoned",
                            reconciliation_status="abandoned_by_cancellation",
                            reconciliation_source="branch_cancellation",
                            reconciliation_details={"reason": reason},
                            result_ref_updates={"abandoned_reason": reason},
                        )
                    )
                    continue
                raise HardInvalidation(
                    f"cancelled branch {branch_plan.branch_id} cannot safely reconcile side effect {receipt.side_effect_id!r}"
                )
            branch_context.state.side_effect_receipts = [(receipt).model_dump() for receipt in receipt_updates]
            branch_context.state.branch_publications = []
            branch_context.record(
                "branch_cancelled",
                branch_id=branch_plan.branch_id,
                frame_id=getattr(branch_context.active_frame, "frame_id", branch_plan.parent_frame_id),
                reason=reason,
                details=dict(details),
                unresolved_critical=unresolved_critical,
                handles=handle_updates,
            )
            self._emit_branch_publication(
                branch_context,
                publication_kind="cleanup_record",
                logical_key=f"{branch_plan.branch_id}.cancelled.cleanup",
                payload={
                    "reason": reason,
                    "details": dict(details),
                    "handles": handle_updates,
                },
                unresolved_critical=unresolved_critical,
                allow_when_cancelled=True,
            )
            for receipt in receipt_updates:
                if not is_terminal_receipt(receipt):
                    raise HardInvalidation(
                        f"cancelled branch {branch_plan.branch_id} left side effect {receipt.side_effect_id!r} unresolved"
                    )
                branch_context.record(
                    "side_effect_reconciled",
                    branch_id=branch_plan.branch_id,
                    side_effect_id=receipt.side_effect_id,
                    action_kind=receipt.action_kind,
                    status=receipt.status,
                )
                self._emit_branch_publication(
                    branch_context,
                    publication_kind="reconciliation_record",
                    logical_key=f"{branch_plan.branch_id}.receipt.{receipt.side_effect_id}",
                    payload={
                        "side_effect_id": receipt.side_effect_id,
                        "action_kind": receipt.action_kind,
                        "status": receipt.status,
                    },
                    unresolved_critical=unresolved_critical,
                    allow_when_cancelled=True,
                )
            publications = [
                (BranchPublication).model_validate(payload)
                for payload in branch_context.state.branch_publications
            ]
            side_effect_receipts = receipt_updates
            budget_consumed = {
                "cost": branch_context.budget.cost,
                "latency": branch_context.budget.latency,
                "model_calls": branch_context.budget.calls,
                "checks": branch_context.budget.checks,
                "tokens": branch_context.budget.tokens,
                "input_tokens": branch_context.budget.input_tokens,
                "output_tokens": branch_context.budget.output_tokens,
                "created_tools": branch_context.state.created_tools,
                "promoted_nodes": branch_context.state.promoted_nodes,
            }
        return BranchResult(
            branch_plan=branch_plan,
            branch_state=BranchState(
                branch_id=branch_plan.branch_id,
                status="cancelled",
                parent_frame_id=branch_plan.parent_frame_id,
                assigned_node_ids=list(branch_plan.assigned_node_ids),
                merge_priority=branch_plan.merge_priority,
                predicted_solve=branch_plan.predicted_solve,
                reserved_budget=branch_plan.reserved_budget,
                publications=publications,
                budget_consumed=budget_consumed,
                unresolved_critical=unresolved_critical,
                cancellation_record=CancellationRecord(
                    reason=reason,
                    details=dict(details),
                    created_at=now_ts(),
                ),
            ),
            artifact=None,
            verifier_support=0.0,
            unresolved_critical=unresolved_critical,
            side_effect_receipts=side_effect_receipts,
        )
