from __future__ import annotations

import time
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
from .memory_graph import LongTermGraph
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
    RuntimeSessionSeed,
    SideEffectReceipt,
    capability_scope_allows,
    plan_node_requires_default_provider,
    service_action_transport_compatibility,
    is_terminal_receipt,
    terminalize_receipt,
)
from ...utils import count_tokens_rough, ensure_directory, merge_provider_usage, now_ts, stable_hash

class RuntimeLoopMixin:
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
