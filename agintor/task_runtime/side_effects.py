from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence
from ..exceptions import BranchCancelled, HardInvalidation, ProviderExhaustedError, ResumeRecoveryError
from ..runtime_api import (
    AgentFrame,
    PolicyContext,
    RuntimeBudget,
    RuntimeState,
    compile_execution_plan_from_task,
    get_plan_node_descriptor,
    normalize_benchmark_request_id,
)
from ..pydantic_compat import model_copy, model_dump, model_validate
from ..schemas import (
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
from ..utils import count_tokens_rough, ensure_directory, merge_provider_usage, now_ts, stable_hash


class SideEffectsMixin:
    def _persist_side_effect_receipt(self, receipt: SideEffectReceipt) -> None:
        self.shell.save_side_effect_receipt(model_validate(SideEffectReceipt, receipt))

    def _record_side_effect_receipt(self, context: PolicyContext, receipt: SideEffectReceipt) -> None:
        normalized = model_validate(SideEffectReceipt, receipt)
        self.shell.save_side_effect_receipt(normalized)
        deduped: list[dict[str, Any]] = []
        for payload in context.state.side_effect_receipts:
            same_idempotency = str(payload.get("idempotency_key", "")) == normalized.idempotency_key
            same_kind = str(payload.get("action_kind", "")) == normalized.action_kind
            if same_idempotency and same_kind and is_terminal_receipt(normalized):
                continue
            deduped.append(payload)
        deduped.append(model_dump(normalized))
        context.state.side_effect_receipts = deduped

    def _reconcile_side_effect_receipts(
        self,
        context: PolicyContext,
        receipts: Sequence[SideEffectReceipt],
        *,
        reconciliation_policy: str,
    ) -> tuple[list[SideEffectReceipt], set[str]]:
        resolved: list[SideEffectReceipt] = []
        blocked_node_ids: set[str] = set()
        resolved_terminal_keys: set[tuple[str, str]] = set()

        def append_resolved(receipt: SideEffectReceipt) -> None:
            terminal_key = (
                str(receipt.action_kind or ""),
                str(receipt.idempotency_key or ""),
            )
            if is_terminal_receipt(receipt):
                if terminal_key in resolved_terminal_keys:
                    return
                resolved_terminal_keys.add(terminal_key)
            resolved.append(receipt)

        terminal_by_key = {
            receipt.idempotency_key: receipt
            for receipt in receipts
            if is_terminal_receipt(receipt)
        }
        for receipt in receipts:
            if is_terminal_receipt(receipt):
                append_resolved(receipt)
                if receipt.status in {"failed", "abandoned"} and receipt.node_id:
                    blocked_node_ids.add(receipt.node_id)
                continue
            terminal_receipt = terminal_by_key.get(receipt.idempotency_key)
            if terminal_receipt is not None:
                append_resolved(terminal_receipt)
                if terminal_receipt.status in {"failed", "abandoned"} and terminal_receipt.node_id:
                    blocked_node_ids.add(terminal_receipt.node_id)
                context.record(
                    "side_effect_reconciled",
                    side_effect_id=terminal_receipt.side_effect_id,
                    action_kind=terminal_receipt.action_kind,
                    reconciliation_status="reused_terminal_receipt",
                )
                continue
            if receipt.action_kind == "tool_launch":
                result_ref = dict(receipt.result_ref or {})
                handle_id = str(result_ref.get("handle_id", "")).strip()
                launch_mode = str(result_ref.get("launch_mode", "")).strip().lower()
                handle = self.shell.open_handles.handles.get(handle_id) if handle_id else None
                if handle is not None:
                    reconciliation = (
                        self.shell.tool_executor.reconcile_async_handle(handle, self.shell.open_handles)
                        if hasattr(self.shell.tool_executor, "reconcile_async_handle")
                        else None
                    )
                    handle_status = str((reconciliation or {}).get("status", handle.state) or handle.state)
                    if handle_status in {"completed", "failed", "cancelled"}:
                        terminalized = terminalize_receipt(
                            receipt,
                            status="failed" if handle_status == "failed" else "reconciled" if handle_status == "completed" else "abandoned",
                            reconciliation_status="terminalized_from_handle",
                            reconciliation_source="resume_reconciliation",
                            reconciliation_details={
                                "handle_id": handle.handle_id,
                                "handle_state": handle_status,
                                "reconciliation_source": (reconciliation or {}).get("reconciliation_source"),
                            },
                            result_ref_updates={
                                "handle_id": handle.handle_id,
                                "output": (reconciliation or {}).get("output"),
                                "handle_state": handle_status,
                            },
                        )
                        append_resolved(terminalized)
                        if terminalized.status in {"failed", "abandoned"} and terminalized.node_id:
                            blocked_node_ids.add(terminalized.node_id)
                        context.record(
                            "side_effect_reconciled",
                            side_effect_id=terminalized.side_effect_id,
                            action_kind=terminalized.action_kind,
                            reconciliation_status=terminalized.reconciliation.status if terminalized.reconciliation else None,
                        )
                        continue
                    if launch_mode == "async":
                        if reconciliation_policy == "strict":
                            raise ResumeRecoveryError(
                                RecoveryFailureKind.RECEIPT_RECONCILIATION_FAILED.value,
                                f"strict resume requires durable reconciliation for async tool launch {receipt.side_effect_id}",
                            )
                        if receipt.node_id:
                            blocked_node_ids.add(receipt.node_id)
                        append_resolved(receipt)
                        context.record(
                            "side_effect_reconciled",
                            side_effect_id=receipt.side_effect_id,
                            action_kind=receipt.action_kind,
                            reconciliation_status="blocked_best_effort",
                        )
                        continue
                if launch_mode == "sync":
                    if reconciliation_policy == "strict":
                        raise ResumeRecoveryError(
                            RecoveryFailureKind.RECEIPT_RECONCILIATION_FAILED.value,
                            f"strict resume requires terminal proof for sync tool launch {receipt.side_effect_id}",
                        )
                    if receipt.node_id:
                        blocked_node_ids.add(receipt.node_id)
                    terminalized = terminalize_receipt(
                        receipt,
                        status="abandoned",
                        reconciliation_status="blocked_best_effort",
                        reconciliation_source="resume_reconciliation",
                        reconciliation_details={
                            "node_id": receipt.node_id,
                            "action_kind": receipt.action_kind,
                            "launch_mode": launch_mode,
                        },
                    )
                    append_resolved(terminalized)
                    context.record(
                        "side_effect_reconciled",
                        side_effect_id=terminalized.side_effect_id,
                        action_kind=terminalized.action_kind,
                        reconciliation_status="blocked_best_effort",
                    )
                    continue
            if receipt.action_kind == "provider_request" and hasattr(self.provider, "reconcile_request"):
                reconciled = self.provider.reconcile_request(receipt.idempotency_key, receipt)
                if reconciled is not None:
                    terminalized = terminalize_receipt(
                        receipt,
                        status="reconciled",
                        reconciliation_status="terminalized_from_provider_hook",
                        reconciliation_source="resume_reconciliation",
                        reconciliation_details={"idempotency_key": receipt.idempotency_key},
                        result_ref_updates=model_dump(reconciled) if hasattr(reconciled, "model_dump") else dict(reconciled),
                    )
                    append_resolved(terminalized)
                    context.record(
                        "side_effect_reconciled",
                        side_effect_id=terminalized.side_effect_id,
                        action_kind=terminalized.action_kind,
                        reconciliation_status=terminalized.reconciliation.status if terminalized.reconciliation else None,
                    )
                    continue
            if receipt.action_kind == "filesystem_write":
                reconciliation_state = self._filesystem_write_reconciliation_state(receipt)
                if reconciliation_state == "completed":
                    reconciled_receipt = receipt.copy(update={"status": "reconciled"}, deep=True)
                    append_resolved(reconciled_receipt)
                    context.record(
                        "side_effect_reconciled",
                        side_effect_id=reconciled_receipt.side_effect_id,
                        action_kind=reconciled_receipt.action_kind,
                        reconciliation_status="filesystem_state_matches_intent",
                    )
                    continue
                if reconciliation_state == "prewrite_intact":
                    append_resolved(receipt)
                    context.record(
                        "side_effect_reconciled",
                        side_effect_id=receipt.side_effect_id,
                        action_kind=receipt.action_kind,
                        reconciliation_status="filesystem_prewrite_state_intact",
                    )
                    continue
                if reconciliation_policy == "strict":
                    raise ResumeRecoveryError(
                        RecoveryFailureKind.RECEIPT_RECONCILIATION_FAILED.value,
                        f"strict resume requires filesystem writes to be fully reconciled or fully unapplied before reissue; unresolved receipt {receipt.side_effect_id}",
                    )
                if receipt.node_id:
                    blocked_node_ids.add(receipt.node_id)
                terminalized = terminalize_receipt(
                    receipt,
                    status="abandoned",
                    reconciliation_status="blocked_best_effort",
                    reconciliation_source="resume_reconciliation",
                    reconciliation_details={"node_id": receipt.node_id, "action_kind": receipt.action_kind},
                )
                append_resolved(terminalized)
                context.record(
                    "side_effect_reconciled",
                    side_effect_id=terminalized.side_effect_id,
                    action_kind=terminalized.action_kind,
                    reconciliation_status="blocked_best_effort",
                )
                continue
            if receipt.action_kind == "service_action":
                if reconciliation_policy == "strict":
                    raise ResumeRecoveryError(
                        RecoveryFailureKind.RECEIPT_RECONCILIATION_FAILED.value,
                        f"strict resume requires terminal proof for service action {receipt.side_effect_id}",
                    )
                if receipt.node_id:
                    blocked_node_ids.add(receipt.node_id)
                terminalized = terminalize_receipt(
                    receipt,
                    status="abandoned",
                    reconciliation_status="blocked_best_effort",
                    reconciliation_source="resume_reconciliation",
                    reconciliation_details={
                        "node_id": receipt.node_id,
                        "action_kind": receipt.action_kind,
                    },
                )
                append_resolved(terminalized)
                context.record(
                    "side_effect_reconciled",
                    side_effect_id=terminalized.side_effect_id,
                    action_kind=terminalized.action_kind,
                    reconciliation_status="blocked_best_effort",
                )
                continue
            if reconciliation_policy == "strict":
                raise ResumeRecoveryError(
                    RecoveryFailureKind.RECEIPT_RECONCILIATION_FAILED.value,
                    f"strict resume requires reconciled side effects; unresolved receipt {receipt.side_effect_id}",
                )
            if receipt.node_id:
                blocked_node_ids.add(receipt.node_id)
            terminalized = terminalize_receipt(
                receipt,
                status="abandoned",
                reconciliation_status="blocked_best_effort",
                reconciliation_source="resume_reconciliation",
                reconciliation_details={"node_id": receipt.node_id, "action_kind": receipt.action_kind},
            )
            append_resolved(terminalized)
            context.record(
                "side_effect_reconciled",
                side_effect_id=terminalized.side_effect_id,
                action_kind=terminalized.action_kind,
                reconciliation_status="blocked_best_effort",
            )
        return resolved, blocked_node_ids

    @staticmethod
    def _filesystem_write_matches_entry(entry: Mapping[str, Any], *, expected_state: str) -> bool:
        raw_path = str(entry.get("path", "") or "").strip()
        if not raw_path:
            return False
        path = Path(raw_path).expanduser()
        expected_exists = bool(entry.get(f"{expected_state}_exists"))
        if path.exists() != expected_exists:
            return False
        if not expected_exists:
            return True
        try:
            current_content = path.read_text(encoding="utf-8")
        except Exception:
            return False
        expected_digest = str(entry.get(f"{expected_state}_digest", "") or "").strip()
        return bool(expected_digest) and stable_hash(current_content) == expected_digest

    def _filesystem_write_reconciliation_state(self, receipt: SideEffectReceipt) -> str:
        writes = [
            dict(entry)
            for entry in receipt.result_ref.get("writes", [])
            if isinstance(entry, Mapping)
        ]
        if not writes:
            return "unknown"
        if all(self._filesystem_write_matches_entry(entry, expected_state="after") for entry in writes):
            return "completed"
        if all(self._filesystem_write_matches_entry(entry, expected_state="before") for entry in writes):
            return "prewrite_intact"
        return "ambiguous"
