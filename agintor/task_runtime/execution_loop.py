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
from ..memory_graph import LongTermGraph
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
    RuntimeSessionSeed,
    SideEffectReceipt,
    capability_scope_allows,
    plan_node_requires_default_provider,
    service_action_transport_compatibility,
    is_terminal_receipt,
    terminalize_receipt,
)
from ..utils import count_tokens_rough, ensure_directory, merge_provider_usage, now_ts, stable_hash


class ExecutionLoopMixin:
    def _run_execution_plan(
        self,
        task: BenchmarkTask,
        plan: ExecutionPlan,
        seed: int,
        *,
        checkpoint_envelope: CheckpointEnvelope | None = None,
        reconciliation_policy: str = "strict",
        session_seed: RuntimeSessionSeed | None = None,
    ) -> RunResult:
        with self._isolated_provider_environment():
            main_provider_usage_before = self._provider_usage_snapshot(self.provider)
            execution_provider_usage: dict[str, Any] = {}

            def build_result(
                artifact: Any,
                verifier_score: float,
                faults: int,
                hard_invalid: bool,
                invalid_reason: str | None,
                failure_kind: str | None,
            ) -> RunResult:
                provider_usage = merge_provider_usage(
                    execution_provider_usage,
                    self._provider_usage_delta(
                        main_provider_usage_before,
                        self._provider_usage_snapshot(self.provider),
                    ),
                )
                return self._build_run_result(
                    task,
                    plan,
                    seed,
                    artifact,
                    verifier_score,
                    faults,
                    start,
                    budget,
                    state,
                    trace,
                    hard_invalid,
                    invalid_reason,
                    failure_kind,
                    provider_usage=provider_usage,
                )

            task = (task).model_copy(deep=True)
            plan = (plan).model_copy(deep=True)
            episode_scope = None
            if task.transfer_scored:
                episode_scope = f"{getattr(task, 'episode_id', None) or task.task_id}::seed::{seed}"
            self.shell.reset_for_task(
                task.task_id,
                transfer_scored=task.transfer_scored,
                episode_id=episode_scope,
            )
            if session_seed is not None and checkpoint_envelope is None:
                self._apply_session_seed(session_seed)
            budget_payload = self._runtime_budget_overrides()
            budget_payload.update(plan.budget_overrides)
            budget = RuntimeBudget(**budget_payload)
            state = RuntimeState(
                request_id=plan.request_id,
                plan_id=plan.plan_id,
                execution_state="idle",
                visible_tool_names=sorted(self.shell.tool_registry.tools),
            )
            if hasattr(self.shell, "latest_runtime_event_sequence"):
                state.event_sequence_start = int(self.shell.latest_runtime_event_sequence() or 0)
            trace: list[dict[str, Any]] = []
            effective_trace_context = plan.trace_context
            resume_origin_request_id = (
                str(
                    getattr(checkpoint_envelope, "origin_request_id", "")
                    or getattr(checkpoint_envelope, "request_id", "")
                    or ""
                ).strip()
                if checkpoint_envelope is not None
                else ""
            )
            resume_source_checkpoint_ref = (
                self._selected_resume_checkpoint_ref(checkpoint_envelope)
                if checkpoint_envelope is not None
                else ""
            )
            context = PolicyContext(
                runtime_dir=self.runtime.runtime_dir,
                shell=self.shell,
                task=task,
                request_id=plan.request_id,
                plan=plan,
                trace_context=effective_trace_context,
                provider=self.provider,
                profile=self.runtime_profile,
                seed=seed,
                state=state,
                budget=budget,
                trace=trace,
                objective=plan.objective,
                runtime_backend=self.runtime_backend,
                side_effect_callback=self._persist_side_effect_receipt,
                checkpoint_callback=lambda boundary: self._publish_checkpoint_envelope(
                    context,
                    task,
                    plan,
                    seed,
                    boundary,
                    origin_request_id=resume_origin_request_id or None,
                    source_checkpoint_ref=resume_source_checkpoint_ref or None,
                ),
                cancellation_event=None,
            )
            artifact: Any = None
            faults = 0
            verifier_score = 0.0
            prev_best = 0.0
            verified_terminal = False
            stop_policy_requested = False
            start = time.perf_counter()
            context.record("run_started", runtime_hash=self.runtime.runtime_hash, state=state.execution_state)
            state.execution_state = "compiling"
            context.record("plan_compiled", plan_digest=plan.plan_digest, lifecycle_state=plan.lifecycle_state)
            state.execution_state = "validating"
            try:
                plan = self._validate_execution_plan(plan)
            except ValueError as exc:
                state.execution_state = "failed"
                context.record("plan_validation_failed", error=str(exc), failure_class="plan_validation")
                context.record("run_failed", error=str(exc), failure_class="plan_validation", failure_kind="plan_validation_failed")
                return build_result({"error": "plan_validation_failed"}, 0.0, faults, True, str(exc), "plan_validation_failed")
            plan = (plan).model_copy(update={"lifecycle_state": "validated"})
            context.plan = plan
            context.record("plan_loaded", root_node_ids=list(plan.root_node_ids), terminal_output_keys=list(plan.terminal_output_keys))
            self._ingest_context(context)
            if checkpoint_envelope is not None:
                try:
                    self._restore_from_checkpoint(context, checkpoint_envelope, reconciliation_policy=reconciliation_policy)
                except ResumeRecoveryError as exc:
                    state.execution_state = "failed"
                    self._record_failed_recovery_attempt(
                        context,
                        checkpoint_envelope,
                        selected_checkpoint_ref=resume_source_checkpoint_ref,
                        reconciliation_policy=reconciliation_policy,
                        failure_explanation=str(exc),
                    )
                    context.record("run_failed", error=str(exc), failure_class="resume_recovery", failure_kind=exc.failure_kind)
                    return build_result({"error": str(exc)}, 0.0, faults, True, str(exc), exc.failure_kind)
                self.shell.restore_runtime_event_cursor(context.state.event_sequence_no)
                context.record("checkpoint_restored", checkpoint_id=checkpoint_envelope.checkpoint_id)
            else:
                root = self.shell.agent_pool.clone("root")
                executable_node_ids = [node.node_id for node in self._execution_nodes(plan)]
                state.queue.append(
                    AgentFrame(
                        frame_id=stable_hash(plan.request_id, "root", seed)[:16],
                        agent=root,
                        request_id=plan.request_id,
                        plan_id=plan.plan_id,
                        trace_context=effective_trace_context,
                        objective=plan.objective,
                        operation_ids=executable_node_ids,
                        depth=0,
                        role="root",
                        tool_scope=state.visible_tool_names,
                        model_class="medium",
                    )
                )
            state.execution_state = "running"
            try:
                step = 0
                while state.queue and step < self.runtime_profile.execution.max_steps:
                    step += 1
                    self.shell.validate_invariants(transfer_scored=task.transfer_scored)
                    self._compact_if_needed(context)
                    frame = state.queue.pop(0)
                    context.active_frame = frame
                    self.shell.agent_pool.assert_clone(frame.agent)
                    if frame.depth == 0 or frame.role.startswith("merge"):
                        frame.metadata["run_node_id"] = self._start_agent_run(self.shell.short_term, frame, step, frame.checkpoint)
                    context.record(
                        "node_started",
                        step=step,
                        frame_id=frame.frame_id,
                        node_id=frame.metadata.get("run_node_id"),
                        branch_id=frame.worker_id,
                        agent_id=frame.agent.agent_id,
                        frame_role=frame.role,
                        depth=frame.depth,
                        op_ids=frame.operation_ids,
                    )
                    if frame.role == "merge_vertical":
                        artifact = self._artifact_for_output_keys(plan.terminal_output_keys, state.artifacts)
                        if self._all_outputs_present(plan, state.artifacts):
                            artifact, verifier_score, verified_terminal = self._resolve_terminal_progress(
                                context,
                                frame,
                                task,
                                plan,
                                artifact,
                                verifier_score,
                                verified_terminal,
                            )
                        self._record_artifact_node(self.shell.short_term, "final", artifact, frame.metadata.get("run_node_id"))
                        context.record("merge_completed", artifact=artifact, merge_kind="vertical")
                    elif frame.role == "merge_horizontal":
                        state.execution_state = "merging"
                        worker_outputs = frame.metadata.get("worker_outputs", [])
                        frontier_nodes = [
                            self._plan_node_by_id(plan, node_id)
                            for node_id in frame.metadata.get("frontier_node_ids", [])
                        ]
                        context.record(
                            "merge_started",
                            frame_id=frame.frame_id,
                            merge_kind="horizontal",
                            frontier_node_ids=[node.node_id for node in frontier_nodes],
                        )
                        merged_artifact = self.runtime.topology.merge_ensemble(context, worker_outputs)
                        self._apply_horizontal_frontier_outputs(context, frontier_nodes, merged_artifact)
                        self._record_artifact_node(self.shell.short_term, "ensemble", merged_artifact, frame.metadata.get("run_node_id"))
                        if self._all_outputs_present(plan, state.artifacts):
                            artifact, verifier_score, verified_terminal = self._resolve_terminal_progress(
                                context,
                                frame,
                                task,
                                plan,
                                self._artifact_for_output_keys(plan.terminal_output_keys, state.artifacts),
                                verifier_score,
                                verified_terminal,
                            )
                            if artifact is not None:
                                state.execution_state = "completing"
                            else:
                                state.execution_state = "running"
                        else:
                            artifact = None
                            self._queue_root_continuation(context, frame)
                            state.execution_state = "running"
                        context.record("merge_completed", artifact=merged_artifact, merge_kind="horizontal")
                    elif frame.depth == 0:
                        artifact, local_faults, verifier_score, verified_terminal = self._run_root_frame(
                            context,
                            frame,
                            task,
                            plan,
                            verifier_score,
                            verified_terminal,
                            execution_provider_usage,
                        )
                        faults += local_faults
                    else:
                        operations = [self._plan_node_by_id(plan, op_id) for op_id in frame.operation_ids]
                        output, local_faults, checkpoint = self._execute_isolated_frame(context, frame, operations)
                        faults += local_faults
                        self._store_output_artifacts(state, operations, output)
                        state.checkpoints[self._checkpoint_key(frame)] = checkpoint
                        context.record("node_completed", role=frame.role, outputs=list(state.artifacts.keys()))
                    explicit_verify = self._resolved_verify_status(plan, state.artifacts)
                    if explicit_verify is not None:
                        verifier_score = float(explicit_verify.get("verifier_score", verifier_score) or 0.0)
                        verified_terminal = bool(explicit_verify.get("verified", verifier_score >= 1.0))
                    unresolved = [
                        output_key
                        for output_key in plan.terminal_output_keys
                        if output_key not in state.artifacts and not (isinstance(artifact, dict) and output_key in artifact)
                    ]
                    state.unresolved_goals = unresolved
                    terminal_ready = verified_terminal or not plan.execution_flags.requires_terminal_verification
                    best_optimistic = self._best_next_action_utility(context, unresolved, terminal_ready)
                    self._update_subgoal_progress(context, unresolved, best_optimistic, prev_best, terminal_ready)
                    if state.queue and self._has_pending_plan_nodes(plan, state):
                        prev_best = best_optimistic
                        context.active_frame = None
                        continue
                    if self.runtime.control.stop_policy(context, best_optimistic, prev_best, len(unresolved), terminal_ready):
                        stop_policy_requested = True
                        break
                    prev_best = best_optimistic
                    context.active_frame = None
                if state.execution_state != "cancelled":
                    state.execution_state = "completing"
                if artifact is None and state.artifacts:
                    artifact = self._terminal_artifact(plan, state.artifacts)
                    explicit_verify = self._resolved_verify_status(plan, state.artifacts)
                    if explicit_verify is not None:
                        verifier_score = float(explicit_verify.get("verifier_score", verifier_score) or 0.0)
                        verified_terminal = bool(explicit_verify.get("verified", verifier_score >= 1.0))
                controlled_failure = False
                if stop_policy_requested and self._stop_policy_requires_cancellation(
                    plan,
                    artifact=artifact,
                    unresolved=state.unresolved_goals,
                    verified_terminal=verified_terminal,
                ):
                    state.execution_state = "cancelled"
                elif not verified_terminal and plan.execution_flags.requires_terminal_verification and not plan.execution_flags.allow_best_effort:
                    artifact = {"error": "controlled_failure"}
                    controlled_failure = True
                elif artifact is None and not plan.execution_flags.allow_best_effort:
                    artifact = {"error": "controlled_failure"}
                    controlled_failure = True
                if state.execution_state == "cancelled":
                    context.record(
                        "run_cancelled",
                        reason="parent_stop_policy" if stop_policy_requested else None,
                        unresolved=list(state.unresolved_goals),
                        latest_checkpoint_ref=state.latest_checkpoint_ref,
                    )
                elif controlled_failure:
                    state.execution_state = "failed"
                    context.record(
                        "run_failed",
                        error="controlled_failure",
                        failure_class="controlled_failure",
                        failure_kind="controlled_failure",
                        unresolved=list(state.unresolved_goals),
                        verified=verified_terminal,
                    )
                else:
                    context.record(
                        "terminal_emitted",
                        unresolved=list(state.unresolved_goals),
                        verified=verified_terminal,
                        terminal_ready=verified_terminal or not plan.execution_flags.requires_terminal_verification,
                        artifact=artifact,
                    )
                    state.execution_state = "completed"
                self._assert_terminal_exit_contract(context)
            except KeyboardInterrupt:
                state.execution_state = "cancelled"
                context.record("run_cancelled", reason="external_interrupt")
                return build_result({"error": "cancelled"}, 0.0, faults, False, "runtime execution cancelled", "external_interrupt")
            except ResumeRecoveryError as exc:
                state.execution_state = "failed"
                if checkpoint_envelope is not None:
                    self._record_failed_recovery_attempt(
                        context,
                        checkpoint_envelope,
                        selected_checkpoint_ref=resume_source_checkpoint_ref,
                        reconciliation_policy=reconciliation_policy,
                        failure_explanation=str(exc),
                    )
                context.record("run_failed", error=str(exc), failure_class="resume_recovery", failure_kind=exc.failure_kind)
                return build_result({"error": str(exc)}, 0.0, faults, True, str(exc), exc.failure_kind)
            except HardInvalidation as exc:
                state.execution_state = "failed"
                context.record("run_failed", error=str(exc), failure_class="hard_invalidation")
                return build_result({"error": str(exc)}, 0.0, faults, True, str(exc), "hard_invalidation")
            return build_result(artifact, verifier_score, faults, False, None, None)

    def _run_root_frame(
        self,
        context: PolicyContext,
        frame: AgentFrame,
        task: BenchmarkTask,
        plan: ExecutionPlan,
        verifier_score: float,
        verified_terminal: bool,
        provider_usage_ledger: dict[str, Any],
    ) -> tuple[Any, int, float, bool]:
        faults = 0
        artifact: Any = None
        frontier_nodes = self._active_runnable_frontier(context, plan, branch_group_id=frame.branch_group_id)
        if not frontier_nodes:
            if self._all_outputs_present(plan, context.state.artifacts):
                artifact = self._terminal_artifact(plan, context.state.artifacts)
                artifact, verifier_score, verified_terminal = self._resolve_terminal_progress(
                    context,
                    frame,
                    task,
                    plan,
                    artifact,
                    verifier_score,
                    verified_terminal,
                )
                return artifact, faults, verifier_score, verified_terminal
            return None, faults, verifier_score, verified_terminal
        candidate_nodes = frontier_nodes
        mode = self.runtime.topology.select_mode(context, frame, candidate_nodes)
        context.state.mode = mode
        context.record("mode_selected", mode=mode, plan_id=plan.plan_id)
        if mode == "single":
            _, local_faults = self._execute_operations(context, frame, self._ordered_execution_nodes(plan))
            faults += local_faults
            artifact = self._terminal_artifact(plan, context.state.artifacts)
            artifact, verifier_score, verified_terminal = self._resolve_terminal_progress(
                context,
                frame,
                task,
                plan,
                artifact,
                verifier_score,
                verified_terminal,
            )
            return artifact, faults, verifier_score, verified_terminal
        if mode == "vertical":
            context.state.execution_state = "running"
            children = self.runtime.topology.propose_children(context, frame, candidate_nodes)
            for child in children:
                agent = self._resolve_agent(context, child)
                tool_scope = self.runtime.topology.assign_scope(context, child, context.state.visible_tool_names)
                context.state.queue.append(
                    AgentFrame(
                        frame_id=stable_hash(plan.request_id, child.child_id, len(context.state.queue))[:16],
                        agent=agent,
                        request_id=plan.request_id,
                        plan_id=plan.plan_id,
                        trace_context=context.derive_trace_context(
                            agent_id=agent.agent_id,
                            frame_role=child.role,
                            worker_id=child.child_id,
                        ),
                        objective=child.instruction,
                        operation_ids=[child.init_summary.get("op_id", child.child_id)],
                        depth=frame.depth + 1,
                        parent_id=frame.agent.agent_id,
                        role=child.role,
                        tool_scope=tool_scope,
                        model_class=child.model_class,
                        metadata={"child_spec": (child).model_dump(), "parent_run_node_id": frame.metadata.get("run_node_id")},
                    )
                )
            self._schedule_root_continuation(context, frame, append=True)
            return artifact, faults, verifier_score, verified_terminal
        frontier_nodes = self._active_runnable_frontier(context, plan, branch_group_id=frame.branch_group_id)
        if len(frontier_nodes) < 2:
            _, local_faults = self._execute_operations(context, frame, frontier_nodes)
            faults += local_faults
            if self._all_outputs_present(plan, context.state.artifacts):
                final_artifact = self._terminal_artifact(plan, context.state.artifacts)
                artifact, verifier_score, verified_terminal = self._resolve_terminal_progress(
                    context,
                    frame,
                    task,
                    plan,
                    final_artifact,
                    verifier_score,
                    verified_terminal,
                )
            else:
                artifact = None
                self._queue_root_continuation(context, frame)
            return artifact, faults, verifier_score, verified_terminal
        restored_worker_outputs = self._restored_branch_frontier(context, frame)
        if restored_worker_outputs is None:
            branch_snapshots = self._restorable_branch_snapshots(context, frame)
            context.state.execution_state = "branching"
            if branch_snapshots:
                worker_outputs, local_faults = self._resume_horizontal_branches(
                    context,
                    frame,
                    task,
                    plan,
                    provider_usage_ledger,
                )
            else:
                workers = self.runtime.topology.select_workers(context, frame, frontier_nodes)
                worker_outputs, local_faults = self._execute_horizontal_branches(
                    context,
                    frame,
                    task,
                    plan,
                    workers,
                    provider_usage_ledger,
                )
            faults += local_faults
            if worker_outputs is None:
                context.state.execution_state = "running"
                _, local_faults = self._execute_operations(context, frame, frontier_nodes)
                faults += local_faults
                if self._all_outputs_present(plan, context.state.artifacts):
                    final_artifact = self._terminal_artifact(plan, context.state.artifacts)
                    artifact, verifier_score, verified_terminal = self._resolve_terminal_progress(
                        context,
                        frame,
                        task,
                        plan,
                        final_artifact,
                        verifier_score,
                        verified_terminal,
                    )
                else:
                    artifact = None
                    self._queue_root_continuation(context, frame)
                return artifact, faults, verifier_score, verified_terminal
        else:
            worker_outputs = restored_worker_outputs
            context.record(
                "branch_frontier_restored",
                parent_frame_id=frame.frame_id,
                branch_count=len(restored_worker_outputs),
            )
        merge_node = next(
            (
                node
                for node in plan.nodes
                if str(node.node_kind) == "merge"
                and str(node.metadata.get("consumes_branch_group", "") or "").strip() == str(frontier_nodes[0].branch_group_id or "")
            ),
            None,
        )
        if merge_node is None:
            raise HardInvalidation("branchable frontier is missing an explicit merge node")
        for node in frontier_nodes:
            context.state.plan_node_status[node.node_id] = "completed"
        context.state.worker_plans[merge_node.node_id] = {
            "worker_outputs": worker_outputs,
            "frontier_node_ids": [node.node_id for node in frontier_nodes],
            "parent_run_node_id": frame.metadata.get("run_node_id"),
        }
        context.state.execution_state = "running"
        self._queue_root_continuation(context, frame)
        return artifact, faults, verifier_score, verified_terminal

    def _stop_policy_requires_cancellation(
        self,
        plan: ExecutionPlan,
        *,
        artifact: Any,
        unresolved: Sequence[str],
        verified_terminal: bool,
    ) -> bool:
        terminal_ready = verified_terminal or not plan.execution_flags.requires_terminal_verification
        if unresolved:
            return True
        if artifact is None:
            return True
        return not terminal_ready

    def _assert_terminal_exit_contract(self, context: PolicyContext) -> None:
        final_state = str(context.state.execution_state or "").strip()
        expected_event = {
            "completed": "terminal_emitted",
            "failed": "run_failed",
            "cancelled": "run_cancelled",
        }.get(final_state)
        if expected_event is None:
            raise HardInvalidation(f"illegal terminal execution state {final_state!r}")
        terminal_events = [
            str(row.get("event", "") or "")
            for row in context.trace
            if str(row.get("event", "") or "") in {"terminal_emitted", "run_failed", "run_cancelled"}
        ]
        if len(terminal_events) != 1 or terminal_events[0] != expected_event:
            raise HardInvalidation(
                f"terminal exit contract violated for {context.request_id}: state={final_state!r}, events={terminal_events!r}"
            )

    def _update_subgoal_progress(
        self,
        context: PolicyContext,
        unresolved: Sequence[str],
        best_optimistic: float,
        previous_best_utility: float,
        verified_terminal: bool,
    ) -> None:
        current_goal = unresolved[0] if unresolved else None
        previous_goal = context.state.last_unresolved_goal
        if previous_goal and previous_goal not in unresolved:
            context.state.subgoal_negative_steps.pop(previous_goal, None)
        if current_goal is None or verified_terminal:
            context.state.last_unresolved_goal = None
            return
        if current_goal == previous_goal and best_optimistic < previous_best_utility:
            context.state.subgoal_negative_steps[current_goal] = context.state.subgoal_negative_steps.get(current_goal, 0) + 1
        elif current_goal != previous_goal:
            context.state.subgoal_negative_steps[current_goal] = 0
        context.state.last_unresolved_goal = current_goal
