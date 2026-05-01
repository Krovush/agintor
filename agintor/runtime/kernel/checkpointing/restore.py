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

class CheckpointRestoreMixin:
    def _selected_resume_checkpoint_ref(self, checkpoint_envelope: CheckpointEnvelope) -> str:
        snapshot = getattr(checkpoint_envelope, "runtime_state_snapshot", None)
        candidates = [
            getattr(checkpoint_envelope, "selected_checkpoint_ref", None),
            getattr(checkpoint_envelope, "source_checkpoint_ref", None),
            getattr(snapshot, "latest_checkpoint_ref", None),
        ]
        for candidate in candidates:
            selected = str(candidate or "").strip()
            if selected:
                return selected
        return ""

    def _restore_from_checkpoint(
        self,
        context: PolicyContext,
        checkpoint_envelope: CheckpointEnvelope,
        *,
        reconciliation_policy: str,
    ) -> None:
        selected_checkpoint_ref = self._selected_resume_checkpoint_ref(checkpoint_envelope)
        current_fingerprint = self._capture_environment_fingerprint(
            context,
            source_checkpoint_ref=selected_checkpoint_ref or None,
        )
        source_fingerprint = None
        if getattr(self.shell, "run_store", None) is not None:
            source_fingerprint = self.shell.run_store.load_environment_fingerprint(
                checkpoint_envelope.run_root or self.shell.run_root,
                checkpoint_envelope.environment_fingerprint_id,
            )
        if checkpoint_envelope.request_id != context.request_id:
            raise ResumeRecoveryError(
                RecoveryFailureKind.REQUEST_MISMATCH.value,
                f"checkpoint request mismatch: expected {context.request_id}, found {checkpoint_envelope.request_id}",
            )
        if checkpoint_envelope.plan_id != context.plan.plan_id:
            raise ResumeRecoveryError(
                RecoveryFailureKind.REQUEST_MISMATCH.value,
                f"checkpoint plan mismatch: expected {context.plan.plan_id}, found {checkpoint_envelope.plan_id}",
            )
        if checkpoint_envelope.checkpoint_schema_version != CHECKPOINT_ENVELOPE_SCHEMA_VERSION:
            raise ResumeRecoveryError(
                RecoveryFailureKind.RUNTIME_CONTRACT_MISMATCH.value,
                "checkpoint envelope schema does not match the loaded runtime",
            )
        if checkpoint_envelope.runtime_contract_version != self.runtime.kernel_manifest.runtime_contract_version:
            raise ResumeRecoveryError(
                RecoveryFailureKind.RUNTIME_CONTRACT_MISMATCH.value,
                "checkpoint runtime contract does not match the loaded runtime",
            )
        if checkpoint_envelope.runtime_hash != self.runtime.runtime_hash:
            raise ResumeRecoveryError(
                RecoveryFailureKind.RUNTIME_HASH_MISMATCH.value,
                "checkpoint runtime hash does not match the loaded runtime",
            )
        plan_snapshot = (ExecutionPlan).model_validate(checkpoint_envelope.plan_snapshot)
        if plan_snapshot.plan_digest != context.plan.plan_digest:
            raise ResumeRecoveryError(
                RecoveryFailureKind.PLAN_DIGEST_MISMATCH.value,
                "checkpoint plan digest does not match the compiled execution plan",
            )
        self.shell.restore_checkpoint_shell_state(checkpoint_envelope.shell_state_snapshot)
        self._restore_runtime_state_snapshot(context, checkpoint_envelope.runtime_state_snapshot)
        ledger_receipts = [
            (SideEffectReceipt).model_validate(receipt)
            for receipt in checkpoint_envelope.side_effect_ledger.get("receipts", [])
        ]
        root_receipts = [
            receipt
            for receipt in ledger_receipts
            if not str(receipt.branch_id or "").strip()
        ]
        branch_receipts = [
            receipt
            for receipt in ledger_receipts
            if str(receipt.branch_id or "").strip()
        ]
        receipts, blocked_node_ids = self._reconcile_side_effect_receipts(
            context,
            root_receipts,
            reconciliation_policy=reconciliation_policy,
        )
        self._restore_completed_nodes_from_receipts(context, receipts, branch_id=None)
        context.state.side_effect_receipts = [
            (receipt).model_dump()
            for receipt in [*receipts, *branch_receipts]
        ]
        for node_id in blocked_node_ids:
            if context.state.plan_node_status.get(node_id) != "completed":
                context.state.plan_node_status[node_id] = "recovery_blocked"
        context.state.unresolved_goals = [
            output_key
            for output_key in context.plan.terminal_output_keys
            if output_key not in context.state.artifacts
        ]
        context.state.latest_checkpoint_ref = selected_checkpoint_ref or None
        self._record_recovery_attempt(
            context,
            checkpoint_envelope,
            selected_checkpoint_ref=selected_checkpoint_ref,
            reconciliation_policy=reconciliation_policy,
            current_fingerprint=current_fingerprint,
            source_fingerprint=source_fingerprint,
            receipts=receipts,
            blocked_node_ids=blocked_node_ids,
        )
        context.active_frame = None

    def _restore_runtime_state_snapshot(
        self,
        context: PolicyContext,
        snapshot: Mapping[str, Any],
    ) -> None:
        payload = dict(snapshot or {})
        context.state.request_id = str(payload.get("request_id", context.request_id) or context.request_id)
        context.state.plan_id = str(payload.get("plan_id", context.plan.plan_id) or context.plan.plan_id)
        context.state.execution_state = str(payload.get("execution_state", context.state.execution_state) or context.state.execution_state)
        context.state.active_branch_count = int(payload.get("active_branch_count", context.state.active_branch_count) or 0)
        context.state.checkpoint_sequence_no = int(payload.get("checkpoint_sequence_no", context.state.checkpoint_sequence_no) or 0)
        context.state.event_sequence_no = int(payload.get("event_sequence_no", context.state.event_sequence_no) or 0)
        context.state.visible_tool_names = list(payload.get("visible_tool_names", context.state.visible_tool_names))
        context.state.unresolved_goals = list(payload.get("unresolved_goals", context.state.unresolved_goals))
        context.state.confidence = float(payload.get("confidence", context.state.confidence) or 0.0)
        context.state.mode = payload.get("mode")
        context.state.created_tools = int(payload.get("created_tools", context.state.created_tools) or 0)
        context.state.promoted_nodes = int(payload.get("promoted_nodes", context.state.promoted_nodes) or 0)
        context.state.checks_used = int(payload.get("checks_used", context.state.checks_used) or 0)
        context.state.interface_usage = dict(payload.get("interface_usage", context.state.interface_usage))
        context.state.artifacts = dict(payload.get("artifacts", context.state.artifacts))
        context.state.checkpoints = {
            key: (Checkpoint).model_validate(value)
            for key, value in dict(payload.get("checkpoints", {})).items()
        }
        context.state.worker_plans = {
            str(key): dict(value)
            for key, value in dict(payload.get("worker_plans", {})).items()
        }
        context.state.open_handle_ids = list(payload.get("open_handle_ids", context.state.open_handle_ids))
        context.state.plan_node_status = dict(payload.get("plan_node_status", context.state.plan_node_status))
        context.state.branch_states = {
            str(key): dict(value)
            for key, value in dict(payload.get("branch_states", {})).items()
        }
        context.state.branch_publications = [dict(item) for item in payload.get("branch_publications", context.state.branch_publications)]
        context.state.branch_resume_snapshots = {
            str(key): dict(value)
            for key, value in dict(payload.get("branch_resume_snapshots", context.state.branch_resume_snapshots)).items()
        }
        context.state.latest_checkpoint_ref = payload.get("latest_checkpoint_ref")
        context.state.subgoal_negative_steps = dict(payload.get("subgoal_negative_steps", context.state.subgoal_negative_steps))
        context.state.subgoal_last_model = dict(payload.get("subgoal_last_model", context.state.subgoal_last_model))
        context.state.last_unresolved_goal = payload.get("last_unresolved_goal")
        budget_totals = dict(payload.get("budget_totals", {}))
        context.budget.cost = float(budget_totals.get("cost", context.budget.cost) or 0.0)
        context.budget.latency = float(budget_totals.get("latency", context.budget.latency) or 0.0)
        context.budget.calls = int(budget_totals.get("calls", context.budget.calls) or 0)
        context.budget.checks = int(budget_totals.get("checks", context.budget.checks) or 0)
        context.budget.tokens = int(budget_totals.get("tokens", context.budget.tokens) or 0)
        context.budget.input_tokens = int(budget_totals.get("input_tokens", context.budget.input_tokens) or 0)
        context.budget.output_tokens = int(budget_totals.get("output_tokens", context.budget.output_tokens) or 0)
        restored_queue: list[AgentFrame] = []
        active_frame = payload.get("active_frame")
        if active_frame is not None:
            restored_queue.append(self._restore_frame_snapshot(context, active_frame))
        restored_queue.extend(
            self._restore_frame_snapshot(context, frame_snapshot)
            for frame_snapshot in payload.get("queued_frames", [])
        )
        context.state.queue = restored_queue

    @staticmethod
    def _receipt_restore_priority(receipt: SideEffectReceipt) -> tuple[int, int, float]:
        return (
            2 if receipt.status == "completed" else 1 if receipt.status == "reconciled" else 0,
            1 if receipt.action_kind in {"provider_completion", "tool_completion"} else 0,
            float(receipt.created_at or 0.0),
        )

    def _restore_completed_nodes_from_receipts(
        self,
        context: PolicyContext,
        receipts: Sequence[SideEffectReceipt],
        *,
        branch_id: str | None,
    ) -> None:
        node_map = {node.node_id: node for node in context.plan.nodes}
        restored_outputs: dict[str, tuple[SideEffectReceipt, Any]] = {}
        for receipt in receipts:
            node_id = str(receipt.node_id or "").strip()
            if not node_id or node_id not in node_map:
                continue
            receipt_branch_id = str(receipt.branch_id or "").strip() or None
            if branch_id is None and receipt_branch_id is not None:
                continue
            if branch_id is not None and receipt_branch_id != branch_id:
                continue
            if receipt.status not in {"completed", "reconciled"}:
                continue
            node = node_map[node_id]
            restorable, output = self._restore_output_from_receipt(node, receipt)
            if not restorable:
                continue
            current = restored_outputs.get(node_id)
            if current is None or self._receipt_restore_priority(receipt) > self._receipt_restore_priority(current[0]):
                restored_outputs[node_id] = (receipt, output)
        for node_id, (_, output) in restored_outputs.items():
            node = node_map[node_id]
            context.state.plan_node_status[node_id] = "completed"
            context.state.artifacts[node.output_key] = output
            self._restore_missing_artifact_node(context, node, output)

    def _restore_output_from_receipt(
        self,
        node: PlanNode,
        receipt: SideEffectReceipt,
    ) -> tuple[bool, Any]:
        result_ref = dict(receipt.result_ref or {})
        node_kind = str(node.node_kind or "")
        if node_kind == "direct_response" and "text" in result_ref:
            return True, self._decode_direct_response_output(str(result_ref.get("text", "")))
        if node_kind in {"builtin_op", "tool_call", "tool_synthesis", "repo_patch", "service_action"} and "output" in result_ref:
            return True, result_ref.get("output")
        return False, None

    def _restore_missing_artifact_node(
        self,
        context: PolicyContext,
        node: PlanNode,
        output: Any,
    ) -> None:
        artifact_exists = any(
            graph_node.get("type") == "Artifact" and graph_node.get("label") == node.output_key
            for graph_node in self.shell.short_term.nodes.values()
        )
        if artifact_exists:
            return
        producer_node_id = next(
            (
                frame.metadata.get("run_node_id")
                for frame in context.state.queue
                if not str(frame.worker_id or "").strip() and node.node_id in frame.operation_ids
            ),
            None,
        )
        self._record_artifact_node(
            self.shell.short_term,
            node.output_key,
            output,
            producer_node_id if isinstance(producer_node_id, str) else None,
        )

    def _restore_frame_snapshot(
        self,
        context: PolicyContext,
        frame_snapshot: QueuedFrameSnapshot | Mapping[str, Any],
    ) -> AgentFrame:
        snapshot = (
            frame_snapshot
            if isinstance(frame_snapshot, QueuedFrameSnapshot)
            else (QueuedFrameSnapshot).model_validate(frame_snapshot)
        )
        agent_snapshot = snapshot.agent_snapshot
        if agent_snapshot.restore_mode == "canonical_clone" and agent_snapshot.canonical_agent_id:
            try:
                restored_agent = self.shell.agent_pool.clone(agent_snapshot.canonical_agent_id)
            except KeyError as exc:
                raise ResumeRecoveryError(
                    RecoveryFailureKind.FRAME_RECONSTRUCTION_FAILED.value,
                    f"canonical agent {agent_snapshot.canonical_agent_id!r} is missing during restore",
                ) from exc
        else:
            restored_agent = (AgentTemplate).model_validate((agent_snapshot.agent_payload).model_dump())
            setattr(restored_agent, "_canonical", False)
            setattr(restored_agent, "_clone", True)
        return AgentFrame(
            frame_id=snapshot.frame_id or stable_hash(context.request_id, len(context.state.queue))[:16],
            agent=restored_agent,
            request_id=snapshot.request_id or context.request_id,
            plan_id=snapshot.plan_id or context.plan.plan_id,
            objective=snapshot.objective or context.plan.objective,
            operation_ids=list(snapshot.operation_ids),
            depth=int(snapshot.depth or 0),
            checkpoint=snapshot.checkpoint,
            parent_id=snapshot.parent_id,
            worker_id=snapshot.worker_id,
            role=snapshot.role,
            tool_scope=list(snapshot.tool_scope),
            model_class=snapshot.model_class,
            branch_group_id=snapshot.branch_group_id,
            trace_context=snapshot.trace_context or context.trace_context,
            metadata=dict(snapshot.metadata),
        )
