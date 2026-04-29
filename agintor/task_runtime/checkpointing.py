from __future__ import annotations

import time
from typing import Any, Mapping, Sequence
from ..exceptions import BranchCancelled, HardInvalidation, ProviderExhaustedError, ResumeRecoveryError
from ..openai_trace import resolve_trace_session_id, trace_grouping_key, trace_session_dir_name
from ..runtime_api import (
    AgentFrame,
    PolicyContext,
    RuntimeBudget,
    RuntimeState,
    compile_execution_plan_from_task,
    get_plan_node_descriptor,
    normalize_benchmark_request_id,
)
from ..schemas import (
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
from ..utils import count_tokens_rough, ensure_directory, merge_provider_usage, now_ts, stable_hash


class CheckpointingMixin:
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

    def _build_working_memory_snapshot(
        self,
        context: PolicyContext,
        *,
        boundary: str,
    ) -> WorkingMemorySnapshot:
        verified_facts: list[VerifiedFactRef] = []
        for receipt_payload in context.state.side_effect_receipts:
            receipt = (SideEffectReceipt).model_validate(receipt_payload)
            if receipt.status not in {"completed", "reconciled"}:
                continue
            result_ref = dict(receipt.result_ref or {})
            if "text" not in result_ref and "output" not in result_ref:
                continue
            content = str(result_ref.get("text", result_ref.get("output", "")))[:500]
            if not content.strip():
                continue
            verified_facts.append(
                VerifiedFactRef(
                    fact_id=f"fact.{stable_hash(receipt.side_effect_id, content)[:16]}",
                    content=content,
                    supporting_receipt_ids=[receipt.side_effect_id],
                )
            )
        branch_refs = sorted(
            str(branch_id)
            for branch_id, payload in context.state.branch_states.items()
            if str(payload.get("status", "") or "") in {"running", "completed", "published"}
        )
        selected_refs = sorted(
            {
                ref
                for ref in [
                    context.state.latest_checkpoint_ref,
                    *[
                        str(payload.get("checkpoint_ref", "") or "")
                        for payload in context.state.branch_resume_snapshots.values()
                        if isinstance(payload, Mapping)
                    ],
                ]
                if str(ref or "").strip()
            }
        )
        warnings = []
        if not context.state.unresolved_goals:
            active_summary = f"{boundary}: no unresolved goals"
        else:
            active_summary = f"{boundary}: {len(context.state.unresolved_goals)} unresolved goal(s)"
            warnings = [str(goal) for goal in context.state.unresolved_goals[:5]]
        return WorkingMemorySnapshot(
            current_objective=context.objective or context.task.prompt,
            accepted_constraints=list(context.task.symbolic_seeds) + list(context.task.file_paths),
            active_plan_summary=active_summary,
            verified_facts=verified_facts[:10],
            unresolved_critical_items=list(context.state.unresolved_goals),
            active_branch_refs=branch_refs,
            selected_checkpoint_refs=selected_refs,
            active_recovery_warnings=warnings,
            captured_at=now_ts(),
        )

    def _build_trace_cursor_snapshot(
        self,
        context: PolicyContext,
        task: BenchmarkTask,
        seed: int,
    ) -> TraceCursorSnapshot:
        latest_event = context.trace[-1] if context.trace else {}
        trace_context = context.trace_context
        linked_call_ids = sorted(
            {
                str(row.get("trace_call_id") or row.get("call_id") or row.get("openai_call_id") or "")
                for row in context.trace
                if isinstance(row, Mapping)
                and str(row.get("trace_call_id") or row.get("call_id") or row.get("openai_call_id") or "").strip()
            }
        )
        grouping = trace_grouping_key(trace_context)
        runtime_task_key = grouping[1] if grouping is not None else ""
        resolved_session_id = resolve_trace_session_id(trace_context.session_id)
        return TraceCursorSnapshot(
            runtime_trace_length=len(context.trace),
            latest_runtime_event=str(latest_event.get("event") or "") or None,
            latest_runtime_event_sequence_no=int(context.state.event_sequence_no or 0),
            last_session_id=resolved_session_id,
            last_build_id=trace_context.build_id,
            last_solve_request_id=context.request_id,
            last_runtime_task_key=runtime_task_key,
            linked_call_ids=linked_call_ids,
            materialization_state_ref=(
                f"openai_api_traces/sessions/{trace_session_dir_name(resolved_session_id)}/materialization_state.json"
            ),
            captured_at=now_ts(),
        )

    def _capture_environment_fingerprint(
        self,
        context: PolicyContext,
        *,
        source_checkpoint_ref: str | None,
    ) -> EnvironmentFingerprint:
        kernel_manifest = self.runtime.kernel_manifest
        provider_identity = [context.provider.__class__.__name__]
        tool_runtime_ids = sorted(self.shell.tool_registry.tools.keys())
        dependency_digest = stable_hash(getattr(kernel_manifest, "files", {}))
        fingerprint = EnvironmentFingerprint(
            runtime_backend=context.runtime_backend,
            runtime_hash=self.runtime.runtime_hash,
            runtime_contract_version=kernel_manifest.runtime_contract_version,
            runtime_isolation_policy=context.runtime_backend,
            supported_guarantees=[
                "checkpoint_envelopes",
                "canonical_json_run_store",
                "sqlite_state_index",
                "typed_subsystem_snapshots",
            ],
            provider_identity=provider_identity,
            model_class=getattr(context.profile, "default_model_class", None),
            sandbox_hash=stable_hash(str(getattr(self.shell.artifact_policy, "sandbox_root", ""))),
            tool_runtime_ids=tool_runtime_ids,
            dependency_digest=dependency_digest,
            filesystem_policy=str(getattr(self.shell.artifact_policy, "mode", "")),
            network_policy="host_policy",
            captured_at=now_ts(),
            source_attempt_id=str(getattr(self.shell, "attempt_id", "") or "") or None,
            source_checkpoint_ref=source_checkpoint_ref,
        )
        if getattr(self.shell, "run_store", None) is not None:
            self.shell.run_store.write_environment_fingerprint(self.shell.run_root, fingerprint)
        return fingerprint

    def _fingerprint_deltas(
        self,
        source_fingerprint: EnvironmentFingerprint | None,
        current_fingerprint: EnvironmentFingerprint,
    ) -> list[FingerprintDelta]:
        if source_fingerprint is None:
            return []
        source_payload = EnvironmentFingerprint.content_payload((source_fingerprint).model_dump())
        current_payload = EnvironmentFingerprint.content_payload((current_fingerprint).model_dump())
        deltas: list[FingerprintDelta] = []
        for field_name in EnvironmentFingerprint.content_field_names():
            if source_payload.get(field_name) != current_payload.get(field_name):
                deltas.append(
                    FingerprintDelta(
                        field=field_name,
                        previous=source_payload.get(field_name),
                        current=current_payload.get(field_name),
                    )
                )
        return deltas

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
