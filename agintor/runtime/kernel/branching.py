from __future__ import annotations

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
    SideEffectReceipt,
    capability_scope_allows,
    plan_node_requires_default_provider,
    service_action_transport_compatibility,
    is_terminal_receipt,
    terminalize_receipt,
)
from ...utils import count_tokens_rough, ensure_directory, merge_provider_usage, now_ts, stable_hash


class BranchingMixin:
    def _emit_branch_publication(
        self,
        context: PolicyContext,
        *,
        publication_kind: str,
        logical_key: str,
        payload: Mapping[str, Any],
        verifier_support: float = 0.0,
        unresolved_critical: int = 0,
        allow_when_cancelled: bool = False,
    ) -> BranchPublication | None:
        branch_id = getattr(context.active_frame, "worker_id", None) or getattr(context.trace_context, "worker_id", None)
        if not branch_id:
            return None
        if (
            context.cancellation_event is not None
            and getattr(context.cancellation_event, "is_set", lambda: False)()
            and not allow_when_cancelled
        ):
            return None
        publication = BranchPublication(
            publication_id=f"publication.{stable_hash(branch_id, logical_key, len(context.state.branch_publications))[:12]}",
            publication_kind=publication_kind,
            logical_key=logical_key,
            sequence_no=len(context.state.branch_publications),
            accepted=False,
            branch_id=branch_id,
            trace_context=context.trace_context,
            verifier_support=verifier_support,
            unresolved_critical=unresolved_critical,
            branch_rank=int(getattr(context.active_frame, "metadata", {}).get("merge_priority", 0) or 0),
            payload=dict(payload),
        )
        context.state.branch_publications.append((publication).model_dump())
        return publication

    def _candidate_artifact_publication(
        self,
        publications: Sequence[BranchPublication],
        branch_id: str,
    ) -> BranchPublication | None:
        candidates = [
            publication
            for publication in publications
            if publication.branch_id == branch_id
            and publication.accepted
            and publication.publication_kind == "candidate_artifact"
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda publication: (publication.sequence_no, publication.publication_id))

    def _restored_branch_frontier(
        self,
        context: PolicyContext,
        frame: AgentFrame,
    ) -> list[dict[str, Any]] | None:
        branch_states = [
            (BranchState).model_validate(payload)
            for payload in context.state.branch_states.values()
            if str(payload.get("parent_frame_id", "")) == frame.frame_id
        ]
        if not branch_states:
            return None
        if any(branch_state.status == "failed" for branch_state in branch_states):
            failed = sorted(
                f"{branch_state.branch_id}:{branch_state.failure_kind}"
                for branch_state in branch_states
                if branch_state.status == "failed"
            )
            raise HardInvalidation(
                f"restored branch frontier contains failed branches and cannot be merged: {', '.join(failed)}"
            )
        if any(branch_state.status not in {"completed", "cancelled"} for branch_state in branch_states):
            return None
        publications = [
            (BranchPublication).model_validate(payload)
            for payload in context.state.branch_publications
        ]
        worker_outputs: list[dict[str, Any]] = []
        for branch_state in sorted(branch_states, key=lambda item: (item.merge_priority, item.branch_id)):
            publication = self._candidate_artifact_publication(publications, branch_state.branch_id)
            if branch_state.status == "completed" and publication is None:
                return None
            if publication is None:
                continue
            worker_outputs.append(
                {
                    "worker_id": branch_state.branch_id,
                    "branch_id": branch_state.branch_id,
                    "merge_priority": branch_state.merge_priority,
                    "artifact": publication.payload.get("artifact"),
                    "verifier_support": publication.verifier_support,
                    "predicted_solve": branch_state.predicted_solve,
                    "unresolved_critical": publication.unresolved_critical,
                }
            )
        return worker_outputs or None

    def _restorable_branch_snapshots(
        self,
        context: PolicyContext,
        frame: AgentFrame,
    ) -> list[BranchResumeSnapshot]:
        snapshots: list[BranchResumeSnapshot] = []
        for payload in context.state.branch_resume_snapshots.values():
            snapshot = (BranchResumeSnapshot).model_validate(payload)
            if snapshot.branch_plan.parent_frame_id != frame.frame_id:
                continue
            snapshots.append(snapshot)
        snapshots.sort(key=lambda item: (item.branch_plan.merge_priority, item.branch_plan.branch_id))
        return snapshots

    def _extend_side_effect_receipts(
        self,
        context: PolicyContext,
        receipts: Sequence[SideEffectReceipt],
    ) -> None:
        by_id = {
            str(payload.get("side_effect_id", "")): dict(payload)
            for payload in context.state.side_effect_receipts
            if str(payload.get("side_effect_id", ""))
        }
        for receipt in receipts:
            payload = (receipt).model_dump()
            by_id[payload["side_effect_id"]] = payload
        context.state.side_effect_receipts = list(by_id.values())

    @staticmethod
    def _branch_budget_consumed(branch_context: PolicyContext) -> dict[str, Any]:
        return {
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

    @staticmethod
    def _branch_publications_snapshot(branch_context: PolicyContext) -> list[BranchPublication]:
        return [
            (BranchPublication).model_validate(payload)
            for payload in branch_context.state.branch_publications
        ]

    @staticmethod
    def _branch_receipts_snapshot(branch_context: PolicyContext) -> list[SideEffectReceipt]:
        return [
            (SideEffectReceipt).model_validate(payload)
            for payload in branch_context.state.side_effect_receipts
        ]

    def _branch_resume_snapshot(
        self,
        branch_plan: BranchPlan,
        branch_context: PolicyContext,
    ) -> BranchResumeSnapshot:
        return BranchResumeSnapshot(
            branch_plan=(branch_plan).model_copy(deep=True),
            execution_state=branch_context.state.execution_state,
            active_frame=self._frame_payload(branch_context.active_frame)
            if branch_context.active_frame is not None
            else None,
            queued_frames=[
                self._frame_payload(frame)
                for frame in branch_context.state.queue
            ],
            visible_tool_names=list(branch_context.state.visible_tool_names),
            artifacts=dict(branch_context.state.artifacts),
            open_handle_ids=list(branch_context.state.open_handle_ids),
            plan_node_status=dict(branch_context.state.plan_node_status),
            branch_publications=self._branch_publications_snapshot(branch_context),
            side_effect_receipts=self._branch_receipts_snapshot(branch_context),
            budget_totals={
                "normalized": branch_context.budget.normalized(),
                "cost": branch_context.budget.cost,
                "latency": branch_context.budget.latency,
                "calls": branch_context.budget.calls,
                "checks": branch_context.budget.checks,
                "tokens": branch_context.budget.tokens,
                "input_tokens": branch_context.budget.input_tokens,
                "output_tokens": branch_context.budget.output_tokens,
            },
            shell_state_snapshot=branch_context.shell.snapshot_checkpoint_shell_state(),
            created_tools=branch_context.state.created_tools,
            promoted_nodes=branch_context.state.promoted_nodes,
            checks_used=branch_context.state.checks_used,
        )

    def _store_branch_resume_snapshot(
        self,
        parent_context: PolicyContext,
        branch_plan: BranchPlan,
        branch_context: PolicyContext,
    ) -> None:
        parent_context.state.branch_resume_snapshots[branch_plan.branch_id] = (self._branch_resume_snapshot(branch_plan, branch_context)).model_dump()

    def _restore_branch_resume_snapshot(
        self,
        branch_context: PolicyContext,
        snapshot: BranchResumeSnapshot,
        *,
        reconciliation_policy: str,
    ) -> set[str]:
        branch_context.shell.restore_checkpoint_shell_state(snapshot.shell_state_snapshot)
        branch_context.state.execution_state = snapshot.execution_state
        branch_context.state.visible_tool_names = list(snapshot.visible_tool_names)
        branch_context.state.artifacts = dict(snapshot.artifacts)
        branch_context.state.open_handle_ids = list(snapshot.open_handle_ids)
        branch_context.state.plan_node_status = dict(snapshot.plan_node_status)
        branch_context.state.branch_publications = [
            (publication).model_dump()
            for publication in snapshot.branch_publications
        ]
        branch_context.state.created_tools = snapshot.created_tools
        branch_context.state.promoted_nodes = snapshot.promoted_nodes
        branch_context.state.checks_used = snapshot.checks_used
        branch_context.budget.cost = float(snapshot.budget_totals.cost or 0.0)
        branch_context.budget.latency = float(snapshot.budget_totals.latency or 0.0)
        branch_context.budget.calls = int(snapshot.budget_totals.calls or 0)
        branch_context.budget.checks = int(snapshot.budget_totals.checks or 0)
        branch_context.budget.tokens = int(snapshot.budget_totals.tokens or 0)
        branch_context.budget.input_tokens = int(snapshot.budget_totals.input_tokens or 0)
        branch_context.budget.output_tokens = int(snapshot.budget_totals.output_tokens or 0)
        branch_context.state.queue = []
        if snapshot.active_frame is not None:
            branch_context.state.queue.append(
                self._restore_frame_snapshot(branch_context, snapshot.active_frame)
            )
        branch_context.state.queue.extend(
            self._restore_frame_snapshot(branch_context, frame_snapshot)
            for frame_snapshot in snapshot.queued_frames
        )
        receipts, blocked_node_ids = self._reconcile_side_effect_receipts(
            branch_context,
            list(snapshot.side_effect_receipts),
            reconciliation_policy=reconciliation_policy,
        )
        self._restore_completed_nodes_from_receipts(
            branch_context,
            receipts,
            branch_id=snapshot.branch_plan.branch_id,
        )
        branch_context.state.side_effect_receipts = [(receipt).model_dump() for receipt in receipts]
        for node_id in blocked_node_ids:
            if branch_context.state.plan_node_status.get(node_id) != "completed":
                branch_context.state.plan_node_status[node_id] = "recovery_blocked"
        return blocked_node_ids

    def _branch_output_from_state(
        self,
        plan: ExecutionPlan,
        branch_plan: BranchPlan,
        artifacts: Mapping[str, Any],
    ) -> Any:
        output_keys = [
            self._plan_node_by_id(plan, node_id).output_key
            for node_id in branch_plan.assigned_node_ids
            if self._plan_node_by_id(plan, node_id).output_key in artifacts
        ]
        if not output_keys:
            return None
        if len(output_keys) == 1:
            return artifacts.get(output_keys[0])
        return {output_key: artifacts.get(output_key) for output_key in output_keys}

    @staticmethod
    def _publication_acceptance_sort_key(publication: BranchPublication) -> tuple[Any, ...]:
        return (
            -float(publication.verifier_support or 0.0),
            int(publication.unresolved_critical or 0),
            int(publication.branch_rank or 0),
            str(publication.branch_id or ""),
            int(publication.sequence_no or 0),
            str(publication.publication_id or ""),
        )

    def _classify_branch_failure(
        self,
        plan: ExecutionPlan,
        branch_plan: BranchPlan,
        branch_context: PolicyContext,
        exc: Exception,
    ) -> tuple[str, dict[str, Any]]:
        message = str(exc)
        budget_consumed = self._branch_budget_consumed(branch_context)
        if "exceeded reserved budget" in message:
            return "reservation_exceeded", {
                "reserved_budget": (branch_plan.reserved_budget).model_dump(),
                "budget_consumed": budget_consumed,
            }
        if any(
            token in message
            for token in (
                "failed to clean up handle",
                "left handle",
                "cannot safely reconcile side effect",
                "cannot reconcile tool launch receipt",
            )
        ):
            return "cleanup_failure", {"error": message, "budget_consumed": budget_consumed}
        if any(
            self._plan_node_by_id(plan, node_id).node_kind == "verify"
            and branch_context.state.plan_node_status.get(node_id) == "failed"
            for node_id in branch_plan.assigned_node_ids
        ):
            return "verification_failure", {"error": message, "budget_consumed": budget_consumed}
        if isinstance(exc, (ValueError, TypeError, AssertionError)):
            return "protocol_failure", {"error": message, "budget_consumed": budget_consumed}
        return "branch_execution_error", {"error": message, "budget_consumed": budget_consumed}

    @staticmethod
    def _failed_branch_cancellation_reason(failure_kind: str | None) -> str:
        if failure_kind == "verification_failure":
            return "verification_failure"
        if failure_kind == "reservation_exceeded":
            return "budget_exhaustion"
        return "fatal_branch_fault"

    def _failed_branch_result(
        self,
        branch_plan: BranchPlan,
        branch_context: PolicyContext,
        *,
        failure_kind: str,
        failure_details: Mapping[str, Any],
        error: str,
        artifact: Any = None,
    ) -> BranchResult:
        unresolved_critical = max(
            0,
            sum(
                1
                for node_id in branch_plan.assigned_node_ids
                if branch_context.state.plan_node_status.get(node_id) != "completed"
            ),
        )
        verifier_support = self._worker_support(branch_context.task, artifact) if artifact is not None else 0.0
        return BranchResult(
            branch_plan=branch_plan,
            branch_state=BranchState(
                branch_id=branch_plan.branch_id,
                status="failed",
                parent_frame_id=branch_plan.parent_frame_id,
                assigned_node_ids=list(branch_plan.assigned_node_ids),
                merge_priority=branch_plan.merge_priority,
                predicted_solve=branch_plan.predicted_solve,
                reserved_budget=branch_plan.reserved_budget,
                publications=self._branch_publications_snapshot(branch_context),
                budget_consumed=self._branch_budget_consumed(branch_context),
                verifier_support=verifier_support,
                unresolved_critical=unresolved_critical,
                failure_kind=failure_kind,
                failure_details=dict(failure_details),
                error=error,
            ),
            artifact=artifact,
            verifier_support=verifier_support,
            unresolved_critical=unresolved_critical,
            side_effect_receipts=self._branch_receipts_snapshot(branch_context),
        )

    def _accept_branch_publications(
        self,
        context: PolicyContext,
        branch_results: Sequence[BranchResult],
    ) -> None:
        accepted_ids = {
            publication.publication_id
            for branch_result in branch_results
            if branch_result.branch_state.status != "failed"
            for publication in branch_result.branch_state.publications
        }
        if not accepted_ids:
            return
        updated_publications: list[dict[str, Any]] = []
        for payload in context.state.branch_publications:
            publication = (BranchPublication).model_validate(payload)
            if publication.publication_id in accepted_ids:
                publication = publication.model_copy(update={"accepted": True}, deep=True)
            updated_publications.append((publication).model_dump())
        context.state.branch_publications = updated_publications
        for branch_id, payload in list(context.state.branch_states.items()):
            branch_state = (BranchState).model_validate(payload)
            branch_state.publications = [
                publication.model_copy(update={"accepted": True}, deep=True)
                if publication.publication_id in accepted_ids
                else publication
                for publication in branch_state.publications
            ]
            context.state.branch_states[branch_id] = (branch_state).model_dump()

    def _apply_branch_group_results(
        self,
        context: PolicyContext,
        frame: AgentFrame,
        task: BenchmarkTask,
        plan: ExecutionPlan,
        branch_results: Sequence[BranchResult],
        provider_usage_ledger: dict[str, Any],
        *,
        propagated_resume_error: ResumeRecoveryError | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        worker_outputs: list[dict[str, Any]] = []
        max_branch_latency = 0.0
        faults = 0
        failed_results: list[BranchResult] = []
        for branch_result in branch_results:
            context.state.branch_states[branch_result.branch_plan.branch_id] = (branch_result.branch_state).model_dump()
            self._merge_provider_usage_into(provider_usage_ledger, branch_result.provider_usage)
            existing_publication_ids = {
                str(payload.get("publication_id", ""))
                for payload in context.state.branch_publications
            }
            for publication in branch_result.branch_state.publications:
                payload = (publication).model_dump()
                if payload["publication_id"] in existing_publication_ids:
                    continue
                context.state.branch_publications.append(payload)
                existing_publication_ids.add(payload["publication_id"])
            self._extend_side_effect_receipts(context, branch_result.side_effect_receipts)
            budget_consumed = dict(branch_result.branch_state.budget_consumed)
            context.budget.cost += float(budget_consumed.get("cost", 0.0) or 0.0)
            max_branch_latency = max(max_branch_latency, float(budget_consumed.get("latency", 0.0) or 0.0))
            context.budget.calls += int(budget_consumed.get("model_calls", 0) or 0)
            context.budget.checks += int(budget_consumed.get("checks", 0) or 0)
            context.budget.tokens += int(budget_consumed.get("tokens", 0) or 0)
            context.budget.input_tokens += int(budget_consumed.get("input_tokens", 0) or 0)
            context.budget.output_tokens += int(budget_consumed.get("output_tokens", 0) or 0)
            context.state.checks_used += int(budget_consumed.get("checks", 0) or 0)
            context.state.created_tools += int(budget_consumed.get("created_tools", 0) or 0)
            context.state.promoted_nodes += int(budget_consumed.get("promoted_nodes", 0) or 0)
            if branch_result.branch_state.status == "failed":
                failed_results.append(branch_result)
                faults += 1
                context.record(
                    "branch_failed",
                    branch_id=branch_result.branch_plan.branch_id,
                    frame_id=frame.frame_id,
                    assigned_node_ids=list(branch_result.branch_plan.assigned_node_ids),
                    error=branch_result.branch_state.error,
                    failure_kind=branch_result.branch_state.failure_kind,
                    failure_details=branch_result.branch_state.failure_details,
                )
            if branch_result.branch_state.status != "completed":
                continue
            worker_outputs.append(
                {
                    "worker_id": branch_result.branch_plan.branch_id,
                    "branch_id": branch_result.branch_plan.branch_id,
                    "merge_priority": branch_result.branch_state.merge_priority,
                    "artifact": branch_result.artifact,
                    "verifier_support": branch_result.branch_state.verifier_support,
                    "predicted_solve": branch_result.branch_state.predicted_solve,
                    "unresolved_critical": branch_result.branch_state.unresolved_critical,
                }
            )
        context.budget.latency += max_branch_latency
        self._accept_branch_publications(context, branch_results)
        for branch_result in branch_results:
            if branch_result.branch_state.status != "completed":
                continue
            context.state.branch_resume_snapshots.pop(branch_result.branch_plan.branch_id, None)
            self.shell.message_board.append(
                branch_result.branch_plan.branch_id,
                {
                    "artifact": branch_result.artifact,
                    "branch_id": branch_result.branch_plan.branch_id,
                    "accepted": True,
                },
            )
        resume_eligible_override = propagated_resume_error is None and not failed_results
        ineligibility_reason = None
        if propagated_resume_error is not None:
            ineligibility_reason = f"resume_recovery_error:{propagated_resume_error.failure_kind}"
        elif failed_results:
            ineligibility_reason = "failed_branch_group"
        self._publish_checkpoint_envelope(
            context,
            task,
            plan,
            context.seed,
            "after_branch_completion",
            resume_eligible_override=resume_eligible_override,
            resume_ineligibility_reason=ineligibility_reason,
        )
        if propagated_resume_error is not None:
            raise propagated_resume_error
        if failed_results:
            failure_summary = ", ".join(
                f"{result.branch_plan.branch_id}:{result.branch_state.failure_kind}"
                for result in sorted(
                    failed_results,
                    key=lambda result: (result.branch_plan.merge_priority, result.branch_plan.branch_id),
                )
            )
            raise HardInvalidation(f"branch group failed after accounting: {failure_summary}")
        return worker_outputs, faults
