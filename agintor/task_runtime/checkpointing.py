from __future__ import annotations

import time
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


class CheckpointingMixin:
    def _restore_from_checkpoint(
        self,
        context: PolicyContext,
        checkpoint_envelope: CheckpointEnvelope,
        *,
        reconciliation_policy: str,
    ) -> None:
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
        if checkpoint_envelope.runtime_abi != self.runtime.kernel_manifest.runtime_abi:
            raise ResumeRecoveryError(
                RecoveryFailureKind.RUNTIME_ABI_MISMATCH.value,
                f"checkpoint runtime ABI mismatch: expected {self.runtime.kernel_manifest.runtime_abi}, found {checkpoint_envelope.runtime_abi}",
            )
        if checkpoint_envelope.storage_schema_version != self.runtime.kernel_manifest.storage_schema_version:
            raise ResumeRecoveryError(
                RecoveryFailureKind.STORAGE_SCHEMA_MISMATCH.value,
                "checkpoint storage schema version does not match the loaded runtime",
            )
        if checkpoint_envelope.runtime_hash != self.runtime.runtime_hash:
            raise ResumeRecoveryError(
                RecoveryFailureKind.RUNTIME_HASH_MISMATCH.value,
                "checkpoint runtime hash does not match the loaded runtime",
            )
        plan_snapshot = model_validate(ExecutionPlan, checkpoint_envelope.plan_snapshot)
        if plan_snapshot.plan_digest != context.plan.plan_digest:
            raise ResumeRecoveryError(
                RecoveryFailureKind.PLAN_DIGEST_MISMATCH.value,
                "checkpoint plan digest does not match the compiled execution plan",
            )
        self.shell.restore_checkpoint_shell_state(checkpoint_envelope.shell_state_snapshot)
        self._restore_runtime_state_snapshot(context, checkpoint_envelope.runtime_state_snapshot)
        ledger_receipts = [
            model_validate(SideEffectReceipt, receipt)
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
            model_dump(receipt)
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
        context.state.latest_checkpoint_ref = self.shell.latest_checkpoint_ref(
            checkpoint_envelope.run_id or checkpoint_envelope.request_id
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
            key: model_validate(Checkpoint, value)
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
        shell_snapshot = self.shell.snapshot_checkpoint_shell_state()
        resume_eligible, computed_ineligibility_reason = self._checkpoint_resume_eligibility(
            context,
            resume_eligible_override=resume_eligible_override,
            resume_ineligibility_reason=resume_ineligibility_reason,
        )
        envelope = CheckpointEnvelope(
            checkpoint_id=f"checkpoint.{plan.request_id}.{context.state.checkpoint_sequence_no:04d}",
            runtime_abi=self.runtime.kernel_manifest.runtime_abi,
            storage_schema_version=self.runtime.kernel_manifest.storage_schema_version,
            runtime_hash=self.runtime.runtime_hash,
            run_id=getattr(self.shell, "run_id", ""),
            run_root=str(getattr(self.shell, "run_root", self.shell.workspace)),
            attempt_id=getattr(self.shell, "attempt_id", ""),
            runtime_backend=context.runtime_backend,
            request_id=plan.request_id,
            origin_request_id=str(origin_request_id or "").strip() or None,
            source_checkpoint_ref=str(source_checkpoint_ref or "").strip() or None,
            plan_id=plan.plan_id,
            task_id=task.task_id,
            seed=seed,
            sequence_no=context.state.checkpoint_sequence_no,
            boundary=boundary,
            created_at=created_at,
            resume_eligible=resume_eligible,
            resume_ineligibility_reason=computed_ineligibility_reason,
            plan_snapshot=model_dump(plan),
            task_payload=model_dump(task),
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
                    key: model_dump(value)
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
            shell_state_snapshot=model_dump(shell_snapshot),
            side_effect_ledger={
                "receipts": [
                    model_validate(SideEffectReceipt, receipt)
                    for receipt in context.state.side_effect_receipts
                ]
            },
            attempt_snapshot=model_dump(self.shell.snapshot_attempt_state(boundary=boundary, published_at=created_at)),
            working_state_summary={
                "boundary": boundary,
                "execution_state": context.state.execution_state,
                "mode": context.state.mode,
                "confidence": context.state.confidence,
                "created_tools": context.state.created_tools,
                "promoted_nodes": context.state.promoted_nodes,
                "checks_used": context.state.checks_used,
                "interface_usage": dict(context.state.interface_usage),
                "subgoal_negative_steps": dict(context.state.subgoal_negative_steps),
                "subgoal_last_model": dict(context.state.subgoal_last_model),
                "last_unresolved_goal": context.state.last_unresolved_goal,
                "message_board_entries": list(context.shell.message_board.entries),
                "message_board_cursors": dict(context.shell.message_board.cursors),
            },
            trace_cursor={
                "trace_length": len(context.trace),
                "latest_event": context.trace[-1]["event"] if context.trace else None,
                "latest_event_sequence_no": context.state.event_sequence_no,
            },
        )
        checkpoint_ref = self.shell.save_checkpoint_envelope(envelope)
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
                agent_payload=model_validate(AgentTemplate, model_dump(frame.agent)),
            ),
        )

    def _restore_frame_snapshot(
        self,
        context: PolicyContext,
        frame_snapshot: QueuedFrameSnapshot | Mapping[str, Any],
    ) -> AgentFrame:
        snapshot = (
            frame_snapshot
            if isinstance(frame_snapshot, QueuedFrameSnapshot)
            else model_validate(QueuedFrameSnapshot, frame_snapshot)
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
            restored_agent = model_validate(AgentTemplate, model_dump(agent_snapshot.agent_payload))
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
