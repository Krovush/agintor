from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from threading import Event, Lock
from typing import Any, Mapping, Sequence
from ..exceptions import BranchCancelled, HardInvalidation, ProviderExhaustedError, ResumeRecoveryError
from ..providers import (
    ModelProvider,
    ReplayProvider,
    clone_provider,
    known_provider_environment_names,
    provider_environment_names,
    provider_environment_names_for_instance,
)
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


def _clone_provider(provider, *, provider_profile=None):
    from .. import runner as runner_facade

    return runner_facade.clone_provider(provider, provider_profile=provider_profile)


class BranchExecutionMixin:
    def _branch_minimum_model_calls(self, plan: ExecutionPlan, worker: Mapping[str, Any]) -> int:
        count = 0
        for node_id in worker.get("op_ids", []):
            node = self._plan_node_by_id(plan, str(node_id))
            descriptor = get_plan_node_descriptor(str(node.node_kind))
            if descriptor.provider_backed_metadata_key:
                if bool(node.metadata.get(descriptor.provider_backed_metadata_key)):
                    count += 1
            elif descriptor.requires_default_provider:
                count += 1
        return count

    def _branch_minimum_latency(self, plan: ExecutionPlan, worker: Mapping[str, Any]) -> float:
        branch_latency_floor_s = max(1.0, float(self.runtime_profile.execution.branch_latency_floor_s or 0.0))
        latency_slices = 0
        for node_id in worker.get("op_ids", []):
            node_kind = str(self._plan_node_by_id(plan, str(node_id)).node_kind)
            if node_kind in {"merge", "checkpoint", "memory_lookup"}:
                continue
            if node_kind in {"direct_response", "tool_synthesis", "tool_call", "repo_patch", "verify", "service_action"}:
                latency_slices += 1
        return float(latency_slices) * branch_latency_floor_s

    def _apportion_integer_budget(
        self,
        total: int,
        weights: Sequence[float],
        minimums: Sequence[int],
    ) -> list[int]:
        if not weights:
            return []
        if total < sum(minimums):
            return []
        allocations = list(minimums)
        remainder = total - sum(allocations)
        if remainder <= 0:
            return allocations
        normalized_weights = [max(0.0, float(weight)) for weight in weights]
        weight_total = sum(normalized_weights) or float(len(normalized_weights))
        ideal_extras = [(weight / weight_total) * remainder for weight in normalized_weights]
        base_extras = [int(value) for value in ideal_extras]
        allocations = [allocation + extra for allocation, extra in zip(allocations, base_extras)]
        assigned = sum(base_extras)
        order = sorted(
            range(len(normalized_weights)),
            key=lambda idx: (
                ideal_extras[idx] - base_extras[idx],
                normalized_weights[idx],
                -idx,
            ),
            reverse=True,
        )
        for idx in order[: remainder - assigned]:
            allocations[idx] += 1
        return allocations

    def _apportion_latency_budget(
        self,
        total: float,
        weights: Sequence[float],
        minimums: Sequence[float],
    ) -> list[float]:
        if not weights:
            return []
        minimum_total = sum(float(value) for value in minimums)
        if float(total) + 1e-9 < minimum_total:
            return []
        allocations = [float(value) for value in minimums]
        remainder = max(0.0, float(total) - minimum_total)
        if remainder <= 1e-9:
            return allocations
        normalized_weights = [max(0.0, float(weight)) for weight in weights]
        weight_total = sum(normalized_weights) or float(len(normalized_weights))
        remaining = remainder
        for index, weight in enumerate(normalized_weights):
            if index == len(normalized_weights) - 1:
                allocations[index] += max(0.0, remaining)
                break
            share = round((weight / weight_total) * remainder, 6)
            share = min(max(0.0, share), remaining)
            allocations[index] += share
            remaining -= share
        return allocations

    def _launchable_branch_plans(
        self,
        context: PolicyContext,
        frame: AgentFrame,
        plan: ExecutionPlan,
        workers: Sequence[dict[str, Any]],
    ) -> list[BranchPlan]:
        remaining_calls = context.budget.remaining_model_calls()
        remaining_checks = context.budget.remaining_checks()
        remaining_latency = context.budget.remaining_latency()
        ranked_workers = sorted(
            list(workers),
            key=lambda worker: (
                -float(worker.get("predicted_solve", 0.0) or 0.0),
                str(worker.get("worker_id", "")),
            ),
        )
        branch_specs: list[dict[str, Any]] = []
        for worker in ranked_workers:
            predicted_solve = max(0.01, float(worker.get("predicted_solve", 0.0) or 0.0))
            branch_specs.append(
                {
                    "worker": worker,
                    "weight": predicted_solve,
                    "min_calls": self._branch_minimum_model_calls(plan, worker),
                    "min_checks": 0,
                    "min_latency": self._branch_minimum_latency(plan, worker),
                }
            )
        while branch_specs and (
            sum(spec["min_calls"] for spec in branch_specs) > remaining_calls
            or sum(spec["min_checks"] for spec in branch_specs) > remaining_checks
            or sum(spec["min_latency"] for spec in branch_specs) > remaining_latency + 1e-9
        ):
            branch_specs.pop()
        if not branch_specs:
            return []
        call_allocations = self._apportion_integer_budget(
            remaining_calls,
            [spec["weight"] for spec in branch_specs],
            [spec["min_calls"] for spec in branch_specs],
        )
        check_allocations = self._apportion_integer_budget(
            remaining_checks,
            [spec["weight"] for spec in branch_specs],
            [spec["min_checks"] for spec in branch_specs],
        )
        latency_allocations = self._apportion_latency_budget(
            remaining_latency,
            [spec["weight"] for spec in branch_specs],
            [spec["min_latency"] for spec in branch_specs],
        )
        if not call_allocations or not check_allocations or not latency_allocations:
            return []
        branch_plans: list[BranchPlan] = []
        for index, spec in enumerate(branch_specs):
            worker = spec["worker"]
            branch_id = str(worker.get("worker_id", f"branch_{index}"))
            branch_plans.append(
                BranchPlan(
                    branch_id=branch_id,
                    parent_frame_id=frame.frame_id,
                    request_id=plan.request_id,
                    trace_context=context.derive_trace_context(
                        worker_id=branch_id,
                        frame_role="worker",
                        agent_id=str(worker.get("agent_id", "root")),
                    ),
                    assigned_node_ids=list(worker.get("op_ids", [])),
                    merge_priority=index,
                    predicted_solve=float(worker.get("predicted_solve", 0.0) or 0.0),
                    reserved_budget=BranchBudget(
                        model_calls_max=call_allocations[index],
                        checks_max=check_allocations[index],
                        latency_max=latency_allocations[index],
                        allow_tool_synthesis=bool(plan.execution_flags.allow_tool_synthesis),
                    ),
                    cancel_on_parent_stop=True,
                )
            )
        return branch_plans

    def _branch_provider_overrides(self) -> dict[str, ModelProvider]:
        overrides = getattr(self, "_replay_branch_providers", None)
        if overrides is None:
            overrides = {}
            setattr(self, "_replay_branch_providers", overrides)
        return overrides

    def _prepare_branch_provider(
        self,
        plan: ExecutionPlan,
        branch_plan: BranchPlan,
        resume_snapshot: BranchResumeSnapshot | None = None,
    ) -> tuple[BranchPlan, ModelProvider]:
        if not isinstance(self.provider, ReplayProvider):
            return branch_plan, _clone_provider(
                self.provider,
                provider_profile=self.runtime_profile.runtime_provider,
            )
        allocation = branch_plan.replay_allocation
        if allocation is not None and self.provider.can_apply_allocation(allocation):
            prepared_plan = model_copy(
                branch_plan,
                update={"replay_allocation": allocation},
                deep=True,
            )
            return prepared_plan, self.provider.clone_for_allocation(allocation)
        provider_node_ids = {
            str(node_id)
            for node_id in branch_plan.assigned_node_ids
            if plan_node_requires_default_provider(self._plan_node_by_id(plan, str(node_id)))
        }
        provider_calls = len(provider_node_ids)
        if resume_snapshot is not None:
            completed_nodes = {
                str(node_id)
                for node_id, status in dict(resume_snapshot.plan_node_status).items()
                if str(status) == "completed"
            }
            unresolved_provider_launch_nodes = {
                str(receipt.node_id or "")
                for receipt in resume_snapshot.side_effect_receipts
                if receipt.action_kind == "provider_request"
                and not is_terminal_receipt(receipt)
                and str(receipt.node_id or "").strip()
            }
            remaining_provider_nodes = provider_node_ids - completed_nodes - unresolved_provider_launch_nodes
            provider_calls = len(remaining_provider_nodes)
        if provider_calls <= 0:
            return branch_plan, _clone_provider(
                self.provider,
                provider_profile=self.runtime_profile.runtime_provider,
            )
        allocation_key = (
            allocation.allocation_key
            if allocation is not None and str(allocation.allocation_key or "").strip()
            else f"{plan.request_id}:{branch_plan.branch_id}"
        )
        try:
            allocation = self.provider.reserve_rows(
                provider_calls,
                allocation_key=allocation_key,
            )
        except ProviderExhaustedError as exc:
            raise HardInvalidation(
                f"replay allocation exhausted for branch {branch_plan.branch_id}: need {provider_calls} rows"
            ) from exc
        prepared_plan = model_copy(
            branch_plan,
            update={"replay_allocation": allocation},
            deep=True,
        )
        return prepared_plan, self.provider.clone_for_allocation(allocation)

    @staticmethod
    def _branch_plan_with_updated_replay_cursor(
        branch_plan: BranchPlan,
        branch_provider: ModelProvider,
    ) -> BranchPlan:
        if not isinstance(branch_provider, ReplayProvider):
            return branch_plan
        allocation = branch_provider.current_allocation()
        if allocation is None:
            return branch_plan
        allocation_key = (
            branch_plan.replay_allocation.allocation_key
            if branch_plan.replay_allocation is not None
            else f"{branch_plan.request_id}:{branch_plan.branch_id}"
        )
        return model_copy(
            branch_plan,
            update={"replay_allocation": allocation.copy(update={"allocation_key": allocation_key}, deep=True)},
            deep=True,
        )

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
            branch_state = model_validate(BranchState, payload)
            if branch_state.parent_frame_id != frame.frame_id:
                continue
            if branch_state.status not in {"completed", "cancelled", "failed"}:
                continue
            publications = branch_state.publications or [
                model_validate(BranchPublication, publication_payload)
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
                        model_validate(SideEffectReceipt, receipt_payload)
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
                model_validate(BranchPlan, model_dump(snapshot.branch_plan)),
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
            context.state.branch_states[branch_plan.branch_id] = model_dump(
                BranchState(
                    branch_id=branch_plan.branch_id,
                    status="pending",
                    parent_frame_id=frame.frame_id,
                    assigned_node_ids=list(branch_plan.assigned_node_ids),
                    merge_priority=branch_plan.merge_priority,
                    predicted_solve=branch_plan.predicted_solve,
                    reserved_budget=branch_plan.reserved_budget,
                )
            )
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
            future_map = {
                provider_overrides.__setitem__(prepared_branch_plan.branch_id, branch_provider) or
                executor.submit(self._run_branch_plan, context, task, plan, prepared_branch_plan, cancellation_event, persist_lock): prepared_branch_plan
                for prepared_branch_plan, branch_provider in prepared_branches
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
            finalized = result.copy(
                update={"branch_plan": finalized_branch_plan, "provider_usage": provider_usage},
                deep=True,
            )
            with persist_lock:
                parent_context.state.branch_states[finalized.branch_plan.branch_id] = model_dump(finalized.branch_state)
                parent_context.state.branch_resume_snapshots.pop(finalized.branch_plan.branch_id, None)
                existing_ids = {
                    str(payload.get("publication_id", ""))
                    for payload in parent_context.state.branch_publications
                }
                for publication in finalized.branch_state.publications:
                    payload = model_dump(publication)
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
                parent_context.state.branch_states[persisted_branch_plan.branch_id] = model_dump(
                    BranchState(
                        branch_id=persisted_branch_plan.branch_id,
                        status="running",
                        parent_frame_id=persisted_branch_plan.parent_frame_id,
                        assigned_node_ids=list(persisted_branch_plan.assigned_node_ids),
                        merge_priority=persisted_branch_plan.merge_priority,
                        predicted_solve=persisted_branch_plan.predicted_solve,
                        reserved_budget=persisted_branch_plan.reserved_budget,
                        publications=self._branch_publications_snapshot(branch_context),
                        budget_consumed=self._branch_budget_consumed(branch_context),
                    )
                )
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
                    BranchResult(
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
                        artifact=output,
                        verifier_support=verifier_support,
                        unresolved_critical=unresolved_critical,
                        side_effect_receipts=self._branch_receipts_snapshot(branch_context),
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
                            "reserved_budget": model_dump(branch_plan.reserved_budget),
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
                "summary": model_dump(checkpoint.summary),
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
            BranchResult(
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
                artifact=output,
                verifier_support=verifier_support,
                unresolved_critical=unresolved_critical,
                side_effect_receipts=self._branch_receipts_snapshot(branch_context),
            ),
            boundary="after_branch_completion",
        )

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
                receipt = model_validate(SideEffectReceipt, receipt_payload)
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
            branch_context.state.side_effect_receipts = [model_dump(receipt) for receipt in receipt_updates]
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
                model_validate(BranchPublication, payload)
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
