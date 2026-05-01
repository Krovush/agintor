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

class CheckpointRecoveryMixin:
    def _record_recovery_attempt(
        self,
        context: PolicyContext,
        checkpoint_envelope: CheckpointEnvelope,
        *,
        selected_checkpoint_ref: str,
        reconciliation_policy: str,
        current_fingerprint: EnvironmentFingerprint,
        source_fingerprint: EnvironmentFingerprint | None,
        receipts: Sequence[SideEffectReceipt],
        blocked_node_ids: Sequence[str] | set[str],
    ) -> None:
        if getattr(self.shell, "run_store", None) is None:
            return
        deltas = self._fingerprint_deltas(source_fingerprint, current_fingerprint)
        blocked_nodes = sorted(str(node_id) for node_id in blocked_node_ids if str(node_id).strip())
        compatibility_result = (
            "exact_compatible"
            if source_fingerprint is not None and not deltas and not blocked_nodes
            else "degraded_compatible"
        )
        recovery = RecoveryAttempt(
            recovery_attempt_id=f"recovery.{stable_hash(selected_checkpoint_ref, context.request_id, now_ts())[:16]}",
            run_id=str(getattr(self.shell, "run_id", "") or checkpoint_envelope.run_id),
            attempt_id=str(getattr(self.shell, "attempt_id", "") or ""),
            selected_checkpoint_ref=selected_checkpoint_ref,
            source_checkpoint_ref=selected_checkpoint_ref or None,
            origin_request_id=checkpoint_envelope.origin_request_id,
            rebound_request_id=context.request_id,
            reconciliation_policy=reconciliation_policy if reconciliation_policy in {"strict", "best_effort"} else "strict",
            compatibility_result=compatibility_result,
            source_fingerprint_id=getattr(source_fingerprint, "fingerprint_id", None),
            current_fingerprint_id=current_fingerprint.fingerprint_id,
            fingerprint_deltas=deltas,
            receipts_reused=[
                receipt.side_effect_id
                for receipt in receipts
                if receipt.status in {"completed", "reconciled"} and str(receipt.side_effect_id).strip()
            ],
            receipts_blocked=[
                receipt.side_effect_id
                for receipt in receipts
                if receipt.node_id in blocked_nodes and str(receipt.side_effect_id).strip()
            ],
            blocked_node_ids=blocked_nodes,
            degraded_plan_node_ids=blocked_nodes if compatibility_result == "degraded_compatible" else [],
            resume_explanation=(
                "resume restored with matching environment fingerprint"
                if compatibility_result == "exact_compatible"
                else "resume restored with environment fingerprint differences, blocked receipts, or missing source fingerprint"
            ),
            attempted_at=now_ts(),
            completed_at=now_ts(),
        )
        self.shell.run_store.write_recovery_attempt(self.shell.run_root, recovery)

    def _record_failed_recovery_attempt(
        self,
        context: PolicyContext,
        checkpoint_envelope: CheckpointEnvelope,
        *,
        selected_checkpoint_ref: str,
        reconciliation_policy: str,
        failure_explanation: str,
    ) -> None:
        if getattr(self.shell, "run_store", None) is None:
            return
        try:
            current_fingerprint = self._capture_environment_fingerprint(
                context,
                source_checkpoint_ref=selected_checkpoint_ref or None,
            )
            recovery = RecoveryAttempt(
                recovery_attempt_id=f"recovery.{stable_hash(selected_checkpoint_ref, context.request_id, failure_explanation, now_ts())[:16]}",
                run_id=str(getattr(self.shell, "run_id", "") or checkpoint_envelope.run_id),
                attempt_id=str(getattr(self.shell, "attempt_id", "") or ""),
                selected_checkpoint_ref=selected_checkpoint_ref,
                source_checkpoint_ref=selected_checkpoint_ref or None,
                origin_request_id=checkpoint_envelope.origin_request_id,
                rebound_request_id=context.request_id,
                reconciliation_policy=reconciliation_policy if reconciliation_policy in {"strict", "best_effort"} else "strict",
                compatibility_result="fail_closed",
                current_fingerprint_id=current_fingerprint.fingerprint_id,
                resume_explanation=failure_explanation,
                attempted_at=now_ts(),
                completed_at=now_ts(),
            )
            self.shell.run_store.write_recovery_attempt(self.shell.run_root, recovery)
        except Exception:
            return
