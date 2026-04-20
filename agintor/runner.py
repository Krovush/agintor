from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import copy
import difflib
import os
import json
import tempfile
import time
from urllib import error as urllib_error
from urllib import request as urllib_request
from threading import Event, Lock
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping, Sequence

from .exceptions import BranchCancelled, HardInvalidation, ProviderExhaustedError, ResumeRecoveryError
from .memory_graph import ShortTermGraph
from .providers import (
    ModelProvider,
    ReplayProvider,
    clone_provider,
    known_provider_environment_names,
    provider_environment_names,
    provider_environment_names_for_instance,
)
from .runtime_profile import RuntimeProfile, load_runtime_profile
from .runtime_api import (
    AgentFrame,
    PolicyContext,
    RuntimeBudget,
    RuntimeState,
    compile_execution_plan_from_task,
    get_plan_node_descriptor,
    normalize_benchmark_request_id,
)
from .runtime_loader import LoadedRuntime
from .pydantic_compat import model_copy, model_dump, model_validate
from .schemas import (
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
from .shell import FixedShell
from .tool_runtime import _signature_arg_names
from .utils import count_tokens_rough, ensure_directory, merge_provider_usage, now_ts, stable_hash
from .verifiers import run_checker, verify_task


def _category_allowed(allowed_categories: Sequence[str], category_key: str | None) -> bool:
    return capability_scope_allows(allowed_categories, category_key)


class TaskRuntime:
    def __init__(
        self,
        runtime: LoadedRuntime,
        shell: FixedShell,
        provider: ModelProvider,
        budget_overrides: Mapping[str, Any] | None = None,
        runtime_profile: RuntimeProfile | None = None,
        runtime_backend: str | None = None,
    ) -> None:
        self.runtime = runtime
        self.shell = shell
        self.provider = provider
        self.budget_overrides = dict(budget_overrides or {})
        self.runtime_profile = runtime_profile or load_runtime_profile(runtime.runtime_dir)
        self.runtime_backend = str(
            runtime_backend or os.environ.get("AGINTOR_RUNTIME_BACKEND", "local")
        ).strip().lower()

    def _runtime_budget_overrides(self) -> dict[str, Any]:
        profile = self.runtime_profile.execution
        overrides = {
            "C_max": profile.cost_max,
            "L_max": profile.latency_max,
            "M_max": profile.model_calls_max,
            "Q_max": profile.checks_max,
            "context_window_tokens": profile.context_window_tokens,
        }
        overrides.update(self.budget_overrides)
        return overrides

    @contextmanager
    def _isolated_provider_environment(self):
        known_envs = set(known_provider_environment_names(include_api_key_file_env=True))
        known_envs.update(
            provider_environment_names(
                self.runtime_profile.runtime_provider.name,
                provider_profile=self.runtime_profile.runtime_provider,
                include_api_key_file_env=True,
            )
        )
        selected_envs = set(provider_environment_names_for_instance(self.provider))
        removed: dict[str, str] = {}
        for env_name in sorted(known_envs - selected_envs):
            if env_name in os.environ:
                removed[env_name] = os.environ.pop(env_name)
        try:
            yield
        finally:
            for env_name, value in removed.items():
                os.environ[env_name] = value

    @staticmethod
    def _provider_usage_snapshot(provider: ModelProvider | None) -> dict[str, Any]:
        if provider is None:
            return {}
        return dict(provider.usage_summary())

    @classmethod
    def _provider_usage_delta(
        cls,
        before: Mapping[str, Any] | None,
        after: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        delta: dict[str, Any] = {}
        before_payload = dict(before or {})
        after_payload = dict(after or {})
        for key in sorted(set(before_payload) | set(after_payload)):
            previous = before_payload.get(key, 0)
            current = after_payload.get(key, 0)
            if isinstance(previous, (int, float)) and isinstance(current, (int, float)):
                delta[key] = current - previous
            else:
                delta[key] = current
        return delta

    @staticmethod
    def _merge_provider_usage_into(
        target: dict[str, Any],
        payload: Mapping[str, Any] | None,
    ) -> None:
        merged = merge_provider_usage(dict(target), payload)
        target.clear()
        target.update(merged)

    def run_task(
        self,
        task: BenchmarkTask,
        seed: int,
        *,
        request_id: str | None = None,
        trace_context: Any | None = None,
        plan: ExecutionPlan | None = None,
    ) -> RunResult:
        normalized_request_id = request_id or normalize_benchmark_request_id(task.task_id, seed)
        compiled_plan = plan or compile_execution_plan_from_task(
            task,
            request_id=normalized_request_id,
            seed=seed,
            runtime_hash=self.runtime.runtime_hash,
            runtime_dir=str(self.runtime.runtime_dir),
            trace_context=trace_context,
            budget_overrides=self.budget_overrides,
        )
        return self._run_execution_plan(task, compiled_plan, seed)

    def resume_from_checkpoint(
        self,
        envelope: CheckpointEnvelope,
        *,
        reconciliation_policy: str = "strict",
    ) -> RunResult:
        task = model_validate(BenchmarkTask, envelope.task_payload)
        plan = model_validate(ExecutionPlan, envelope.plan_snapshot)
        return self._run_execution_plan(
            task,
            plan,
            envelope.seed,
            checkpoint_envelope=envelope,
            reconciliation_policy=reconciliation_policy,
        )

    def _run_execution_plan(
        self,
        task: BenchmarkTask,
        plan: ExecutionPlan,
        seed: int,
        *,
        checkpoint_envelope: CheckpointEnvelope | None = None,
        reconciliation_policy: str = "strict",
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

            task = model_copy(task, deep=True)
            plan = model_copy(plan, deep=True)
            episode_scope = None
            if task.transfer_scored:
                episode_scope = f"{getattr(task, 'episode_id', None) or task.task_id}::seed::{seed}"
            self.shell.reset_for_task(
                task.task_id,
                transfer_scored=task.transfer_scored,
                episode_id=episode_scope,
            )
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
                str(getattr(checkpoint_envelope, "source_checkpoint_ref", "") or "").strip()
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
            plan = model_copy(plan, update={"lifecycle_state": "validated"})
            context.plan = plan
            context.record("plan_loaded", root_node_ids=list(plan.root_node_ids), terminal_output_keys=list(plan.terminal_output_keys))
            self._ingest_context(context)
            if checkpoint_envelope is not None:
                try:
                    self._restore_from_checkpoint(context, checkpoint_envelope, reconciliation_policy=reconciliation_policy)
                except ResumeRecoveryError as exc:
                    state.execution_state = "failed"
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
                context.record("run_failed", error=str(exc), failure_class="resume_recovery", failure_kind=exc.failure_kind)
                return build_result({"error": str(exc)}, 0.0, faults, True, str(exc), exc.failure_kind)
            except HardInvalidation as exc:
                state.execution_state = "failed"
                context.record("run_failed", error=str(exc), failure_class="hard_invalidation")
                return build_result({"error": str(exc)}, 0.0, faults, True, str(exc), "hard_invalidation")
            return build_result(artifact, verifier_score, faults, False, None, None)

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

    def _persist_side_effect_receipt(self, receipt: SideEffectReceipt) -> None:
        self.shell.save_side_effect_receipt(model_validate(SideEffectReceipt, receipt))

    def _record_side_effect_receipt(self, context: PolicyContext, receipt: SideEffectReceipt) -> None:
        normalized = model_validate(SideEffectReceipt, receipt)
        self.shell.save_side_effect_receipt(normalized)
        deduped: list[dict[str, Any]] = []
        for payload in context.state.side_effect_receipts:
            same_idempotency = str(payload.get("idempotency_key", "")) == normalized.idempotency_key
            same_kind = str(payload.get("action_kind", "")) == normalized.action_kind
            if same_idempotency and same_kind and is_terminal_receipt(normalized):
                continue
            deduped.append(payload)
        deduped.append(model_dump(normalized))
        context.state.side_effect_receipts = deduped

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

    def _reconcile_side_effect_receipts(
        self,
        context: PolicyContext,
        receipts: Sequence[SideEffectReceipt],
        *,
        reconciliation_policy: str,
    ) -> tuple[list[SideEffectReceipt], set[str]]:
        resolved: list[SideEffectReceipt] = []
        blocked_node_ids: set[str] = set()
        resolved_terminal_keys: set[tuple[str, str]] = set()

        def append_resolved(receipt: SideEffectReceipt) -> None:
            terminal_key = (
                str(receipt.action_kind or ""),
                str(receipt.idempotency_key or ""),
            )
            if is_terminal_receipt(receipt):
                if terminal_key in resolved_terminal_keys:
                    return
                resolved_terminal_keys.add(terminal_key)
            resolved.append(receipt)

        terminal_by_key = {
            receipt.idempotency_key: receipt
            for receipt in receipts
            if is_terminal_receipt(receipt)
        }
        for receipt in receipts:
            if is_terminal_receipt(receipt):
                append_resolved(receipt)
                if receipt.status in {"failed", "abandoned"} and receipt.node_id:
                    blocked_node_ids.add(receipt.node_id)
                continue
            terminal_receipt = terminal_by_key.get(receipt.idempotency_key)
            if terminal_receipt is not None:
                append_resolved(terminal_receipt)
                if terminal_receipt.status in {"failed", "abandoned"} and terminal_receipt.node_id:
                    blocked_node_ids.add(terminal_receipt.node_id)
                context.record(
                    "side_effect_reconciled",
                    side_effect_id=terminal_receipt.side_effect_id,
                    action_kind=terminal_receipt.action_kind,
                    reconciliation_status="reused_terminal_receipt",
                )
                continue
            if receipt.action_kind == "tool_launch":
                result_ref = dict(receipt.result_ref or {})
                handle_id = str(result_ref.get("handle_id", "")).strip()
                launch_mode = str(result_ref.get("launch_mode", "")).strip().lower()
                handle = self.shell.open_handles.handles.get(handle_id) if handle_id else None
                if handle is not None:
                    reconciliation = (
                        self.shell.tool_executor.reconcile_async_handle(handle, self.shell.open_handles)
                        if hasattr(self.shell.tool_executor, "reconcile_async_handle")
                        else None
                    )
                    handle_status = str((reconciliation or {}).get("status", handle.state) or handle.state)
                    if handle_status in {"completed", "failed", "cancelled"}:
                        terminalized = terminalize_receipt(
                            receipt,
                            status="failed" if handle_status == "failed" else "reconciled" if handle_status == "completed" else "abandoned",
                            reconciliation_status="terminalized_from_handle",
                            reconciliation_source="resume_reconciliation",
                            reconciliation_details={
                                "handle_id": handle.handle_id,
                                "handle_state": handle_status,
                                "reconciliation_source": (reconciliation or {}).get("reconciliation_source"),
                            },
                            result_ref_updates={
                                "handle_id": handle.handle_id,
                                "output": (reconciliation or {}).get("output"),
                                "handle_state": handle_status,
                            },
                        )
                        append_resolved(terminalized)
                        if terminalized.status in {"failed", "abandoned"} and terminalized.node_id:
                            blocked_node_ids.add(terminalized.node_id)
                        context.record(
                            "side_effect_reconciled",
                            side_effect_id=terminalized.side_effect_id,
                            action_kind=terminalized.action_kind,
                            reconciliation_status=terminalized.reconciliation.status if terminalized.reconciliation else None,
                        )
                        continue
                    if launch_mode == "async":
                        if reconciliation_policy == "strict":
                            raise ResumeRecoveryError(
                                RecoveryFailureKind.RECEIPT_RECONCILIATION_FAILED.value,
                                f"strict resume requires durable reconciliation for async tool launch {receipt.side_effect_id}",
                            )
                        if receipt.node_id:
                            blocked_node_ids.add(receipt.node_id)
                        append_resolved(receipt)
                        context.record(
                            "side_effect_reconciled",
                            side_effect_id=receipt.side_effect_id,
                            action_kind=receipt.action_kind,
                            reconciliation_status="blocked_best_effort",
                        )
                        continue
                if launch_mode == "sync":
                    if reconciliation_policy == "strict":
                        raise ResumeRecoveryError(
                            RecoveryFailureKind.RECEIPT_RECONCILIATION_FAILED.value,
                            f"strict resume requires terminal proof for sync tool launch {receipt.side_effect_id}",
                        )
                    if receipt.node_id:
                        blocked_node_ids.add(receipt.node_id)
                    terminalized = terminalize_receipt(
                        receipt,
                        status="abandoned",
                        reconciliation_status="blocked_best_effort",
                        reconciliation_source="resume_reconciliation",
                        reconciliation_details={
                            "node_id": receipt.node_id,
                            "action_kind": receipt.action_kind,
                            "launch_mode": launch_mode,
                        },
                    )
                    append_resolved(terminalized)
                    context.record(
                        "side_effect_reconciled",
                        side_effect_id=terminalized.side_effect_id,
                        action_kind=terminalized.action_kind,
                        reconciliation_status="blocked_best_effort",
                    )
                    continue
            if receipt.action_kind == "provider_request" and hasattr(self.provider, "reconcile_request"):
                reconciled = self.provider.reconcile_request(receipt.idempotency_key, receipt)
                if reconciled is not None:
                    terminalized = terminalize_receipt(
                        receipt,
                        status="reconciled",
                        reconciliation_status="terminalized_from_provider_hook",
                        reconciliation_source="resume_reconciliation",
                        reconciliation_details={"idempotency_key": receipt.idempotency_key},
                        result_ref_updates=model_dump(reconciled) if hasattr(reconciled, "model_dump") else dict(reconciled),
                    )
                    append_resolved(terminalized)
                    context.record(
                        "side_effect_reconciled",
                        side_effect_id=terminalized.side_effect_id,
                        action_kind=terminalized.action_kind,
                        reconciliation_status=terminalized.reconciliation.status if terminalized.reconciliation else None,
                    )
                    continue
            if receipt.action_kind == "filesystem_write":
                reconciliation_state = self._filesystem_write_reconciliation_state(receipt)
                if reconciliation_state == "completed":
                    reconciled_receipt = receipt.copy(update={"status": "reconciled"}, deep=True)
                    append_resolved(reconciled_receipt)
                    context.record(
                        "side_effect_reconciled",
                        side_effect_id=reconciled_receipt.side_effect_id,
                        action_kind=reconciled_receipt.action_kind,
                        reconciliation_status="filesystem_state_matches_intent",
                    )
                    continue
                if reconciliation_state == "prewrite_intact":
                    append_resolved(receipt)
                    context.record(
                        "side_effect_reconciled",
                        side_effect_id=receipt.side_effect_id,
                        action_kind=receipt.action_kind,
                        reconciliation_status="filesystem_prewrite_state_intact",
                    )
                    continue
                if reconciliation_policy == "strict":
                    raise ResumeRecoveryError(
                        RecoveryFailureKind.RECEIPT_RECONCILIATION_FAILED.value,
                        f"strict resume requires filesystem writes to be fully reconciled or fully unapplied before reissue; unresolved receipt {receipt.side_effect_id}",
                    )
                if receipt.node_id:
                    blocked_node_ids.add(receipt.node_id)
                terminalized = terminalize_receipt(
                    receipt,
                    status="abandoned",
                    reconciliation_status="blocked_best_effort",
                    reconciliation_source="resume_reconciliation",
                    reconciliation_details={"node_id": receipt.node_id, "action_kind": receipt.action_kind},
                )
                append_resolved(terminalized)
                context.record(
                    "side_effect_reconciled",
                    side_effect_id=terminalized.side_effect_id,
                    action_kind=terminalized.action_kind,
                    reconciliation_status="blocked_best_effort",
                )
                continue
            if receipt.action_kind == "service_action":
                if reconciliation_policy == "strict":
                    raise ResumeRecoveryError(
                        RecoveryFailureKind.RECEIPT_RECONCILIATION_FAILED.value,
                        f"strict resume requires terminal proof for service action {receipt.side_effect_id}",
                    )
                if receipt.node_id:
                    blocked_node_ids.add(receipt.node_id)
                terminalized = terminalize_receipt(
                    receipt,
                    status="abandoned",
                    reconciliation_status="blocked_best_effort",
                    reconciliation_source="resume_reconciliation",
                    reconciliation_details={
                        "node_id": receipt.node_id,
                        "action_kind": receipt.action_kind,
                    },
                )
                append_resolved(terminalized)
                context.record(
                    "side_effect_reconciled",
                    side_effect_id=terminalized.side_effect_id,
                    action_kind=terminalized.action_kind,
                    reconciliation_status="blocked_best_effort",
                )
                continue
            if reconciliation_policy == "strict":
                raise ResumeRecoveryError(
                    RecoveryFailureKind.RECEIPT_RECONCILIATION_FAILED.value,
                    f"strict resume requires reconciled side effects; unresolved receipt {receipt.side_effect_id}",
                )
            if receipt.node_id:
                blocked_node_ids.add(receipt.node_id)
            terminalized = terminalize_receipt(
                receipt,
                status="abandoned",
                reconciliation_status="blocked_best_effort",
                reconciliation_source="resume_reconciliation",
                reconciliation_details={"node_id": receipt.node_id, "action_kind": receipt.action_kind},
            )
            append_resolved(terminalized)
            context.record(
                "side_effect_reconciled",
                side_effect_id=terminalized.side_effect_id,
                action_kind=terminalized.action_kind,
                reconciliation_status="blocked_best_effort",
            )
        return resolved, blocked_node_ids

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
                        metadata={"child_spec": model_dump(child), "parent_run_node_id": frame.metadata.get("run_node_id")},
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

    def _execution_nodes(self, plan: ExecutionPlan) -> list[PlanNode]:
        return list(plan.nodes)

    def _ordered_execution_nodes(self, plan: ExecutionPlan) -> list[PlanNode]:
        node_map = {node.node_id: node for node in self._execution_nodes(plan)}
        ordered: list[PlanNode] = []
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visited or node_id not in node_map:
                return
            for dependency_id in node_map[node_id].dependencies:
                visit(dependency_id)
            visited.add(node_id)
            ordered.append(node_map[node_id])

        for node in self._execution_nodes(plan):
            visit(node.node_id)
        return ordered

    def _active_runnable_frontier(
        self,
        context: PolicyContext,
        plan: ExecutionPlan,
        *,
        branch_group_id: str | None = None,
    ) -> list[PlanNode]:
        runnable = [
            node
            for node in self._ordered_execution_nodes(plan)
            if context.state.plan_node_status.get(node.node_id) != "completed"
            and all(context.state.plan_node_status.get(dep_id) == "completed" for dep_id in node.dependencies)
        ]
        if not runnable:
            return []
        if branch_group_id is not None:
            return [node for node in runnable if node.branch_group_id == branch_group_id]
        first_runnable = runnable[0]
        if not first_runnable.branch_group_id:
            return [first_runnable]
        return [node for node in runnable if node.branch_group_id == first_runnable.branch_group_id]

    def _apply_horizontal_frontier_outputs(
        self,
        context: PolicyContext,
        frontier_nodes: Sequence[PlanNode],
        artifact: Any,
    ) -> None:
        if len(frontier_nodes) == 1 and not isinstance(artifact, Mapping):
            artifact_payload = {frontier_nodes[0].output_key: artifact}
        elif isinstance(artifact, Mapping):
            artifact_payload = dict(artifact)
        else:
            raise HardInvalidation("horizontal merge must return a mapping for a multi-node frontier")
        for node in frontier_nodes:
            if node.output_key not in artifact_payload:
                raise HardInvalidation(
                    f"horizontal merge did not return required frontier output {node.output_key!r}"
                )
            context.state.artifacts[node.output_key] = artifact_payload[node.output_key]
            context.state.plan_node_status[node.node_id] = "completed"

    def _schedule_root_continuation(
        self,
        context: PolicyContext,
        frame: AgentFrame,
        *,
        append: bool,
    ) -> None:
        remaining_ops = [
            node.node_id
            for node in self._execution_nodes(context.plan)
            if context.state.plan_node_status.get(node.node_id) != "completed"
        ]
        if not remaining_ops:
            return
        continuation = AgentFrame(
            frame_id=stable_hash(context.request_id, "root-continuation", len(context.state.queue))[:16],
            agent=self.shell.agent_pool.clone("root"),
            request_id=context.request_id,
            plan_id=context.plan.plan_id,
            trace_context=context.trace_context,
            objective=context.plan.objective,
            operation_ids=remaining_ops,
            depth=frame.depth,
            role="root",
            tool_scope=list(context.state.visible_tool_names),
            model_class=frame.model_class,
            branch_group_id=frame.branch_group_id,
            metadata={"continued_from_frame_id": frame.frame_id},
        )
        if append:
            context.state.queue.append(continuation)
        else:
            context.state.queue.insert(0, continuation)

    def _queue_root_continuation(self, context: PolicyContext, frame: AgentFrame) -> None:
        self._schedule_root_continuation(context, frame, append=False)

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
        context.state.branch_publications.append(model_dump(publication))
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
            model_validate(BranchState, payload)
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
            model_validate(BranchPublication, payload)
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
            snapshot = model_validate(BranchResumeSnapshot, payload)
            if snapshot.branch_plan.parent_frame_id != frame.frame_id:
                continue
            snapshots.append(snapshot)
        snapshots.sort(key=lambda item: (item.branch_plan.merge_priority, item.branch_plan.branch_id))
        return snapshots

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
            payload = model_dump(receipt)
            by_id[payload["side_effect_id"]] = payload
        context.state.side_effect_receipts = list(by_id.values())

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
            return branch_plan, clone_provider(
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
            return branch_plan, clone_provider(
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
            model_validate(BranchPublication, payload)
            for payload in branch_context.state.branch_publications
        ]

    @staticmethod
    def _branch_receipts_snapshot(branch_context: PolicyContext) -> list[SideEffectReceipt]:
        return [
            model_validate(SideEffectReceipt, payload)
            for payload in branch_context.state.side_effect_receipts
        ]

    def _branch_resume_snapshot(
        self,
        branch_plan: BranchPlan,
        branch_context: PolicyContext,
    ) -> BranchResumeSnapshot:
        return BranchResumeSnapshot(
            branch_plan=model_copy(branch_plan, deep=True),
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
        parent_context.state.branch_resume_snapshots[branch_plan.branch_id] = model_dump(
            self._branch_resume_snapshot(branch_plan, branch_context)
        )

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
            model_dump(publication)
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
        branch_context.state.side_effect_receipts = [model_dump(receipt) for receipt in receipts]
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
                "reserved_budget": model_dump(branch_plan.reserved_budget),
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
            publication = model_validate(BranchPublication, payload)
            if publication.publication_id in accepted_ids:
                publication = publication.copy(update={"accepted": True}, deep=True)
            updated_publications.append(model_dump(publication))
        context.state.branch_publications = updated_publications
        for branch_id, payload in list(context.state.branch_states.items()):
            branch_state = model_validate(BranchState, payload)
            branch_state.publications = [
                publication.copy(update={"accepted": True}, deep=True)
                if publication.publication_id in accepted_ids
                else publication
                for publication in branch_state.publications
            ]
            context.state.branch_states[branch_id] = model_dump(branch_state)

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
            context.state.branch_states[branch_result.branch_plan.branch_id] = model_dump(branch_result.branch_state)
            self._merge_provider_usage_into(provider_usage_ledger, branch_result.provider_usage)
            existing_publication_ids = {
                str(payload.get("publication_id", ""))
                for payload in context.state.branch_publications
            }
            for publication in branch_result.branch_state.publications:
                payload = model_dump(publication)
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
            branch_provider = clone_provider(self.provider, provider_profile=self.runtime_profile.runtime_provider)
        branch_provider_usage_before = self._provider_usage_snapshot(branch_provider)
        branch_runtime = TaskRuntime(
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

    def _start_agent_run(self, graph: ShortTermGraph, frame: AgentFrame, step: int, checkpoint: Checkpoint | None) -> str:
        run_node_id = graph.add_node(
            "AgentRun",
            frame.agent.agent_id,
            {
                "step": step,
                "objective": frame.objective,
                "role": frame.role,
                "depth": frame.depth,
                "worker_id": frame.worker_id,
                "tool_scope": list(frame.tool_scope),
                "model_class": frame.model_class,
            },
        )
        if checkpoint is not None:
            summary_id = graph.add_node("Summary", checkpoint.summary.objective, model_dump(checkpoint.summary), source="checkpoint")
            graph.add_edge(run_node_id, summary_id, "CONTINUES_FROM")
        return run_node_id

    def _execute_isolated_frame(
        self,
        context: PolicyContext,
        frame: AgentFrame,
        operations: Sequence[Any],
        isolate_runtime_state: bool = False,
    ) -> tuple[Any, int, Checkpoint]:
        parent_short_term = self.shell.short_term
        parent_state = context.state
        isolated_short_term = ShortTermGraph()
        isolated_state: RuntimeState | None = None
        tool_registry_snapshot: dict[str, Any] | None = None
        category_snapshot: dict[str, str] | None = None
        open_handle_snapshot: dict[str, Any] | None = None
        long_term_snapshot: dict[str, Any] | None = None
        predictor_observation_snapshot: dict[str, Any] | None = None
        predictor_model_snapshot: dict[str, Any] | None = None
        predictor_ranking_snapshot: dict[str, Any] | None = None
        if isolate_runtime_state:
            isolated_state = self._make_isolated_state(parent_state)
            context.state = isolated_state
            tool_registry_snapshot = copy.deepcopy(self.shell.tool_registry.tools)
            category_snapshot = dict(self.shell.tool_registry._category_summaries)
            open_handle_snapshot = copy.deepcopy(self.shell.open_handles.handles)
            long_term_snapshot = copy.deepcopy(self.shell.long_term.nodes)
            predictor_observation_snapshot = copy.deepcopy(self.shell.predictors._observations)
            predictor_model_snapshot = copy.deepcopy(self.shell.predictors._models)
            predictor_ranking_snapshot = copy.deepcopy(self.shell.predictors._ranking_weights)
        self.shell.short_term = isolated_short_term
        try:
            frame.metadata["run_node_id"] = self._start_agent_run(isolated_short_term, frame, 0, frame.checkpoint)
            output, local_faults = self._execute_operations(context, frame, operations)
            checkpoint = self.runtime.topology.make_checkpoint(
                context,
                frame,
                dict(context.state.artifacts),
                list(context.state.unresolved_goals),
                list(context.state.open_handle_ids),
            )
        finally:
            self.shell.short_term = parent_short_term
            if isolate_runtime_state:
                local_state = context.state
                context.state = parent_state
                if isolated_state is not None:
                    parent_state.created_tools += isolated_state.created_tools
                    parent_state.promoted_nodes += isolated_state.promoted_nodes
                    parent_state.checks_used += isolated_state.checks_used
                if tool_registry_snapshot is not None:
                    self.shell.tool_registry._tools = tool_registry_snapshot
                if category_snapshot is not None:
                    self.shell.tool_registry._category_summaries = category_snapshot
                if open_handle_snapshot is not None:
                    self.shell.open_handles.handles = open_handle_snapshot
                if long_term_snapshot is not None:
                    self.shell.long_term.nodes = long_term_snapshot
                if predictor_observation_snapshot is not None:
                    self.shell.predictors._observations = predictor_observation_snapshot
                if predictor_model_snapshot is not None:
                    self.shell.predictors._models = predictor_model_snapshot
                if predictor_ranking_snapshot is not None:
                    self.shell.predictors._ranking_weights = predictor_ranking_snapshot
        self._publish_checkpoint_summary(frame, checkpoint)
        return output, local_faults, checkpoint

    def _make_isolated_state(self, parent_state: RuntimeState) -> RuntimeState:
        return RuntimeState(
            visible_tool_names=list(parent_state.visible_tool_names),
            confidence=parent_state.confidence,
            mode=parent_state.mode,
            interface_usage=dict(parent_state.interface_usage),
            subgoal_negative_steps=dict(parent_state.subgoal_negative_steps),
            subgoal_last_model=dict(parent_state.subgoal_last_model),
            last_unresolved_goal=parent_state.last_unresolved_goal,
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

    def _publish_checkpoint_summary(self, frame: AgentFrame, checkpoint: Checkpoint) -> None:
        summary_id = self.shell.short_term.add_node(
            "Summary",
            checkpoint.summary.objective,
            model_dump(checkpoint.summary),
            agent_id=frame.agent.agent_id,
            role=frame.role,
        )
        parent_run_node_id = frame.metadata.get("parent_run_node_id")
        if isinstance(parent_run_node_id, str) and parent_run_node_id in self.shell.short_term.nodes:
            self.shell.short_term.add_edge(parent_run_node_id, summary_id, "CALLS_AGENT")
        for artifact_ref in checkpoint.artifact_refs:
            artifact_id = self.shell.short_term.add_node("Artifact", artifact_ref, {"artifact_ref": artifact_ref})
            self.shell.short_term.add_edge(summary_id, artifact_id, "PRODUCES")
        for handle_id in checkpoint.open_handles:
            if handle_id in self.shell.open_handles.handles:
                handle = self.shell.open_handles.get(handle_id)
                handle_node_id = self.shell.short_term.add_node("OpenHandle", handle.tool_name, model_dump(handle))
                self.shell.short_term.add_edge(summary_id, handle_node_id, "WAITS_ON")

    def _record_artifact_node(
        self,
        graph: ShortTermGraph,
        label: str,
        artifact: Any,
        producer_node_id: str | None,
    ) -> str:
        artifact_id = graph.add_node("Artifact", label, artifact)
        if producer_node_id and producer_node_id in graph.nodes:
            graph.add_edge(producer_node_id, artifact_id, "PRODUCES")
        return artifact_id

    def _store_output_artifacts(self, state: RuntimeState, operations: Sequence[Any], output: Any) -> None:
        if len(operations) == 1:
            state.artifacts[operations[0].output_key] = output if not isinstance(output, dict) else output.get(operations[0].output_key, output)
            return
        if isinstance(output, dict):
            for key, value in output.items():
                state.artifacts[key] = value

    def _promote_memory_candidate(self, context: PolicyContext, candidate: MemoryNode) -> None:
        score = self.runtime.memory.score_memory_unit(context, candidate, self.shell.long_term.all_nodes())
        if not self.runtime.memory.should_promote(context, candidate, score):
            return
        action, target_id = self.runtime.memory.dedup_candidates(context, candidate, self.shell.long_term.all_nodes())
        self.runtime.memory.upsert_memory(context, candidate, action, target_id)
        context.state.promoted_nodes += 1
        context.record("memory_promoted", node_id=candidate.node_id, node_type=candidate.type, action=action)

    def _ingest_context(self, context: PolicyContext) -> None:
        task = context.task
        for item in task.context_items:
            raw_id = self.shell.short_term.add_node("RawBlob", "context", item)
            context.record("context_ingested", raw_id=raw_id, item=item)
            candidate = None
            if "symbol" in item:
                candidate = MemoryNode(
                    node_id=stable_hash(task.task_id, item["symbol"], item.get("value"))[:16],
                    type="Symbol",
                    label=item["symbol"],
                    content=str(item.get("value")),
                    embedding=[],
                    symbol_set=[item["symbol"]],
                    file_paths=[],
                    source_task_id=task.task_id,
                    verifier_support=1.0,
                    timestamps={"created": now_ts()},
                    provenance={"source": "task_context"},
                    tombstoned=False,
                )
            elif "file_path" in item:
                candidate = MemoryNode(
                    node_id=stable_hash(task.task_id, item["file_path"], item.get("owner"))[:16],
                    type="File",
                    label=item["file_path"],
                    content=str(item.get("owner")),
                    embedding=[],
                    symbol_set=[],
                    file_paths=[item["file_path"]],
                    source_task_id=task.task_id,
                    verifier_support=1.0,
                    timestamps={"created": now_ts()},
                    provenance={"source": "task_context"},
                    tombstoned=False,
                )
            elif "rows" in item:
                candidate = MemoryNode(
                    node_id=stable_hash(task.task_id, stable_hash(item))[:16],
                    type="TaskNote",
                    label="rows",
                    content=json.dumps(item["rows"], sort_keys=True),
                    embedding=[],
                    symbol_set=[],
                    file_paths=[],
                    source_task_id=task.task_id,
                    verifier_support=0.5,
                    timestamps={"created": now_ts()},
                    provenance={"source": "task_context"},
                    tombstoned=False,
                )
            if candidate is not None:
                self._promote_memory_candidate(context, candidate)

    def _compact_if_needed(self, context: PolicyContext) -> None:
        short_term = self.shell.short_term
        total_text = " ".join(str(node["content"]) for node in short_term.nodes.values())
        used_tokens = count_tokens_rough(total_text)
        fraction = used_tokens / max(1.0, float(context.budget.context_window_tokens))
        if fraction <= context.profile.memory.b_hi:
            return
        span_ids = [node_id for node_id, node in short_term.nodes.items() if node["type"] in {"Event", "RawBlob"}]
        if not span_ids:
            return
        selected = self.runtime.memory.select_spans_for_compaction(context, span_ids, fraction)
        for group in selected:
            summary = self.runtime.memory.summarize_span(context, [short_term.nodes[node_id] for node_id in group])
            short_term.summary_replace(group, summary)
            context.record("compaction", node_ids=group, summary=model_dump(summary))

    def _resolve_agent(self, context: PolicyContext, child: ChildSpec) -> AgentTemplate:
        best_agent = None
        best_score = -1e9
        for agent in self.shell.agent_pool.list():
            score = self.runtime.topology.score_agent(context, agent, child)
            if score > best_score:
                best_score = score
                best_agent = agent
        if best_score < context.profile.topology.theta_create:
            ephemeral = AgentTemplate(
                agent_id=child.child_id,
                description=child.instruction,
                capability_set=child.required_capabilities,
                symbol_set=[],
                default_tool_scope=child.tool_scope,
                success_stats={},
                staleness_clock=0,
                model_policy_tag=child.model_class,
            )
            setattr(ephemeral, "_canonical", False)
            setattr(ephemeral, "_clone", True)
            context.record("agent_created", child_id=child.child_id, score=best_score)
            return ephemeral
        assert best_agent is not None
        clone = self.shell.agent_pool.clone(best_agent.agent_id)
        context.record("agent_reused", child_id=child.child_id, agent_id=best_agent.agent_id, score=best_score)
        return clone

    def _execute_operations(self, context: PolicyContext, frame: AgentFrame, operations: Sequence[Any]) -> tuple[Any, int]:
        results: dict[str, Any] = {}
        faults = 0
        run_node_id = frame.metadata.get("run_node_id")
        for operation in operations:
            context.raise_if_cancelled()
            existing_status = context.state.plan_node_status.get(operation.node_id)
            if existing_status == "completed" and operation.output_key in context.state.artifacts:
                results[operation.output_key] = context.state.artifacts[operation.output_key]
                context.record("node_reused_from_checkpoint", node_id=operation.node_id, output_key=operation.output_key)
                continue
            if existing_status == "recovery_blocked":
                blocked_output = {
                    "error": "recovery_blocked",
                    "node_id": operation.node_id,
                    "action_kind": self._node_operation_kind(operation),
                }
                results[operation.output_key] = blocked_output
                context.state.artifacts[operation.output_key] = blocked_output
                context.record("node_recovery_blocked", node_id=operation.node_id, output_key=operation.output_key)
                continue
            node_kind = self._node_operation_kind(operation)
            descriptor = get_plan_node_descriptor(str(operation.node_kind))
            context.state.plan_node_status[operation.node_id] = "running"
            event_id = self.shell.short_term.add_node("Event", operation.node_id, {"kind": node_kind, "description": operation.instruction})
            if isinstance(run_node_id, str) and run_node_id in self.shell.short_term.nodes:
                self.shell.short_term.add_edge(run_node_id, event_id, "EMITS")
            resolved_args = self._resolve_plan_node_args(context, operation)
            model_class = self.runtime.control.assign_model(context, operation, frame)
            node_trace_context = context.derive_trace_context(
                agent_id=frame.agent.agent_id,
                frame_role=frame.role,
                worker_id=frame.worker_id,
                op_id=operation.node_id,
                run_node_id=run_node_id if isinstance(run_node_id, str) else None,
            )
            context.record(
                "node_started",
                node_id=operation.node_id,
                frame_id=frame.frame_id,
                branch_id=frame.worker_id,
                output_key=operation.output_key,
                node_kind=operation.node_kind,
            )
            context.record("model_assigned", op_id=operation.node_id, model_class=model_class)
            try:
                if descriptor.executor_name == "_execute_memory_lookup_node":
                    output = self._execute_memory_lookup(context, operation, run_node_id)
                elif descriptor.executor_name in {"_execute_builtin_node", "_execute_tool_call_node", "_execute_tool_synthesis_node"}:
                    output, used_tool, created_tool, local_faults = self._execute_tool_operation(
                        context,
                        frame,
                        operation,
                        resolved_args,
                        run_node_id if isinstance(run_node_id, str) else None,
                    )
                    faults += local_faults
                    context.record("tool_operation", op_id=operation.node_id, tool=used_tool, created=created_tool, output=output)
                elif descriptor.executor_name == "_execute_direct_response_node":
                    output = self._execute_direct_response(context, operation, resolved_args, model_class, node_trace_context)
                elif descriptor.executor_name == "_execute_repo_patch_node":
                    output = self._execute_repo_patch_node(
                        context,
                        operation,
                        resolved_args,
                        model_class,
                        node_trace_context,
                    )
                elif descriptor.executor_name == "_execute_service_action_node":
                    output = self._execute_service_action_node(
                        context,
                        operation,
                        resolved_args,
                        node_trace_context,
                    )
                elif descriptor.executor_name == "_execute_merge_node":
                    output = self._execute_merge_node(context, operation)
                elif descriptor.executor_name == "_execute_verify_node":
                    output = self._execute_verify_node(context, operation, run_node_id if isinstance(run_node_id, str) else None)
                else:
                    raise HardInvalidation(f"unsupported plan node kind {operation.node_kind!r}")
            except Exception as exc:
                context.state.plan_node_status[operation.node_id] = "failed"
                context.record(
                    "node_failed",
                    node_id=operation.node_id,
                    frame_id=frame.frame_id,
                    branch_id=frame.worker_id,
                    output_key=operation.output_key,
                    node_kind=operation.node_kind,
                    error=str(exc),
                )
                raise
            results[operation.output_key] = output
            context.state.artifacts[operation.output_key] = output
            self._record_artifact_node(self.shell.short_term, operation.output_key, output, run_node_id if isinstance(run_node_id, str) else None)
            context.state.plan_node_status[operation.node_id] = "completed"
            context.record("node_completed", node_id=operation.node_id, output_key=operation.output_key)
            if frame.worker_id:
                context.publish_checkpoint_boundary("after_branch_node_completion")
            context.state.unresolved_goals = [key for key in context.plan.terminal_output_keys if key not in context.state.artifacts]
            context.raise_if_cancelled()
        if len(results) == 1:
            return next(iter(results.values())), faults
        return results, faults

    def _resolve_plan_node_args(self, context: PolicyContext, node: PlanNode) -> dict[str, Any]:
        resolved: dict[str, Any] = {}
        for binding in node.input_bindings:
            if binding.source_kind == "plan_constant":
                if binding.source_ref in context.plan.plan_constants:
                    resolved[binding.target_arg] = context.plan.plan_constants[binding.source_ref]
                elif binding.required:
                    raise HardInvalidation(
                        f"plan node {node.node_id} requires plan constant {binding.source_ref!r}"
                    )
            elif binding.source_kind == "upstream_output":
                dep_node = self._plan_node_by_id(context.plan, binding.source_ref)
                if dep_node.output_key in context.state.artifacts:
                    resolved[binding.target_arg] = context.state.artifacts[dep_node.output_key]
                elif binding.required:
                    raise HardInvalidation(
                        f"plan node {node.node_id} requires upstream output from {binding.source_ref}"
                    )
            elif binding.source_kind == "request_file":
                matching_specs = [
                    file_ref
                    for file_ref in context.plan.file_ref_specs
                    if str(file_ref.runtime_path) == binding.source_ref
                ]
                if matching_specs:
                    file_ref = matching_specs[0]
                    if str(file_ref.path_root) == "runtime_workspace_relative":
                        workspace_root = self._runtime_workspace_root(context)
                        resolved[binding.target_arg] = str(
                            (workspace_root / str(file_ref.workspace_relative_path or "")).resolve()
                        )
                    else:
                        resolved[binding.target_arg] = str(file_ref.runtime_path)
                elif binding.source_ref in context.plan.file_refs:
                    resolved[binding.target_arg] = binding.source_ref
                elif binding.required:
                    raise HardInvalidation(
                        f"plan node {node.node_id} requires request file {binding.source_ref!r}"
                    )
            elif binding.source_kind == "request_context":
                matches = [item for item in context.plan.context_refs if str(item.get("key", item.get("symbol", ""))) == binding.source_ref]
                if matches:
                    resolved[binding.target_arg] = matches[0]
                elif binding.required:
                    raise HardInvalidation(
                        f"plan node {node.node_id} requires request context {binding.source_ref!r}"
                    )
        return resolved

    @staticmethod
    def _artifact_for_output_keys(output_keys: Sequence[str], artifacts: Mapping[str, Any]) -> Any:
        ordered_keys = [str(output_key) for output_key in output_keys]
        if len(ordered_keys) == 1:
            return artifacts.get(ordered_keys[0])
        return {output_key: artifacts.get(output_key) for output_key in ordered_keys}

    @classmethod
    def _terminal_artifact(cls, plan: ExecutionPlan, artifacts: Mapping[str, Any]) -> Any:
        return cls._artifact_for_output_keys(plan.terminal_output_keys, artifacts)

    def _resolved_verify_status(
        self,
        plan: ExecutionPlan,
        artifacts: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        for node in plan.nodes:
            if str(node.node_kind) != "verify":
                continue
            payload = artifacts.get(node.output_key)
            if isinstance(payload, Mapping):
                return dict(payload)
        return None

    def _has_pending_explicit_verify(self, plan: ExecutionPlan, state: RuntimeState) -> bool:
        return any(
            str(node.node_kind) == "verify"
            and state.plan_node_status.get(node.node_id) != "completed"
            for node in plan.nodes
        )

    def _has_pending_plan_nodes(self, plan: ExecutionPlan, state: RuntimeState) -> bool:
        return any(state.plan_node_status.get(node.node_id) != "completed" for node in plan.nodes)

    def _resolve_terminal_progress(
        self,
        context: PolicyContext,
        frame: AgentFrame,
        task: BenchmarkTask,
        plan: ExecutionPlan,
        artifact: Any,
        verifier_score: float,
        verified_terminal: bool,
    ) -> tuple[Any | None, float, bool]:
        explicit_verify = self._resolved_verify_status(plan, context.state.artifacts)
        if explicit_verify is not None:
            verifier_score = float(explicit_verify.get("verifier_score", verifier_score) or 0.0)
            verified_terminal = bool(explicit_verify.get("verified", verifier_score >= 1.0))
            return artifact, verifier_score, verified_terminal
        if self._has_pending_explicit_verify(plan, context.state):
            self._queue_root_continuation(context, frame)
            return None, verifier_score, verified_terminal
        if plan.execution_flags.requires_terminal_verification:
            verifier_score = self._maybe_verify(
                context,
                artifact,
                frame.metadata.get("run_node_id"),
                exact_verifier_exists=self._has_exact_verifier(task),
            )
            verified_terminal = verifier_score >= 1.0
        return artifact, verifier_score, verified_terminal

    def _validate_execution_plan(self, plan: ExecutionPlan) -> ExecutionPlan:
        return model_validate(ExecutionPlan, model_dump(plan))

    @staticmethod
    def _node_operation_kind(node: PlanNode) -> str:
        return str(node.metadata.get("operation_kind", node.node_kind))

    def _execute_merge_node(self, context: PolicyContext, operation: PlanNode) -> Any:
        context.state.execution_state = "merging"
        payload = dict(context.state.worker_plans.get(operation.node_id, {}))
        frontier_node_ids = list(payload.get("frontier_node_ids", operation.dependencies))
        frontier_nodes = [self._plan_node_by_id(context.plan, node_id) for node_id in frontier_node_ids]
        context.record(
            "merge_started",
            node_id=operation.node_id,
            frontier_node_ids=frontier_node_ids,
            merge_kind="plan_node",
        )
        worker_outputs = list(payload.get("worker_outputs", []))
        if worker_outputs:
            merged_artifact = self.runtime.topology.merge_ensemble(context, worker_outputs)
        else:
            merged_artifact = {node.output_key: context.state.artifacts.get(node.output_key) for node in frontier_nodes}
        self._apply_horizontal_frontier_outputs(context, frontier_nodes, merged_artifact)
        context.record("merge_completed", node_id=operation.node_id, merge_kind="plan_node", artifact=merged_artifact)
        context.state.worker_plans.pop(operation.node_id, None)
        context.state.execution_state = "running"
        return merged_artifact

    def _execute_verify_node(self, context: PolicyContext, operation: PlanNode, run_node_id: str | None) -> Any:
        terminal_output_keys = list(operation.metadata.get("terminal_output_keys", context.plan.terminal_output_keys))
        artifact = self._artifact_for_output_keys(terminal_output_keys, context.state.artifacts)
        verifier_score = self._maybe_verify(
            context,
            artifact,
            run_node_id,
            exact_verifier_exists=self._has_exact_verifier(context.task),
        )
        return {"verifier_score": verifier_score, "verified": verifier_score >= 1.0}

    def _execute_memory_lookup(self, context: PolicyContext, operation: Any, run_node_id: str | None) -> Any:
        required_symbol = str(operation.static_args.get("requires_exact_symbol", "")).strip()
        exact_symbols = [required_symbol] if required_symbol else context.task.symbolic_seeds
        candidates = self.shell.long_term.retrieve_candidates(context.task.prompt, exact_symbols, context.task.file_paths)
        ranked = self.runtime.memory.retrieve_long_term(context, context.task.prompt, exact_symbols, context.task.file_paths, candidates)
        if not ranked:
            raise HardInvalidation("memory retrieval returned no candidates for exact symbol/path query")
        node = ranked[0]
        evidence_id = self.shell.short_term.add_node(
            "VerifierEvidence",
            operation.output_key,
            {"retrieved": node.node_id, "label": node.label, "type": node.type},
        )
        if run_node_id and run_node_id in self.shell.short_term.nodes:
            self.shell.short_term.add_edge(run_node_id, evidence_id, "VALIDATED_BY")
        feeds_downstream = any(operation.output_key in candidate.dependencies for candidate in context.plan.nodes)
        return self._coerce(node.content) if feeds_downstream else node.content

    @staticmethod
    def _decode_direct_response_output(raw_text: str) -> Any:
        try:
            return json.loads(raw_text)
        except Exception:
            return raw_text

    @staticmethod
    def _jsonable_prompt_inputs(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(key): TaskRuntime._jsonable_prompt_inputs(item) for key, item in value.items()}
        if isinstance(value, list):
            return [TaskRuntime._jsonable_prompt_inputs(item) for item in value]
        return value

    @classmethod
    def _collect_file_snapshots(cls, value: Any) -> list[dict[str, Any]]:
        collected: dict[str, dict[str, Any]] = {}

        def visit(candidate: Any) -> None:
            if isinstance(candidate, Mapping):
                if "path" in candidate and "content" in candidate:
                    path = str(candidate.get("path") or "").strip()
                    if path:
                        collected[path] = {
                            "path": path,
                            "content": str(candidate.get("content", "")),
                            "exists": bool(candidate.get("exists", True)),
                        }
                    return
                for nested in candidate.values():
                    visit(nested)
                return
            if isinstance(candidate, list):
                for nested in candidate:
                    visit(nested)

        visit(value)
        return [collected[path] for path in sorted(collected)]

    def _direct_response_prompt(
        self,
        context: PolicyContext,
        resolved_args: Mapping[str, Any],
    ) -> str:
        prompt_lines = [context.task.prompt]
        if context.task.context_items:
            prompt_lines.append("Context items:")
            prompt_lines.append(json.dumps(context.task.context_items, sort_keys=True, default=str))
        prompt_inputs = {
            key: self._jsonable_prompt_inputs(value)
            for key, value in resolved_args.items()
            if key not in {"request_id", "output_schema"}
        }
        if prompt_inputs:
            prompt_lines.append("Resolved inputs:")
            prompt_lines.append(json.dumps(prompt_inputs, sort_keys=True, default=str))
        elif context.task.file_paths:
            prompt_lines.append("File paths:")
            prompt_lines.append(json.dumps(context.task.file_paths, sort_keys=True, default=str))
        output_schema = resolved_args.get("output_schema", {})
        if output_schema:
            prompt_lines.append("Output schema:")
            prompt_lines.append(json.dumps(output_schema, sort_keys=True, default=str))
        return "\n".join(prompt_lines)

    def _execute_direct_response(
        self,
        context: PolicyContext,
        operation: Any,
        resolved_args: Mapping[str, Any],
        model_class: str,
        trace_context: Any,
    ) -> Any:
        response = context.run_model_request(
            instructions="Return the strongest bounded answer you can for the request. Use JSON only when an output schema is provided.",
            prompt=self._direct_response_prompt(context, resolved_args),
            model_class=model_class,
            purpose="user_request",
            payload={
                "prompt": context.task.prompt,
                "output_schema": resolved_args.get("output_schema", {}),
            },
            trace_context=trace_context,
        )
        return self._decode_direct_response_output(response.text)

    @staticmethod
    def _filesystem_is_read_only(policy: str) -> bool:
        normalized = str(policy or "").strip().lower()
        return "read-only" in normalized or normalized in {"readonly", "read_only", "none"}

    @staticmethod
    def _service_action_allowed(policy: str) -> bool:
        normalized = str(policy or "").strip().lower()
        return normalized not in {"", "none", "restricted", "provider-only"}

    @staticmethod
    def _parse_json_provider_payload(text: str, *, operation_kind: str) -> dict[str, Any]:
        try:
            payload = json.loads(text)
        except Exception as exc:
            raise HardInvalidation(f"{operation_kind} provider response must be valid JSON") from exc
        if not isinstance(payload, Mapping):
            raise HardInvalidation(f"{operation_kind} provider response must be a JSON object")
        return dict(payload)

    @staticmethod
    def _path_identity(path: Path) -> str:
        resolved = path.resolve()
        rendered = str(resolved)
        return rendered.casefold() if os.name == "nt" else rendered

    @staticmethod
    def _path_within_root(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
        except ValueError:
            return False
        return True

    def _runtime_workspace_root(self, context: PolicyContext) -> Path:
        isolation_policy = getattr(self.runtime.deployment_contract, "runtime_isolation_policy", None)
        declared_root = str(getattr(isolation_policy, "workspace_root", "") or ".").strip() or "."
        workspace_root = Path(declared_root)
        if not workspace_root.is_absolute():
            workspace_root = (context.shell.workspace / workspace_root).resolve()
        else:
            workspace_root = workspace_root.resolve()
        return workspace_root

    def _resolve_bounded_path(
        self,
        raw_path: str,
        *,
        workspace_root: Path,
        operation_kind: str,
    ) -> Path:
        cleaned = str(raw_path or "").strip()
        if not cleaned:
            raise HardInvalidation(f"{operation_kind} path may not be empty")
        candidate = Path(cleaned).expanduser()
        if candidate.is_absolute():
            return candidate.resolve()
        resolved = (workspace_root / candidate).resolve()
        if not self._path_within_root(resolved, workspace_root):
            raise HardInvalidation(
                f"{operation_kind} path {cleaned!r} escapes the runtime workspace root {workspace_root}"
            )
        return resolved

    def _normalized_repo_patch_targets(
        self,
        context: PolicyContext,
        *,
        target_file_paths: Sequence[str],
        file_snapshots: Sequence[Mapping[str, Any]],
    ) -> tuple[Path, dict[str, Path], dict[str, Mapping[str, Any]]]:
        workspace_root = self._runtime_workspace_root(context)
        snapshot_by_identity: dict[str, Mapping[str, Any]] = {}
        for snapshot in file_snapshots:
            snapshot_path = str(snapshot.get("path", "") or "").strip()
            if not snapshot_path:
                continue
            resolved_snapshot_path = self._resolve_bounded_path(
                snapshot_path,
                workspace_root=workspace_root,
                operation_kind="repo_patch snapshot",
            )
            snapshot_by_identity[self._path_identity(resolved_snapshot_path)] = snapshot
        resolved_targets: dict[str, Path] = {}
        for raw_target_path in target_file_paths:
            resolved_target_path = self._resolve_bounded_path(
                raw_target_path,
                workspace_root=workspace_root,
                operation_kind="repo_patch target",
            )
            target_identity = self._path_identity(resolved_target_path)
            if (
                not self._path_within_root(resolved_target_path, workspace_root)
                and target_identity not in snapshot_by_identity
            ):
                raise HardInvalidation(
                    "repo_patch targets must stay inside the runtime workspace or match explicitly hydrated request files"
                )
            resolved_targets[target_identity] = resolved_target_path
        if not resolved_targets:
            raise HardInvalidation("repo_patch execution requires at least one bounded target path")
        return workspace_root, resolved_targets, snapshot_by_identity

    @classmethod
    def _normalize_repo_patch_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        target_file_paths: Sequence[str],
    ) -> dict[str, Any]:
        allowed_paths = {str(path).strip() for path in target_file_paths if str(path).strip()}
        files_payload = payload.get("files", [])
        if not isinstance(files_payload, list):
            raise HardInvalidation("repo_patch response must include a files array")
        normalized_files: list[dict[str, Any]] = []
        for raw_file in files_payload:
            if not isinstance(raw_file, Mapping):
                raise HardInvalidation("repo_patch files entries must be JSON objects")
            path = str(raw_file.get("path") or "").strip()
            if not path:
                raise HardInvalidation("repo_patch files entries must include path")
            if allowed_paths and path not in allowed_paths:
                raise HardInvalidation(f"repo_patch attempted to modify undeclared path {path!r}")
            if "updated_content" not in raw_file:
                raise HardInvalidation(f"repo_patch response for {path!r} must include updated_content")
            normalized_files.append(
                {
                    "path": path,
                    "updated_content": str(raw_file.get("updated_content", "")),
                }
            )
        return {
            "summary": str(payload.get("summary", "") or "").strip(),
            "files": normalized_files,
        }

    @staticmethod
    def _unified_diff(path: str, before: str, after: str) -> str:
        return "\n".join(
            difflib.unified_diff(
                before.splitlines(),
                after.splitlines(),
                fromfile=path,
                tofile=path,
                lineterm="",
            )
        )

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            directory_fd = os.open(str(path), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        except OSError:
            pass
        finally:
            os.close(directory_fd)

    @classmethod
    def _write_text_atomic(cls, path: Path, text: str) -> None:
        ensure_directory(path.parent)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(path.parent)) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        temp_path.replace(path)
        cls._fsync_directory(path.parent)

    @staticmethod
    def _filesystem_write_matches_entry(entry: Mapping[str, Any], *, expected_state: str) -> bool:
        raw_path = str(entry.get("path", "") or "").strip()
        if not raw_path:
            return False
        path = Path(raw_path).expanduser()
        expected_exists = bool(entry.get(f"{expected_state}_exists"))
        if path.exists() != expected_exists:
            return False
        if not expected_exists:
            return True
        try:
            current_content = path.read_text(encoding="utf-8")
        except Exception:
            return False
        expected_digest = str(entry.get(f"{expected_state}_digest", "") or "").strip()
        return bool(expected_digest) and stable_hash(current_content) == expected_digest

    def _filesystem_write_reconciliation_state(self, receipt: SideEffectReceipt) -> str:
        writes = [
            dict(entry)
            for entry in receipt.result_ref.get("writes", [])
            if isinstance(entry, Mapping)
        ]
        if not writes:
            return "unknown"
        if all(self._filesystem_write_matches_entry(entry, expected_state="after") for entry in writes):
            return "completed"
        if all(self._filesystem_write_matches_entry(entry, expected_state="before") for entry in writes):
            return "prewrite_intact"
        return "ambiguous"

    def _execute_repo_patch_node(
        self,
        context: PolicyContext,
        operation: PlanNode,
        resolved_args: Mapping[str, Any],
        model_class: str,
        trace_context: OpenAITraceContext | None,
    ) -> Any:
        target_file_paths = [
            str(path).strip()
            for path in resolved_args.get("target_file_paths", context.task.file_paths)
            if str(path).strip()
        ]
        file_snapshots = self._collect_file_snapshots(resolved_args)
        if not target_file_paths or not file_snapshots:
            raise HardInvalidation("repo_patch execution requires explicit target files and readable file snapshots")
        workspace_root, resolved_targets, snapshot_by_identity = self._normalized_repo_patch_targets(
            context,
            target_file_paths=target_file_paths,
            file_snapshots=file_snapshots,
        )
        response = context.run_model_request(
            instructions=(
                "Return JSON only with keys summary and files. "
                "files must be an array of {path, updated_content}. "
                "Modify only the provided target files."
            ),
            prompt="\n".join(
                [
                    context.task.prompt,
                    "Target files:",
                    json.dumps(file_snapshots, sort_keys=True, default=str),
                ]
            ),
            model_class=model_class,
            purpose="repo_patch",
            payload={
                "prompt": context.task.prompt,
                "target_file_paths": target_file_paths,
            },
            trace_context=trace_context,
        )
        patch_payload = self._normalize_repo_patch_payload(
            self._parse_json_provider_payload(response.text, operation_kind="repo_patch"),
            target_file_paths=target_file_paths,
        )
        filesystem_write_idempotency_key = stable_hash(
            context.request_id,
            operation.node_id,
            target_file_paths,
            patch_payload,
        )
        unresolved_launch: SideEffectReceipt | None = None
        terminal_receipt: SideEffectReceipt | None = None
        for receipt_payload in context.state.side_effect_receipts:
            receipt = model_validate(SideEffectReceipt, receipt_payload)
            if receipt.action_kind != "filesystem_write" or receipt.idempotency_key != filesystem_write_idempotency_key:
                continue
            if is_terminal_receipt(receipt):
                terminal_receipt = receipt
                continue
            if receipt.status == "launched":
                unresolved_launch = receipt
        if terminal_receipt is not None:
            result_ref = dict(terminal_receipt.result_ref or {})
            if terminal_receipt.status in {"completed", "reconciled"} and "output" in result_ref:
                context.record(
                    "side_effect_reconciled",
                    side_effect_id=terminal_receipt.side_effect_id,
                    action_kind=terminal_receipt.action_kind,
                    reconciliation_status=terminal_receipt.status,
                )
                return result_ref.get("output")
            raise HardInvalidation(
                f"filesystem_write {filesystem_write_idempotency_key[:12]} already has terminal receipt status {terminal_receipt.status!r}"
            )
        applied = not self._filesystem_is_read_only(self.runtime.deployment_contract.filesystem_policy)
        writes: list[dict[str, Any]] = []
        updates: list[dict[str, Any]] = []
        for file_update in patch_payload["files"]:
            path = str(file_update["path"]).strip()
            resolved_path = self._resolve_bounded_path(
                path,
                workspace_root=workspace_root,
                operation_kind="repo_patch write",
            )
            path_identity = self._path_identity(resolved_path)
            if path_identity not in resolved_targets:
                raise HardInvalidation(
                    f"repo_patch attempted to write undeclared or out-of-bounds path {path!r}"
                )
            updated_content = str(file_update["updated_content"])
            existing_snapshot = snapshot_by_identity.get(path_identity)
            before_content = str(existing_snapshot.get("content", "")) if existing_snapshot is not None else ""
            before_exists = resolved_path.exists()
            writes.append(
                {
                    "path": str(resolved_targets[path_identity]),
                    "before_exists": before_exists,
                    "before_digest": stable_hash(before_content) if before_exists else "",
                    "after_exists": True,
                    "after_digest": stable_hash(updated_content),
                    "after_content": updated_content,
                }
            )
            updates.append(
                {
                    "path": str(resolved_targets[path_identity]),
                    "applied": applied,
                    "diff": self._unified_diff(str(resolved_targets[path_identity]), before_content, updated_content),
                }
            )
        output = {
            "summary": patch_payload["summary"],
            "updated_files": updates,
            "applied": applied,
        }
        if unresolved_launch is not None:
            reconciliation_state = self._filesystem_write_reconciliation_state(unresolved_launch)
            if reconciliation_state == "completed":
                context.record(
                    "side_effect_reconciled",
                    side_effect_id=unresolved_launch.side_effect_id,
                    action_kind=unresolved_launch.action_kind,
                    reconciliation_status="filesystem_state_matches_intent",
                )
                return dict(unresolved_launch.result_ref or {}).get("output")
            if reconciliation_state != "prewrite_intact":
                raise HardInvalidation("filesystem_write was already launched and must be reconciled before reissue")
        elif applied:
            context.record_side_effect(
                SideEffectReceipt(
                    side_effect_id=f"filesystem-write.launch.{filesystem_write_idempotency_key[:12]}",
                    action_fingerprint=stable_hash("filesystem_write", writes),
                    idempotency_key=filesystem_write_idempotency_key,
                    action_kind="filesystem_write",
                    request_id=context.request_id,
                    plan_id=context.plan.plan_id,
                    frame_id=getattr(context.active_frame, "frame_id", ""),
                    node_id=operation.node_id,
                    branch_id=getattr(context.active_frame, "worker_id", None),
                    trace_context=trace_context,
                    request_digest=stable_hash(context.request_id, operation.node_id, "filesystem_write"),
                    backend=context.runtime_backend,
                    status="launched",
                    result_ref={"output": output, "writes": writes},
                    replay_policy="reconcile_before_reissue",
                    reconciliation_policy="strict",
                    created_at=now_ts(),
                )
            )
            context.publish_checkpoint_boundary("before_filesystem_write")
            context.raise_if_cancelled()
        if applied:
            write_target: str | None = None
            try:
                for write in writes:
                    write_target = str(write["path"])
                    self._write_text_atomic(Path(write_target), str(write["after_content"]))
            except Exception as exc:
                context.record_side_effect(
                    SideEffectReceipt(
                        side_effect_id=f"filesystem-write.completion.{filesystem_write_idempotency_key[:12]}",
                        action_fingerprint=stable_hash("filesystem_write", writes, "failed"),
                        idempotency_key=filesystem_write_idempotency_key,
                        action_kind="filesystem_write",
                        request_id=context.request_id,
                        plan_id=context.plan.plan_id,
                        frame_id=getattr(context.active_frame, "frame_id", ""),
                        node_id=operation.node_id,
                        branch_id=getattr(context.active_frame, "worker_id", None),
                        trace_context=trace_context,
                        request_digest=stable_hash(context.request_id, operation.node_id, "filesystem_write", "failed"),
                        backend=context.runtime_backend,
                        status="failed",
                        result_ref={"output": output, "writes": writes, "error": str(exc), "failed_path": write_target},
                        replay_policy="reuse_if_completed",
                        reconciliation_policy="strict",
                        created_at=now_ts(),
                    )
                )
                context.publish_checkpoint_boundary("after_filesystem_write")
                raise HardInvalidation(
                    f"repo_patch failed while writing bounded target {write_target!r}: {exc}"
                ) from exc
        context.record_side_effect(
            SideEffectReceipt(
                side_effect_id=f"filesystem-write.completion.{filesystem_write_idempotency_key[:12]}",
                action_fingerprint=stable_hash("filesystem_write", writes if applied else output, "completed"),
                idempotency_key=filesystem_write_idempotency_key,
                action_kind="filesystem_write",
                request_id=context.request_id,
                plan_id=context.plan.plan_id,
                frame_id=getattr(context.active_frame, "frame_id", ""),
                node_id=operation.node_id,
                branch_id=getattr(context.active_frame, "worker_id", None),
                trace_context=trace_context,
                request_digest=stable_hash(context.request_id, operation.node_id, "filesystem_write", "completed"),
                backend=context.runtime_backend,
                status="completed",
                result_ref={"output": output, "writes": writes},
                replay_policy="reuse_if_completed",
                reconciliation_policy="strict",
                created_at=now_ts(),
            )
        )
        context.publish_checkpoint_boundary("after_filesystem_write")
        return output

    @staticmethod
    def _decode_service_response_body(raw_body: bytes, content_type: str) -> Any:
        text = raw_body.decode("utf-8", errors="replace")
        if "json" in content_type.lower():
            try:
                return json.loads(text)
            except Exception:
                return text
        return text

    def _execute_service_action_node(
        self,
        context: PolicyContext,
        operation: PlanNode,
        resolved_args: Mapping[str, Any],
        trace_context: OpenAITraceContext | None,
    ) -> Any:
        network_policy = str(self.runtime.deployment_contract.network_policy or "")
        if not self._service_action_allowed(network_policy):
            raise HardInvalidation(
                f"service_action is not permitted under deployment network policy {network_policy!r}"
            )
        url = str(resolved_args.get("url", "") or "").strip()
        method = str(resolved_args.get("method", "GET") or "GET").strip().upper()
        if not url:
            raise HardInvalidation("service_action requires a url")
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise HardInvalidation(f"service_action has unsupported method {method!r}")
        try:
            transport_compatibility = service_action_transport_compatibility(
                url=url,
                service_transport=resolved_args.get(
                    "service_transport",
                    operation.metadata.get("service_transport"),
                ),
                category_hint=operation.metadata.get(
                    "tool_category_hint",
                    operation.metadata.get("service_category_hint"),
                ),
                allowed_tool_categories=operation.allowed_tool_categories,
            )
        except ValueError as exc:
            raise HardInvalidation(str(exc)) from exc
        service_transport = transport_compatibility.transport
        headers = dict(resolved_args.get("headers", {})) if isinstance(resolved_args.get("headers"), Mapping) else {}
        body = resolved_args.get("body")
        timeout_s = float(resolved_args.get("timeout_s", 10.0) or 10.0)
        service_fingerprint = stable_hash("service_action", service_transport, url, method, headers, body, timeout_s)
        service_idempotency_key = stable_hash(
            context.request_id,
            operation.node_id,
            service_transport,
            url,
            method,
            headers,
            body,
            timeout_s,
        )
        unresolved_launch = False
        terminal_receipt: SideEffectReceipt | None = None
        for receipt_payload in context.state.side_effect_receipts:
            receipt = model_validate(SideEffectReceipt, receipt_payload)
            if receipt.action_kind != "service_action" or receipt.idempotency_key != service_idempotency_key:
                continue
            if is_terminal_receipt(receipt):
                terminal_receipt = receipt
                continue
            if receipt.status == "launched":
                unresolved_launch = True
        if terminal_receipt is not None:
            result_ref = dict(terminal_receipt.result_ref or {})
            if terminal_receipt.status in {"completed", "reconciled"} and "output" in result_ref:
                context.record(
                    "side_effect_reconciled",
                    side_effect_id=terminal_receipt.side_effect_id,
                    action_kind=terminal_receipt.action_kind,
                    reconciliation_status=terminal_receipt.status,
                )
                return result_ref.get("output")
            raise HardInvalidation(
                f"service_action {service_idempotency_key[:12]} already has terminal receipt status {terminal_receipt.status!r}"
            )
        if unresolved_launch:
            raise HardInvalidation("service_action was already launched and must be reconciled before reissue")
        data: bytes | None = None
        if body is not None:
            if isinstance(body, (dict, list)):
                data = json.dumps(body, sort_keys=True).encode("utf-8")
                headers.setdefault("Content-Type", "application/json")
            else:
                data = str(body).encode("utf-8")
        request_payload = {
            "service_transport": service_transport,
            "url": url,
            "method": method,
            "headers": {str(key): str(value) for key, value in headers.items()},
            "body": body,
            "timeout_s": timeout_s,
        }
        context.record_side_effect(
            SideEffectReceipt(
                side_effect_id=f"service-action.launch.{service_idempotency_key[:12]}",
                action_fingerprint=service_fingerprint,
                idempotency_key=service_idempotency_key,
                action_kind="service_action",
                request_id=context.request_id,
                plan_id=context.plan.plan_id,
                frame_id=getattr(context.active_frame, "frame_id", ""),
                node_id=operation.node_id,
                branch_id=getattr(context.active_frame, "worker_id", None),
                trace_context=trace_context,
                request_digest=stable_hash(context.request_id, operation.node_id, request_payload),
                backend=context.runtime_backend,
                status="launched",
                result_ref={"request": request_payload},
                replay_policy="reconcile_before_reissue",
                reconciliation_policy="strict",
                created_at=now_ts(),
            )
        )
        context.publish_checkpoint_boundary("after_service_action_launch")
        context.raise_if_cancelled()
        request = urllib_request.Request(url=url, method=method, headers={str(k): str(v) for k, v in headers.items()}, data=data)
        try:
            with urllib_request.urlopen(request, timeout=timeout_s) as response:
                raw_body = response.read()
                response_headers = dict(response.headers.items())
                output = {
                    "service_transport": service_transport,
                    "url": url,
                    "method": method,
                    "status_code": int(getattr(response, "status", response.getcode())),
                    "headers": response_headers,
                    "body": self._decode_service_response_body(
                        raw_body,
                        str(response_headers.get("Content-Type", "")),
                    ),
                }
        except urllib_error.URLError as exc:
            context.record_side_effect(
                SideEffectReceipt(
                    side_effect_id=f"service-action.completion.{service_idempotency_key[:12]}",
                    action_fingerprint=service_fingerprint,
                    idempotency_key=service_idempotency_key,
                    action_kind="service_action",
                    request_id=context.request_id,
                    plan_id=context.plan.plan_id,
                    frame_id=getattr(context.active_frame, "frame_id", ""),
                    node_id=operation.node_id,
                    branch_id=getattr(context.active_frame, "worker_id", None),
                    trace_context=trace_context,
                    request_digest=stable_hash(context.request_id, operation.node_id, request_payload, str(exc)),
                    backend=context.runtime_backend,
                    status="failed",
                    result_ref={"request": request_payload, "error": str(exc)},
                    replay_policy="reuse_if_completed",
                    reconciliation_policy="strict",
                    created_at=now_ts(),
                )
            )
            context.publish_checkpoint_boundary("after_service_action_completion")
            raise HardInvalidation(f"service_action failed for {url}: {exc}") from exc
        context.record_side_effect(
            SideEffectReceipt(
                side_effect_id=f"service-action.completion.{service_idempotency_key[:12]}",
                action_fingerprint=service_fingerprint,
                idempotency_key=service_idempotency_key,
                action_kind="service_action",
                request_id=context.request_id,
                plan_id=context.plan.plan_id,
                frame_id=getattr(context.active_frame, "frame_id", ""),
                node_id=operation.node_id,
                branch_id=getattr(context.active_frame, "worker_id", None),
                trace_context=trace_context,
                request_digest=stable_hash(context.request_id, operation.node_id, request_payload, output),
                backend=context.runtime_backend,
                status="completed",
                result_ref={"request": request_payload, "output": output},
                replay_policy="reuse_if_completed",
                reconciliation_policy="strict",
                created_at=now_ts(),
            )
        )
        context.publish_checkpoint_boundary("after_service_action_completion")
        return output

    def _dedupe_tools(self, tools: Sequence[Any]) -> list[Any]:
        deduped: dict[str, Any] = {}
        for tool in tools:
            deduped[tool.spec.name] = tool
        return list(deduped.values())

    def _discover_candidate_tools(
        self,
        context: PolicyContext,
        frame: AgentFrame,
        operation: Any,
    ) -> list[Any]:
        allowed_categories = list(context.task.allowed_tool_categories)
        category_summaries = {
            category_key: summary
            for category_key, summary in self.shell.tool_registry.category_summaries.items()
            if _category_allowed(allowed_categories, category_key)
        }
        categories = self.runtime.tooling.rank_categories(
            context,
            operation,
            category_summaries,
        )
        inspected_categories = categories[: context.profile.tooling.k_c]
        candidate_tools: list[Any] = []
        for category in inspected_categories:
            candidate_tools.extend(self.shell.tool_registry.tools_in_category(category))
        candidate_tools = [
            tool
            for tool in candidate_tools
            if _category_allowed(allowed_categories, tool.category_key)
        ]
        if frame.tool_scope:
            allowed = set(frame.tool_scope)
            candidate_tools = [tool for tool in candidate_tools if tool.spec.name in allowed]
        return self._dedupe_tools(candidate_tools)

    def _execute_tool_operation(
        self,
        context: PolicyContext,
        frame: AgentFrame,
        operation: Any,
        args: Mapping[str, Any],
        run_node_id: str | None,
    ) -> tuple[Any, str, bool, int]:
        context.raise_if_cancelled()
        faults = 0
        candidate_tools = self._discover_candidate_tools(context, frame, operation)
        ranked_tool_names = self.runtime.tooling.rank_tools(context, operation, candidate_tools)
        candidate_tool_names = {tool.spec.name for tool in candidate_tools}
        created_tool = False
        hinted_tool_usable = (
            operation.tool_hint
            and operation.tool_hint in context.state.visible_tool_names
            and operation.tool_hint in candidate_tool_names
        )
        if hinted_tool_usable:
            hint_signature = self.shell.tool_registry.get(operation.tool_hint).spec.signature
            hinted_tool_usable = set(args) <= set(_signature_arg_names(hint_signature))
        if hinted_tool_usable:
            tool_name = operation.tool_hint
        elif ranked_tool_names:
            tool_name = ranked_tool_names[0]
        else:
            tool_name = None
        generated_allowed = _category_allowed(context.task.allowed_tool_categories, "generated/local")
        if operation.node_kind == "tool_synthesis" and generated_allowed and self.runtime.tooling.should_create_tool(context, operation, ranked_tool_names):
            synth_name = operation.tool_hint or f"synth:{operation.node_id}"
            try:
                spec, source, executor = self.runtime.tooling.propose_tool_spec(context, operation, dict(args))
                if self.runtime.tooling.validate_tool(context, spec, source):
                    self.shell.tool_registry.register_generated_tool(spec, source, executor=executor)
                    tool_name = spec.name
                    created_tool = True
                    context.state.created_tools += 1
                    context.state.visible_tool_names.append(tool_name)
            except HardInvalidation:
                raise
            except Exception as exc:
                faults += 1
                stderr = str(exc)
                context.record("tool_fault", tool=synth_name, stderr=stderr)
                self._record_tool_failure(context, operation, synth_name, stderr)
                created_tool = False
                if tool_name is None:
                    raise HardInvalidation("no tool available after category-first discovery") from exc
        if tool_name is None:
            raise HardInvalidation("no tool available after category-first discovery")
        side_effect_key = stable_hash(context.request_id, operation.node_id, tool_name, dict(args))
        unresolved_launch = False
        terminal_receipt: SideEffectReceipt | None = None
        for receipt_payload in context.state.side_effect_receipts:
            receipt = model_validate(SideEffectReceipt, receipt_payload)
            if receipt.idempotency_key != side_effect_key:
                continue
            if is_terminal_receipt(receipt):
                terminal_receipt = receipt
                continue
            if receipt.action_kind == "tool_launch" and receipt.status == "launched":
                unresolved_launch = True
        if terminal_receipt is not None:
            result_ref = dict(terminal_receipt.result_ref or {})
            if terminal_receipt.status in {"completed", "reconciled"} and "output" in result_ref:
                context.record(
                    "side_effect_reconciled",
                    side_effect_id=terminal_receipt.side_effect_id,
                    action_kind=terminal_receipt.action_kind,
                    reconciliation_status=terminal_receipt.status,
                )
                return result_ref.get("output"), tool_name, created_tool, faults
            raise HardInvalidation(
                f"tool execution {side_effect_key[:12]} already has terminal receipt status {terminal_receipt.status!r}"
            )
        if unresolved_launch:
            raise HardInvalidation("tool execution was already launched and must be reconciled before reissue")
        tool_trace_context = context.derive_trace_context(
            agent_id=frame.agent.agent_id,
            frame_role=frame.role,
            worker_id=frame.worker_id,
            op_id=operation.node_id,
        )
        dispatch_meta = self.runtime.tooling.dispatch_tool(context, tool_name, args)
        if dispatch_meta.get("async"):
            handle_fingerprint = side_effect_key
            context.raise_if_cancelled()
            handle = self.shell.tool_executor.launch_async(
                tool_name,
                args,
                self.shell.workspace / "handles",
                context.task.task_id,
            )
            self.shell.open_handles.add(handle)
            context.state.open_handle_ids.append(handle.handle_id)
            context.record_side_effect(
                SideEffectReceipt(
                    side_effect_id=f"tool-launch.{handle_fingerprint[:12]}",
                    action_fingerprint=handle_fingerprint,
                    idempotency_key=handle_fingerprint,
                    action_kind="tool_launch",
                    request_id=context.request_id,
                    plan_id=context.plan.plan_id,
                    frame_id=frame.frame_id,
                    node_id=operation.node_id,
                    branch_id=frame.worker_id,
                    trace_context=tool_trace_context,
                    request_digest=handle_fingerprint,
                    backend=context.runtime_backend,
                    status="launched",
                    result_ref={"tool_name": tool_name, "launch_mode": "async", "handle_id": handle.handle_id},
                    replay_policy="reconcile_before_reissue",
                    reconciliation_policy="strict",
                    created_at=now_ts(),
                )
            )
            context.publish_checkpoint_boundary("after_tool_launch")
            handle_node_id = self.shell.short_term.add_node("OpenHandle", handle.tool_name, model_dump(handle))
            if run_node_id and run_node_id in self.shell.short_term.nodes:
                self.shell.short_term.add_edge(run_node_id, handle_node_id, "WAITS_ON")
            context.raise_if_cancelled()
            if hasattr(self.shell.tool_executor, "await_handle"):
                finished = self.shell.tool_executor.await_handle(handle.handle_id, self.shell.open_handles)
                context.budget.consume_tool_latency(float(finished.get("latency_s", 0.0)))
                if finished.get("state") != "completed":
                    faults += 1
                    stderr = str(finished.get("stderr", "async execution failed"))
                    context.record("tool_fault", tool=tool_name, stderr=stderr)
                    self._record_tool_failure(context, operation, tool_name, stderr)
                    raise HardInvalidation(f"tool execution failed for {tool_name}: {stderr}")
                output = finished.get("output")
            elif hasattr(self.shell.tool_executor, "wait_async"):
                result = self.shell.tool_executor.wait_async(handle)
                context.budget.consume_tool_latency(result.latency_s)
                if not result.success:
                    faults += 1
                    context.record("tool_fault", tool=tool_name, stderr=result.stderr)
                    self._record_tool_failure(context, operation, tool_name, result.stderr)
                    raise HardInvalidation(f"tool execution failed for {tool_name}: {result.stderr}")
                output = result.output
                self.shell.open_handles.update_state(handle.handle_id, "completed")
            else:
                self.shell.open_handles.update_state(handle.handle_id, "completed")
                output = None
            completion_fingerprint = stable_hash(context.request_id, operation.node_id, tool_name, dict(args), handle.handle_id)
            context.record_side_effect(
                SideEffectReceipt(
                    side_effect_id=f"tool-completion.{completion_fingerprint[:12]}",
                    action_fingerprint=completion_fingerprint,
                    idempotency_key=handle_fingerprint,
                    action_kind="tool_completion",
                    request_id=context.request_id,
                    plan_id=context.plan.plan_id,
                    frame_id=frame.frame_id,
                    node_id=operation.node_id,
                    branch_id=frame.worker_id,
                    trace_context=tool_trace_context,
                    request_digest=completion_fingerprint,
                    backend=context.runtime_backend,
                    status="completed",
                    result_ref={
                        "tool_name": tool_name,
                        "launch_mode": "async",
                        "handle_id": handle.handle_id,
                        "output": output,
                    },
                    replay_policy="reuse_if_completed",
                    reconciliation_policy="strict",
                    created_at=now_ts(),
                )
            )
            context.publish_checkpoint_boundary("after_tool_completion")
        else:
            sync_fingerprint = side_effect_key
            context.record_side_effect(
                SideEffectReceipt(
                    side_effect_id=f"tool-launch.{sync_fingerprint[:12]}",
                    action_fingerprint=sync_fingerprint,
                    idempotency_key=sync_fingerprint,
                    action_kind="tool_launch",
                    request_id=context.request_id,
                    plan_id=context.plan.plan_id,
                    frame_id=frame.frame_id,
                    node_id=operation.node_id,
                    branch_id=frame.worker_id,
                    trace_context=tool_trace_context,
                    request_digest=sync_fingerprint,
                    backend=context.runtime_backend,
                    status="launched",
                    result_ref={"tool_name": tool_name, "launch_mode": "sync"},
                    replay_policy="reconcile_before_reissue",
                    reconciliation_policy="strict",
                    created_at=now_ts(),
                )
            )
            context.publish_checkpoint_boundary("after_tool_launch")
            context.raise_if_cancelled()
            try:
                result = self.shell.tool_executor.run_tool(tool_name, args, context.task.task_id)
            except Exception as exc:
                faults += 1
                stderr = str(exc)
                context.record("tool_fault", tool=tool_name, stderr=stderr)
                self._record_tool_failure(context, operation, tool_name, stderr)
                raise HardInvalidation(f"tool execution failed for {tool_name}: {stderr}") from exc
            context.budget.consume_tool_latency(result.latency_s)
            if not result.success:
                context.record_side_effect(
                    SideEffectReceipt(
                        side_effect_id=f"tool-completion.{sync_fingerprint[:12]}",
                        action_fingerprint=sync_fingerprint,
                        idempotency_key=sync_fingerprint,
                        action_kind="tool_completion",
                        request_id=context.request_id,
                        plan_id=context.plan.plan_id,
                        frame_id=frame.frame_id,
                        node_id=operation.node_id,
                        branch_id=frame.worker_id,
                        trace_context=tool_trace_context,
                        request_digest=sync_fingerprint,
                        backend=context.runtime_backend,
                        status="failed",
                        result_ref={"tool_name": tool_name, "launch_mode": "sync", "stderr": result.stderr},
                        replay_policy="reuse_if_completed",
                        reconciliation_policy="strict",
                        created_at=now_ts(),
                    )
                )
                context.publish_checkpoint_boundary("after_tool_completion")
                faults += 1
                context.record("tool_fault", tool=tool_name, stderr=result.stderr)
                self._record_tool_failure(context, operation, tool_name, result.stderr)
                raise HardInvalidation(f"tool execution failed for {tool_name}: {result.stderr}")
            output = result.output
            context.record_side_effect(
                SideEffectReceipt(
                    side_effect_id=f"tool-completion.{sync_fingerprint[:12]}",
                    action_fingerprint=sync_fingerprint,
                    idempotency_key=sync_fingerprint,
                    action_kind="tool_completion",
                    request_id=context.request_id,
                    plan_id=context.plan.plan_id,
                    frame_id=frame.frame_id,
                    node_id=operation.node_id,
                    branch_id=frame.worker_id,
                    trace_context=tool_trace_context,
                    request_digest=sync_fingerprint,
                    backend=context.runtime_backend,
                    status="completed",
                    result_ref={"tool_name": tool_name, "launch_mode": "sync", "output": output},
                    replay_policy="reuse_if_completed",
                    reconciliation_policy="strict",
                    created_at=now_ts(),
                )
            )
            context.publish_checkpoint_boundary("after_tool_completion")
        context.raise_if_cancelled()
        tool = self.shell.tool_registry.get(tool_name)
        if operation.node_kind == "tool_synthesis":
            self._record_procedure(context, operation, tool_name)
        if created_tool and self.runtime.tooling.promote_tool(context, tool):
            context.record("tool_promoted", tool=tool_name)
        return output, tool_name, created_tool, faults

    def _record_tool_failure(self, context: PolicyContext, operation: Any, tool_name: str, stderr: str) -> None:
        candidate = MemoryNode(
            node_id=stable_hash(context.task.task_id, operation.node_id, tool_name, stderr)[:16],
            type="ToolFailure",
            label=tool_name,
            content=stderr,
            embedding=[],
            symbol_set=[operation.node_id],
            file_paths=[],
            source_task_id=context.task.task_id,
            verifier_support=0.0,
            timestamps={"created": now_ts()},
            provenance={"source": "tool_fault", "operation": operation.node_id},
            tombstoned=False,
        )
        self._promote_memory_candidate(context, candidate)

    def _record_procedure(self, context: PolicyContext, operation: Any, tool_name: str) -> None:
        expression = getattr(operation, "expression", None) or operation.metadata.get("expression")
        if not expression:
            return
        candidate = MemoryNode(
            node_id=stable_hash(context.task.task_id, tool_name, expression)[:16],
            type="Procedure",
            label=tool_name,
            content=expression,
            embedding=[],
            symbol_set=[operation.node_id],
            file_paths=[],
            source_task_id=context.task.task_id,
            verifier_support=0.6,
            timestamps={"created": now_ts()},
            provenance={"source": "generated_expression"},
            tombstoned=False,
        )
        self._promote_memory_candidate(context, candidate)

    def _record_artifact_signature(self, context: PolicyContext, artifact: Any, verifier_score: float) -> None:
        if verifier_score <= 0.0:
            return
        candidate = MemoryNode(
            node_id=stable_hash(context.task.task_id, stable_hash(artifact))[:16],
            type="ArtifactSignature",
            label=context.task.task_id,
            content=json.dumps(artifact, sort_keys=True, default=str),
            embedding=[],
            symbol_set=list(context.task.symbolic_seeds),
            file_paths=list(context.task.file_paths),
            source_task_id=context.task.task_id,
            verifier_support=verifier_score,
            timestamps={"created": now_ts()},
            provenance={"source": "verifier", "verifier_type": context.task.verifier_type},
            tombstoned=False,
        )
        self._promote_memory_candidate(context, candidate)

    def _maybe_verify(
        self,
        context: PolicyContext,
        artifact: Any,
        run_node_id: str | None,
        *,
        exact_verifier_exists: bool | None = None,
    ) -> float:
        exact_verifier_exists = self._has_exact_verifier(context.task) if exact_verifier_exists is None else exact_verifier_exists
        checkers = self.runtime.control.request_checks(
            context,
            artifact,
            exact_verifier_exists=exact_verifier_exists,
            irreversible=True,
            external_visible=context.task.externally_visible,
        )
        available_checks = context.budget.remaining_checks()
        if available_checks <= 0:
            context.record("checks_skipped", reason="check_budget_exhausted")
            return 0.0
        if len(checkers) > available_checks:
            context.record("checks_trimmed", requested=checkers, allowed=available_checks)
        checkers = list(checkers[:available_checks])
        context.record("checks_requested", checks=checkers)
        verifier_score = 0.0
        total_latency = 0.0
        executed_checks = 0
        has_benchmark = "benchmark" in checkers
        for checker in checkers:
            start = time.perf_counter()
            evidence = run_checker(context.task, artifact, context.trace, checker)
            total_latency += time.perf_counter() - start
            executed_checks += 1
            evidence_id = self.shell.short_term.add_node("VerifierEvidence", checker, evidence, checker=checker)
            if run_node_id and run_node_id in self.shell.short_term.nodes:
                self.shell.short_term.add_edge(run_node_id, evidence_id, "VALIDATED_BY")
            context.record("check_result", checker=checker, passed=evidence.get("passed", False))
            if checker == "benchmark":
                verifier_score = float(evidence.get("score", 0.0))
                break
            if not evidence.get("passed", False) and not has_benchmark:
                break
        context.state.checks_used += executed_checks
        context.budget.consume_check(executed_checks, total_latency)
        self._record_artifact_signature(context, artifact, verifier_score)
        if getattr(context.active_frame, "worker_id", None):
            self._emit_branch_publication(
                context,
                publication_kind="verifier_evidence",
                logical_key=f"{context.active_frame.worker_id}.verifier.completed",
                payload={
                    "event": "branch_verifier_completed",
                    "checks": checkers,
                    "verifier_score": verifier_score,
                },
                verifier_support=verifier_score,
            )
            context.publish_checkpoint_boundary("after_branch_verifier_completion")
            context.raise_if_cancelled()
        return verifier_score

    def _has_exact_verifier(self, task: BenchmarkTask) -> bool:
        return str(task.verifier_type).strip().lower() not in {"", "none", "best_effort"}

    def _best_next_action_utility(self, context: PolicyContext, unresolved: Sequence[str], verified_terminal: bool) -> float:
        if not unresolved and verified_terminal:
            return -0.1
        remaining_budget = 1.0 - max(context.budget.normalized().values())
        if remaining_budget <= 0:
            return -1.0
        candidates = []
        for output_key in unresolved:
            operation = next((op for op in context.task.operations if op.output_key == output_key), None)
            if operation is None:
                continue
            solve = 0.55
            cost = 0.06
            latency = 0.05
            fault = 0.04
            if operation.kind == "memory_lookup":
                solve += 0.10
                cost += 0.04
                latency += 0.03
            elif operation.kind == "generated_expression":
                solve += 0.16
                cost += 0.18
                latency += 0.12
                fault += 0.08
            elif operation.kind == "repo_patch":
                solve += 0.08
                cost += 0.18
                latency += 0.14
                fault += 0.09
            elif operation.kind == "service_action":
                solve += 0.05
                cost += 0.06
                latency += 0.12
                fault += 0.10
            elif operation.kind == "builtin":
                solve += 0.12
                cost += 0.02
                latency += 0.02
            solve += 0.03 * min(3, len(operation.dependencies))
            candidates.append(solve - 0.25 * cost - 0.18 * latency - 0.15 * fault + 0.10 * remaining_budget)
        if verified_terminal:
            candidates.append(-0.05)
        return max(candidates or [-0.5])

    def _worker_support(self, task: BenchmarkTask, artifact: Any) -> float:
        return verify_task(task, artifact, [])

    def _all_outputs_present(self, plan: ExecutionPlan, artifacts: Mapping[str, Any]) -> bool:
        return all(output_key in artifacts for output_key in plan.terminal_output_keys)

    def _plan_node_by_id(self, plan: ExecutionPlan, node_id: str) -> PlanNode:
        for node in plan.nodes:
            if node.node_id == node_id:
                return node
        raise KeyError(node_id)

    def _checkpoint_key(self, frame: AgentFrame) -> str:
        return frame.agent.agent_id + ":" + ",".join(frame.operation_ids)

    def _operation_by_id(self, task: BenchmarkTask, op_id: str):
        for operation in task.operations:
            if operation.op_id == op_id:
                return operation
        raise KeyError(op_id)

    def _coerce(self, value: Any) -> Any:
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            try:
                return json.loads(value)
            except Exception:
                pass
            try:
                return int(value)
            except Exception:
                try:
                    return float(value)
                except Exception:
                    return value
        return value
