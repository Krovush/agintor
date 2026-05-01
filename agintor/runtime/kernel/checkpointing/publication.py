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

class CheckpointPublicationMixin:
    def _publish_checkpoint_envelope(
        self,
        context: PolicyContext,
        task: BenchmarkTask,
        plan: ExecutionPlan,
        seed: int,
        boundary: str,
        *,
        origin_request_id: str | None = None,
        source_checkpoint_ref: str | None = None,
        resume_eligible_override: bool | None = None,
        resume_ineligibility_reason: str | None = None,
    ) -> None:
        context.state.checkpoint_sequence_no += 1
        created_at = now_ts()
        checkpoint_id = f"checkpoint.{plan.request_id}.{context.state.checkpoint_sequence_no:04d}"
        shell_snapshot = self.shell.snapshot_checkpoint_shell_state(
            checkpoint_id=checkpoint_id,
            boundary=boundary,
            branch_publications=context.state.branch_publications,
            side_effect_receipts=context.state.side_effect_receipts,
            runtime_event_refs=[str(row.get("event_id", "")) for row in context.trace if isinstance(row, Mapping)],
        )
        environment_fingerprint = self._capture_environment_fingerprint(
            context,
            source_checkpoint_ref=source_checkpoint_ref,
        )
        resume_eligible, computed_ineligibility_reason = self._checkpoint_resume_eligibility(
            context,
            resume_eligible_override=resume_eligible_override,
            resume_ineligibility_reason=resume_ineligibility_reason,
        )
        envelope = CheckpointEnvelope(
            checkpoint_id=checkpoint_id,
            runtime_contract_version=self.runtime.kernel_manifest.runtime_contract_version,
            runtime_hash=self.runtime.runtime_hash,
            run_id=getattr(self.shell, "run_id", ""),
            run_root=str(getattr(self.shell, "run_root", self.shell.workspace)),
            attempt_id=getattr(self.shell, "attempt_id", ""),
            runtime_backend=context.runtime_backend,
            request_id=plan.request_id,
            origin_request_id=str(origin_request_id or "").strip() or None,
            source_checkpoint_ref=str(source_checkpoint_ref or "").strip() or None,
            environment_fingerprint_id=environment_fingerprint.fingerprint_id,
            plan_id=plan.plan_id,
            task_id=task.task_id,
            seed=seed,
            sequence_no=context.state.checkpoint_sequence_no,
            boundary=boundary,
            created_at=created_at,
            resume_eligible=resume_eligible,
            resume_ineligibility_reason=computed_ineligibility_reason,
            plan_snapshot=(plan).model_dump(),
            task_payload=(task).model_dump(),
            runtime_state_snapshot={
                "request_id": context.state.request_id,
                "plan_id": context.state.plan_id,
                "execution_state": context.state.execution_state,
                "active_branch_count": context.state.active_branch_count,
                "checkpoint_sequence_no": context.state.checkpoint_sequence_no,
                "event_sequence_no": context.state.event_sequence_no,
                "active_frame": self._frame_payload(context.active_frame) if context.active_frame is not None else None,
                "queued_frames": [self._frame_payload(frame) for frame in context.state.queue],
                "visible_tool_names": list(context.state.visible_tool_names),
                "unresolved_goals": list(context.state.unresolved_goals),
                "confidence": context.state.confidence,
                "mode": context.state.mode,
                "created_tools": context.state.created_tools,
                "promoted_nodes": context.state.promoted_nodes,
                "checks_used": context.state.checks_used,
                "interface_usage": dict(context.state.interface_usage),
                "artifacts": dict(context.state.artifacts),
                "checkpoints": {
                    key: (value).model_dump()
                    for key, value in context.state.checkpoints.items()
                },
                "worker_plans": dict(context.state.worker_plans),
                "open_handle_ids": list(context.state.open_handle_ids),
                "plan_node_status": dict(context.state.plan_node_status),
                "branch_states": dict(context.state.branch_states),
                "branch_publications": list(context.state.branch_publications),
                "branch_resume_snapshots": dict(context.state.branch_resume_snapshots),
                "latest_checkpoint_ref": context.state.latest_checkpoint_ref,
                "subgoal_negative_steps": dict(context.state.subgoal_negative_steps),
                "subgoal_last_model": dict(context.state.subgoal_last_model),
                "last_unresolved_goal": context.state.last_unresolved_goal,
                "budget_totals": {
                    "normalized": context.budget.normalized(),
                    "cost": context.budget.cost,
                    "latency": context.budget.latency,
                    "calls": context.budget.calls,
                    "checks": context.budget.checks,
                    "tokens": context.budget.tokens,
                    "input_tokens": context.budget.input_tokens,
                    "output_tokens": context.budget.output_tokens,
                },
                "verifier_state": {
                    "checker_ladder": list(plan.verification_plan.checker_ladder),
                    "required": plan.verification_plan.required,
                    "exact_verifier_required": plan.verification_plan.exact_verifier_required,
                    "verifier_type": plan.verification_plan.verifier_type,
                    "terminal_nodes": list(plan.verification_plan.terminal_nodes),
                },
            },
            shell_state_snapshot=(shell_snapshot).model_dump(),
            side_effect_ledger={
                "receipts": [
                    (SideEffectReceipt).model_validate(receipt)
                    for receipt in context.state.side_effect_receipts
                ]
            },
            attempt_snapshot=(self.shell.snapshot_attempt_state(boundary=boundary, published_at=created_at)).model_dump(),
            working_state=self._build_working_memory_snapshot(context, boundary=boundary),
            trace_cursor=self._build_trace_cursor_snapshot(context, task, seed),
        )
        checkpoint_ref = self.shell.save_checkpoint_envelope(envelope)
        if getattr(self.shell, "run_store", None) is not None:
            self.shell.run_store.write_working_memory_snapshot(
                self.shell.run_root,
                envelope.working_state,
                checkpoint_id=envelope.checkpoint_id,
            )
        if checkpoint_ref.resume_eligible:
            context.state.latest_checkpoint_ref = checkpoint_ref.ref
        context.record(
            "checkpoint_published",
            checkpoint_id=envelope.checkpoint_id,
            checkpoint_ref=checkpoint_ref.ref,
            boundary=boundary,
            resume_eligible=checkpoint_ref.resume_eligible,
            resume_ineligibility_reason=checkpoint_ref.resume_ineligibility_reason,
        )

    def _frame_payload(self, frame: AgentFrame) -> QueuedFrameSnapshot:
        if any(agent.agent_id == frame.agent.agent_id for agent in self.shell.agent_pool.list()):
            restore_mode = "canonical_clone"
            canonical_agent_id = frame.agent.agent_id
        else:
            restore_mode = "serialized_ephemeral"
            canonical_agent_id = None
        return QueuedFrameSnapshot(
            frame_id=frame.frame_id,
            request_id=frame.request_id,
            plan_id=frame.plan_id,
            objective=frame.objective,
            operation_ids=list(frame.operation_ids),
            depth=frame.depth,
            checkpoint=frame.checkpoint,
            parent_id=frame.parent_id,
            worker_id=frame.worker_id,
            role=frame.role,
            tool_scope=list(frame.tool_scope),
            model_class=frame.model_class,
            branch_group_id=frame.branch_group_id,
            trace_context=frame.trace_context,
            metadata=dict(frame.metadata),
            agent_snapshot=QueuedAgentSnapshot(
                restore_mode=restore_mode,
                canonical_agent_id=canonical_agent_id,
                agent_payload=(AgentTemplate).model_validate((frame.agent).model_dump()),
            ),
        )

    def _checkpoint_resume_eligibility(
        self,
        context: PolicyContext,
        *,
        resume_eligible_override: bool | None,
        resume_ineligibility_reason: str | None,
    ) -> tuple[bool, str | None]:
        if resume_eligible_override is not None:
            eligible = bool(resume_eligible_override)
            return eligible, None if eligible else str(resume_ineligibility_reason or "checkpoint_marked_ineligible")
        failed_branches = [
            branch_id
            for branch_id, payload in context.state.branch_states.items()
            if str(payload.get("status", "") or "") == "failed"
        ]
        if failed_branches:
            failed_branches.sort()
            return False, f"failed_branch_group:{','.join(failed_branches)}"
        running_branch_ids = {
            branch_id
            for branch_id, payload in context.state.branch_states.items()
            if str(payload.get("status", "") or "") == "running"
        }
        if running_branch_ids and any(branch_id not in context.state.branch_resume_snapshots for branch_id in running_branch_ids):
            missing = sorted(branch_id for branch_id in running_branch_ids if branch_id not in context.state.branch_resume_snapshots)
            return False, f"missing_branch_resume_snapshot:{','.join(missing)}"
        return True, None
