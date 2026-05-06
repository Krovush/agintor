from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from threading import Event, Lock
from typing import Any, Sequence
from ....core.exceptions import BranchCancelled, ResumeRecoveryError
from ...api import (
    AgentFrame,
    PolicyContext,
    RuntimeBudget,
    RuntimeState,
)
from ....contracts import (
    BenchmarkTask,
    BranchPlan,
    BranchResumeSnapshot,
    BranchResult,
    BranchState,
    ExecutionPlan,
    RecoveryFailureKind,
    SideEffectReceipt,
)
from ....utils import stable_hash
from .providers import _clone_provider

class BranchRunMixin:
    def _execute_horizontal_branches(
        self,
        context: PolicyContext,
        frame: AgentFrame,
        task: BenchmarkTask,
        plan: ExecutionPlan,
        workers: Sequence[dict[str, Any]],
        provider_usage_ledger: dict[str, Any],
    ) -> tuple[list[dict[str, Any]] | None, int]:
        if not workers:
            return None, 0
        branch_plans = self._launchable_branch_plans(context, frame, plan, workers)
        if not branch_plans:
            context.record(
                "branch_skipped",
                parent_frame_id=frame.frame_id,
                reason="joint_budget_infeasible",
            )
            return None, 0
        for branch_plan in branch_plans:
            context.state.branch_states[branch_plan.branch_id] = (BranchState(
                    branch_id=branch_plan.branch_id,
                    status="pending",
                    parent_frame_id=frame.frame_id,
                    assigned_node_ids=list(branch_plan.assigned_node_ids),
                    merge_priority=branch_plan.merge_priority,
                    predicted_solve=branch_plan.predicted_solve,
                    reserved_budget=branch_plan.reserved_budget,
                )).model_dump()
        self._publish_checkpoint_envelope(context, task, plan, context.seed, "before_branch_fanout")
        cancellation_event = Event()
        persist_lock = Lock()
        branch_results: list[BranchResult] = []
        faults = 0
        propagated_resume_error: ResumeRecoveryError | None = None
        context.state.active_branch_count = len(branch_plans)
        prepared_branches = [
            self._prepare_branch_provider(plan, branch_plan)
            for branch_plan in branch_plans
        ]
        executor = ThreadPoolExecutor(max_workers=len(prepared_branches), thread_name_prefix=f"branch-{plan.plan_id[:8]}")
        try:
            provider_overrides = self._branch_provider_overrides()
            future_map = {}
            for prepared_branch_plan, branch_provider in prepared_branches:
                provider_overrides[prepared_branch_plan.branch_id] = branch_provider
                future = executor.submit(
                    self._run_branch_plan,
                    context,
                    task,
                    plan,
                    prepared_branch_plan,
                    cancellation_event,
                    persist_lock,
                )
                future_map[future] = prepared_branch_plan
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
            for prepared_branch_plan, _ in prepared_branches:
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

    def _completed_branch_result(
        self,
        branch_plan: BranchPlan,
        branch_context: PolicyContext,
        *,
        artifact: Any,
        verifier_support: float,
        unresolved_critical: int,
    ) -> BranchResult:
        return BranchResult(
            branch_plan=branch_plan,
            branch_state=BranchState(
                branch_id=branch_plan.branch_id,
                status="completed",
                parent_frame_id=branch_plan.parent_frame_id,
                assigned_node_ids=list(branch_plan.assigned_node_ids),
                merge_priority=branch_plan.merge_priority,
                predicted_solve=branch_plan.predicted_solve,
                reserved_budget=branch_plan.reserved_budget,
                publications=self._branch_publications_snapshot(branch_context),
                budget_consumed=self._branch_budget_consumed(branch_context),
                verifier_support=verifier_support,
                unresolved_critical=unresolved_critical,
            ),
            artifact=artifact,
            verifier_support=verifier_support,
            unresolved_critical=unresolved_critical,
            side_effect_receipts=self._branch_receipts_snapshot(branch_context),
        )

    def _run_branch_plan(
        self,
        parent_context: PolicyContext,
        task: BenchmarkTask,
        plan: ExecutionPlan,
        branch_plan: BranchPlan,
        cancellation_event: Event,
        persist_lock: Lock,
        resume_snapshot: BranchResumeSnapshot | None = None,
        reconciliation_policy: str = "strict",
    ) -> BranchResult:
        branch_shell = parent_context.shell.fork_branch(branch_plan.branch_id)
        provider_overrides = self._branch_provider_overrides()
        branch_provider = provider_overrides.pop(branch_plan.branch_id, None)
        if branch_provider is None:
            branch_provider = _clone_provider(self.provider, provider_profile=self.runtime_profile.runtime_provider)
        branch_provider_usage_before = self._provider_usage_snapshot(branch_provider)
        branch_runtime = type(self)(
            self.runtime,
            branch_shell,
            branch_provider,
            budget_overrides={},
            runtime_profile=self.runtime_profile,
            runtime_backend=parent_context.runtime_backend,
        )
        branch_budget = RuntimeBudget(
            C_max=parent_context.budget.C_max,
            L_max=branch_plan.reserved_budget.latency_max,
            M_max=branch_plan.reserved_budget.model_calls_max,
            Q_max=branch_plan.reserved_budget.checks_max,
            context_window_tokens=parent_context.budget.context_window_tokens,
        )
        branch_state = RuntimeState(
            request_id=plan.request_id,
            plan_id=plan.plan_id,
            execution_state="branching",
            visible_tool_names=list(parent_context.state.visible_tool_names),
        )
        branch_trace: list[dict[str, Any]] = []
        branch_context = PolicyContext(
            runtime_dir=self.runtime.runtime_dir,
            shell=branch_shell,
            task=task,
            request_id=plan.request_id,
            plan=plan,
            trace_context=branch_plan.trace_context or parent_context.trace_context,
            provider=branch_provider,
            profile=self.runtime_profile,
            seed=parent_context.seed,
            state=branch_state,
            budget=branch_budget,
            trace=branch_trace,
            objective=plan.objective,
            runtime_backend=parent_context.runtime_backend,
            cancellation_event=cancellation_event,
        )

        def finalize_branch_result(result: BranchResult, *, boundary: str) -> BranchResult:
            provider_usage = self._provider_usage_delta(
                branch_provider_usage_before,
                self._provider_usage_snapshot(branch_provider),
            )
            finalized_branch_plan = self._branch_plan_with_updated_replay_cursor(
                result.branch_plan,
                branch_provider,
            )
            finalized = result.model_copy(
                update={"branch_plan": finalized_branch_plan, "provider_usage": provider_usage},
                deep=True,
            )
            with persist_lock:
                parent_context.state.branch_states[finalized.branch_plan.branch_id] = (finalized.branch_state).model_dump()
                parent_context.state.branch_resume_snapshots.pop(finalized.branch_plan.branch_id, None)
                existing_ids = {
                    str(payload.get("publication_id", ""))
                    for payload in parent_context.state.branch_publications
                }
                for publication in finalized.branch_state.publications:
                    payload = (publication).model_dump()
                    if payload["publication_id"] in existing_ids:
                        continue
                    parent_context.state.branch_publications.append(payload)
                    existing_ids.add(payload["publication_id"])
                self._extend_side_effect_receipts(parent_context, finalized.side_effect_receipts)
                self._publish_checkpoint_envelope(parent_context, task, plan, parent_context.seed, boundary)
            return finalized

        def persist_branch_state(
            boundary: str,
            *,
            triggering_receipt: SideEffectReceipt | None = None,
        ) -> None:
            with persist_lock:
                if triggering_receipt is not None:
                    self._record_side_effect_receipt(parent_context, triggering_receipt)
                persisted_branch_plan = self._branch_plan_with_updated_replay_cursor(
                    branch_plan,
                    branch_provider,
                )
                self._store_branch_resume_snapshot(parent_context, persisted_branch_plan, branch_context)
                parent_context.state.branch_states[persisted_branch_plan.branch_id] = (BranchState(
                        branch_id=persisted_branch_plan.branch_id,
                        status="running",
                        parent_frame_id=persisted_branch_plan.parent_frame_id,
                        assigned_node_ids=list(persisted_branch_plan.assigned_node_ids),
                        merge_priority=persisted_branch_plan.merge_priority,
                        predicted_solve=persisted_branch_plan.predicted_solve,
                        reserved_budget=persisted_branch_plan.reserved_budget,
                        publications=self._branch_publications_snapshot(branch_context),
                        budget_consumed=self._branch_budget_consumed(branch_context),
                    )).model_dump()
                existing_ids = {
                    str(payload.get("publication_id", ""))
                    for payload in parent_context.state.branch_publications
                }
                for payload in branch_context.state.branch_publications:
                    publication_id = str(payload.get("publication_id", ""))
                    if publication_id in existing_ids:
                        continue
                    parent_context.state.branch_publications.append(dict(payload))
                    existing_ids.add(publication_id)
                self._publish_checkpoint_envelope(parent_context, task, plan, parent_context.seed, boundary)

        branch_context.side_effect_callback = lambda receipt: persist_branch_state(
            "after_branch_side_effect",
            triggering_receipt=receipt,
        )
        branch_context.checkpoint_callback = persist_branch_state
        if resume_snapshot is not None:
            self._restore_branch_resume_snapshot(
                branch_context,
                resume_snapshot,
                reconciliation_policy=reconciliation_policy,
            )
            if branch_context.state.queue:
                branch_context.active_frame = branch_context.state.queue.pop(0)
        branch_context.record(
            "branch_started",
            branch_id=branch_plan.branch_id,
            frame_id=branch_plan.parent_frame_id,
            assigned_node_ids=list(branch_plan.assigned_node_ids),
        )
        if cancellation_event.is_set():
            cancellation_reason = str(
                getattr(cancellation_event, "reason", "parent_stop_policy") or "parent_stop_policy"
            )
            cancellation_details = dict(getattr(cancellation_event, "details", {}) or {})
            return finalize_branch_result(
                self._cancelled_branch_result(
                    branch_plan,
                    branch_context,
                    len(branch_plan.assigned_node_ids),
                    reason=cancellation_reason,
                    details=cancellation_details,
                ),
                boundary="after_branch_completion",
            )
        if branch_context.active_frame is None:
            if resume_snapshot is not None and not branch_context.state.queue:
                output = self._branch_output_from_state(plan, branch_plan, branch_context.state.artifacts)
                if output is None and any(
                    branch_context.state.plan_node_status.get(node_id) != "completed"
                    for node_id in branch_plan.assigned_node_ids
                ):
                    raise ResumeRecoveryError(
                        RecoveryFailureKind.FRAME_RECONSTRUCTION_FAILED.value,
                        f"branch {branch_plan.branch_id} cannot reconstruct runnable work from checkpoint state",
                    )
                verifier_support = self._worker_support(task, output) if output is not None else 0.0
                unresolved_critical = max(
                    0,
                    sum(
                        1
                        for node_id in branch_plan.assigned_node_ids
                        if branch_context.state.plan_node_status.get(node_id) != "completed"
                    ),
                )
                if output is not None and self._candidate_artifact_publication(
                    self._branch_publications_snapshot(branch_context),
                    branch_plan.branch_id,
                ) is None:
                    self._emit_branch_publication(
                        branch_context,
                        publication_kind="candidate_artifact",
                        logical_key=f"{branch_plan.branch_id}.artifact",
                        payload={
                            "artifact": output,
                            "summary": {},
                            "predicted_solve": branch_plan.predicted_solve,
                        },
                        verifier_support=verifier_support,
                        unresolved_critical=unresolved_critical,
                    )
                branch_context.record(
                    "branch_completed",
                    branch_id=branch_plan.branch_id,
                    frame_id=branch_plan.parent_frame_id,
                    assigned_node_ids=list(branch_plan.assigned_node_ids),
                )
                return finalize_branch_result(
                    self._completed_branch_result(
                        branch_plan,
                        branch_context,
                        artifact=output,
                        verifier_support=verifier_support,
                        unresolved_critical=unresolved_critical,
                    ),
                    boundary="after_branch_completion",
                )
            branch_context.active_frame = AgentFrame(
                frame_id=stable_hash(plan.request_id, branch_plan.branch_id, "frame")[:16],
                agent=branch_shell.agent_pool.clone("root"),
                request_id=plan.request_id,
                plan_id=plan.plan_id,
                trace_context=branch_plan.trace_context,
                objective=plan.objective,
                operation_ids=list(branch_plan.assigned_node_ids),
                depth=1,
                role="worker",
                worker_id=branch_plan.branch_id,
                tool_scope=list(parent_context.state.visible_tool_names),
                model_class="small",
                branch_group_id="root-frontier",
                metadata={
                    "parent_run_node_id": parent_context.trace_context.run_node_id if parent_context.trace_context else None,
                    "merge_priority": branch_plan.merge_priority,
                },
            )
        frame = branch_context.active_frame
        operations = [self._plan_node_by_id(plan, node_id) for node_id in frame.operation_ids]
        try:
            output, _, checkpoint = branch_runtime._execute_isolated_frame(
                branch_context,
                frame,
                operations,
                isolate_runtime_state=False,
            )
            branch_context.raise_if_cancelled()
            if (
                branch_budget.calls > branch_plan.reserved_budget.model_calls_max
                or branch_budget.checks > branch_plan.reserved_budget.checks_max
                or branch_budget.latency > branch_plan.reserved_budget.latency_max + 1e-9
            ):
                return finalize_branch_result(
                    self._failed_branch_result(
                        branch_plan,
                        branch_context,
                        failure_kind="reservation_exceeded",
                        failure_details={
                            "reserved_budget": (branch_plan.reserved_budget).model_dump(),
                            "budget_consumed": self._branch_budget_consumed(branch_context),
                        },
                        error=f"branch {branch_plan.branch_id} exceeded reserved budget",
                        artifact=output,
                    ),
                    boundary="after_branch_completion",
                )
        except BranchCancelled:
            cancellation_reason = str(
                getattr(cancellation_event, "reason", "parent_stop_policy") or "parent_stop_policy"
            )
            cancellation_details = dict(getattr(cancellation_event, "details", {}) or {})
            return finalize_branch_result(
                self._cancelled_branch_result(
                    branch_plan,
                    branch_context,
                    len(branch_plan.assigned_node_ids),
                    reason=cancellation_reason,
                    details=cancellation_details,
                ),
                boundary="after_branch_completion",
            )
        except ResumeRecoveryError:
            raise
        except Exception as exc:
            failure_kind, failure_details = self._classify_branch_failure(plan, branch_plan, branch_context, exc)
            return finalize_branch_result(
                self._failed_branch_result(
                    branch_plan,
                    branch_context,
                    failure_kind=failure_kind,
                    failure_details=failure_details,
                    error=str(exc),
                ),
                boundary="after_branch_completion",
            )
        verifier_support = self._worker_support(task, output)
        unresolved_critical = 0 if output else len(branch_plan.assigned_node_ids)
        self._emit_branch_publication(
            branch_context,
            publication_kind="candidate_artifact",
            logical_key=f"{branch_plan.branch_id}.artifact",
            payload={
                "artifact": output,
                "summary": (checkpoint.summary).model_dump(),
                "predicted_solve": branch_plan.predicted_solve,
            },
            verifier_support=verifier_support,
            unresolved_critical=unresolved_critical,
        )
        branch_context.record(
            "branch_completed",
            branch_id=branch_plan.branch_id,
            frame_id=frame.frame_id,
            assigned_node_ids=list(branch_plan.assigned_node_ids),
        )
        return finalize_branch_result(
            self._completed_branch_result(
                branch_plan,
                branch_context,
                artifact=output,
                verifier_support=verifier_support,
                unresolved_critical=unresolved_critical,
            ),
            boundary="after_branch_completion",
        )
