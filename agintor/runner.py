from __future__ import annotations

import copy
import os
import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping, Sequence

from .exceptions import HardInvalidation
from .memory_graph import ShortTermGraph
from .providers import ModelProvider, known_provider_environment_names, provider_environment_names, provider_environment_names_for_instance
from .runtime_profile import RuntimeProfile, load_runtime_profile
from .runtime_api import AgentFrame, PolicyContext, RuntimeBudget, RuntimeState
from .runtime_loader import LoadedRuntime
from .pydantic_compat import model_copy, model_dump
from .schemas import AgentTemplate, BenchmarkTask, Checkpoint, ChildSpec, MemoryNode, RunResult
from .shell import FixedShell
from .tool_runtime import _signature_arg_names
from .utils import count_tokens_rough, ensure_directory, now_ts, stable_hash
from .verifiers import run_checker, verify_task


class TaskRuntime:
    def __init__(
        self,
        runtime: LoadedRuntime,
        shell: FixedShell,
        provider: ModelProvider,
        budget_overrides: Mapping[str, Any] | None = None,
        runtime_profile: RuntimeProfile | None = None,
    ) -> None:
        self.runtime = runtime
        self.shell = shell
        self.provider = provider
        self.budget_overrides = dict(budget_overrides or {})
        self.runtime_profile = runtime_profile or load_runtime_profile(runtime.runtime_dir)

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

    def run_task(self, task: BenchmarkTask, seed: int) -> RunResult:
        with self._isolated_provider_environment():
            task = model_copy(task, deep=True)
            episode_scope = None
            if task.transfer_scored:
                episode_scope = f"{getattr(task, 'episode_id', None) or task.task_id}::seed::{seed}"
            self.shell.reset_for_task(
                task.task_id,
                transfer_scored=task.transfer_scored,
                episode_id=episode_scope,
            )
            budget = RuntimeBudget(**self._runtime_budget_overrides())
            state = RuntimeState(visible_tool_names=sorted(self.shell.tool_registry.tools))
            trace: list[dict[str, Any]] = []
            context = PolicyContext(
                runtime_dir=self.runtime.runtime_dir,
                shell=self.shell,
                task=task,
                provider=self.provider,
                profile=self.runtime_profile,
                seed=seed,
                state=state,
                budget=budget,
                trace=trace,
                objective=task.prompt,
            )
            root = self.shell.agent_pool.clone("root")
            state.queue.append(
                AgentFrame(
                    agent=root,
                    objective=task.prompt,
                    operation_ids=[op.op_id for op in task.operations],
                    depth=0,
                    role="root",
                    tool_scope=state.visible_tool_names,
                    model_class="medium",
                )
            )
            artifact: Any = None
            faults = 0
            verifier_score = 0.0
            prev_best = 0.0
            verified_terminal = False
            start = time.perf_counter()
            self._ingest_context(context)
            try:
                step = 0
                while state.queue and step < self.runtime_profile.execution.max_steps:
                    step += 1
                    self.shell.validate_invariants(transfer_scored=task.transfer_scored)
                    self._compact_if_needed(context)
                    frame = state.queue.pop(0)
                    self.shell.agent_pool.assert_clone(frame.agent)
                    if frame.depth == 0 or frame.role.startswith("merge"):
                        frame.metadata["run_node_id"] = self._start_agent_run(self.shell.short_term, frame, step, frame.checkpoint)
                    context.record("agent_start", step=step, agent_id=frame.agent.agent_id, role=frame.role, depth=frame.depth, op_ids=frame.operation_ids)
                    if frame.role == "merge_vertical":
                        artifact = {op.output_key: state.artifacts.get(op.output_key) for op in task.operations}
                        if self._all_outputs_present(task, state.artifacts):
                            verifier_score = self._maybe_verify(context, artifact, frame.metadata.get("run_node_id"))
                            verified_terminal = verifier_score >= 1.0
                        self._record_artifact_node(self.shell.short_term, "final", artifact, frame.metadata.get("run_node_id"))
                        context.record("merge_vertical", artifact=artifact)
                    elif frame.role == "merge_horizontal":
                        worker_outputs = frame.metadata.get("worker_outputs", [])
                        artifact = self.runtime.topology.merge_ensemble(context, worker_outputs)
                        verifier_score = self._maybe_verify(context, artifact, frame.metadata.get("run_node_id"))
                        verified_terminal = verifier_score >= 1.0
                        self._record_artifact_node(self.shell.short_term, "ensemble", artifact, frame.metadata.get("run_node_id"))
                        context.record("merge_horizontal", artifact=artifact)
                    elif frame.depth == 0:
                        artifact, local_faults, verifier_score, verified_terminal = self._run_root_frame(context, frame, task, verifier_score, verified_terminal)
                        faults += local_faults
                    else:
                        operations = [self._operation_by_id(task, op_id) for op_id in frame.operation_ids]
                        output, local_faults, checkpoint = self._execute_isolated_frame(context, frame, operations)
                        faults += local_faults
                        self._store_output_artifacts(state, operations, output)
                        state.checkpoints[self._checkpoint_key(frame)] = checkpoint
                        context.record("child_complete", role=frame.role, outputs=list(state.artifacts.keys()))
                    unresolved = [
                        op.output_key
                        for op in task.operations
                        if op.output_key not in state.artifacts and not (isinstance(artifact, dict) and op.output_key in artifact)
                    ]
                    state.unresolved_goals = unresolved
                    best_optimistic = self._best_next_action_utility(context, unresolved, verified_terminal)
                    self._update_subgoal_progress(context, unresolved, best_optimistic, prev_best, verified_terminal)
                    if self.runtime.control.stop_policy(context, best_optimistic, prev_best, len(unresolved), verified_terminal):
                        context.record("stop", unresolved=unresolved, verified=verified_terminal, best_optimistic=best_optimistic)
                        break
                    prev_best = best_optimistic
                    context.record("agent_end", step=step, unresolved=unresolved, verified=verified_terminal)
                if artifact is None and state.artifacts:
                    artifact = {op.output_key: state.artifacts.get(op.output_key) for op in task.operations}
                    verifier_score = self._maybe_verify(context, artifact, None)
                    verified_terminal = verifier_score >= 1.0
                if not verified_terminal and task.verification_required and not task.allow_best_effort:
                    artifact = {"error": "controlled_failure"}
                elif artifact is None and not task.allow_best_effort:
                    artifact = {"error": "controlled_failure"}
            except HardInvalidation as exc:
                return self._build_run_result(task, seed, {"error": str(exc)}, 0.0, faults, start, budget, state, trace, True, str(exc))
            return self._build_run_result(task, seed, artifact, verifier_score, faults, start, budget, state, trace, False, None)

    def _run_root_frame(
        self,
        context: PolicyContext,
        frame: AgentFrame,
        task: BenchmarkTask,
        verifier_score: float,
        verified_terminal: bool,
    ) -> tuple[Any, int, float, bool]:
        faults = 0
        artifact: Any = None
        mode = self.runtime.topology.select_mode(context, frame, task.operations)
        context.state.mode = mode
        context.record("mode_selected", mode=mode)
        if mode == "single":
            artifact, local_faults = self._execute_operations(context, frame, task.operations)
            faults += local_faults
            verifier_score = self._maybe_verify(context, artifact, frame.metadata.get("run_node_id"))
            verified_terminal = verifier_score >= 1.0
            return artifact, faults, verifier_score, verified_terminal
        if mode == "vertical":
            children = self.runtime.topology.propose_children(context, frame, task.operations)
            for child in children:
                agent = self._resolve_agent(context, child)
                tool_scope = self.runtime.topology.assign_scope(context, child, context.state.visible_tool_names)
                context.state.queue.append(
                    AgentFrame(
                        agent=agent,
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
            context.state.queue.append(
                AgentFrame(
                    agent=self.shell.agent_pool.clone("root"),
                    objective="merge",
                    operation_ids=[],
                    depth=frame.depth,
                    role="merge_vertical",
                    tool_scope=[],
                    metadata={"parent_run_node_id": frame.metadata.get("run_node_id")},
                )
            )
            return artifact, faults, verifier_score, verified_terminal
        workers = self.runtime.topology.select_workers(context, frame, task.operations)
        worker_outputs = []
        for worker in workers:
            op_order = worker["op_ids"]
            worker_frame = AgentFrame(
                agent=self.shell.agent_pool.clone(worker.get("agent_id", "root")),
                objective=worker["instruction"],
                operation_ids=op_order,
                depth=frame.depth + 1,
                role="worker",
                worker_id=worker["worker_id"],
                tool_scope=worker.get("tool_scope", context.state.visible_tool_names),
                model_class=worker.get("model_class", "small"),
                metadata={**worker, "parent_run_node_id": frame.metadata.get("run_node_id")},
            )
            output, local_faults, checkpoint = self._execute_isolated_frame(
                context,
                worker_frame,
                [self._operation_by_id(task, op_id) for op_id in op_order],
                isolate_runtime_state=True,
            )
            worker_outputs.append(
                {
                    "worker_id": worker_frame.worker_id,
                    "artifact": output,
                    "verifier_support": self._worker_support(task, output),
                    "predicted_solve": worker.get("predicted_solve", 0.5),
                    "unresolved_critical": 0 if output else 1,
                    "summary": model_dump(checkpoint.summary),
                }
            )
            faults += local_faults
            self.shell.message_board.append(worker_frame.worker_id or "worker", {"artifact": output, "summary": model_dump(checkpoint.summary)})
        context.state.queue.append(
            AgentFrame(
                agent=self.shell.agent_pool.clone("root"),
                objective="merge",
                operation_ids=[],
                depth=frame.depth,
                role="merge_horizontal",
                metadata={"worker_outputs": worker_outputs, "parent_run_node_id": frame.metadata.get("run_node_id")},
            )
        )
        return artifact, faults, verifier_score, verified_terminal

    def _build_run_result(
        self,
        task: BenchmarkTask,
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
    ) -> RunResult:
        trace_path = str(self.shell.save_trace(task.task_id, seed, trace))
        return RunResult(
            task_id=task.task_id,
            seed=seed,
            artifact=artifact,
            verifier_score=verifier_score,
            cost=budget.cost,
            latency=time.perf_counter() - start,
            faults=faults,
            trace_path=trace_path,
            hard_invalid=hard_invalid,
            invalid_reason=invalid_reason,
            mode=state.mode,
            created_tools=state.created_tools,
            promoted_nodes=state.promoted_nodes,
            checks_used=state.checks_used,
            model_calls=budget.calls,
            tokens_used=budget.tokens,
            input_tokens=budget.input_tokens,
            output_tokens=budget.output_tokens,
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
        task = context.task
        results: dict[str, Any] = {}
        faults = 0
        run_node_id = frame.metadata.get("run_node_id")
        for operation in operations:
            event_id = self.shell.short_term.add_node("Event", operation.op_id, {"kind": operation.kind, "description": operation.description})
            if isinstance(run_node_id, str) and run_node_id in self.shell.short_term.nodes:
                self.shell.short_term.add_edge(run_node_id, event_id, "EMITS")
            resolved_args = dict(operation.args)
            for dep in operation.dependencies:
                dep_op = self._operation_by_id(task, dep)
                if dep_op.output_key in context.state.artifacts:
                    resolved_args[dep_op.output_key] = context.state.artifacts[dep_op.output_key]
                elif dep in context.state.artifacts:
                    resolved_args[dep] = context.state.artifacts[dep]
            model_class = self.runtime.control.assign_model(context, operation, frame)
            context.record("model_assigned", op_id=operation.op_id, model_class=model_class)
            if operation.kind == "memory_lookup":
                output = self._execute_memory_lookup(context, operation, run_node_id)
            elif operation.kind in {"builtin", "generated_expression"}:
                output, used_tool, created_tool, local_faults = self._execute_tool_operation(
                    context,
                    frame,
                    operation,
                    resolved_args,
                    run_node_id if isinstance(run_node_id, str) else None,
                )
                faults += local_faults
                context.record("tool_operation", op_id=operation.op_id, tool=used_tool, created=created_tool, output=output)
            else:
                output = resolved_args
            results[operation.output_key] = output
            context.state.artifacts[operation.output_key] = output
            self._record_artifact_node(self.shell.short_term, operation.output_key, output, run_node_id if isinstance(run_node_id, str) else None)
            context.state.unresolved_goals = [op.output_key for op in context.task.operations if op.output_key not in context.state.artifacts]
        if len(results) == 1:
            return next(iter(results.values())), faults
        return results, faults

    def _execute_memory_lookup(self, context: PolicyContext, operation: Any, run_node_id: str | None) -> Any:
        exact_symbols = [operation.requires_exact_symbol] if operation.requires_exact_symbol else context.task.symbolic_seeds
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
        feeds_downstream = any(operation.output_key in candidate.dependencies for candidate in context.task.operations)
        return self._coerce(node.content) if feeds_downstream else node.content

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
        categories = self.runtime.tooling.rank_categories(
            context,
            operation,
            self.shell.tool_registry.category_summaries,
        )
        inspected_categories = categories[: context.profile.tooling.k_c]
        candidate_tools: list[Any] = []
        for category in inspected_categories:
            candidate_tools.extend(self.shell.tool_registry.tools_in_category(category))
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
        if operation.kind == "generated_expression" and self.runtime.tooling.should_create_tool(context, operation, ranked_tool_names):
            synth_name = operation.tool_hint or f"synth:{operation.op_id}"
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
        dispatch_meta = self.runtime.tooling.dispatch_tool(context, tool_name, args)
        if dispatch_meta.get("async"):
            handle = self.shell.tool_executor.launch_async(
                tool_name,
                args,
                ensure_directory(self.shell.workspace / "handles"),
                context.task.task_id,
            )
            self.shell.open_handles.add(handle)
            context.state.open_handle_ids.append(handle.handle_id)
            handle_node_id = self.shell.short_term.add_node("OpenHandle", handle.tool_name, model_dump(handle))
            if run_node_id and run_node_id in self.shell.short_term.nodes:
                self.shell.short_term.add_edge(run_node_id, handle_node_id, "WAITS_ON")
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
                output = json.loads(Path(handle.stdout_path).read_text(encoding="utf-8") or "null")
        else:
            result = self.shell.tool_executor.run_tool(tool_name, args, context.task.task_id)
            context.budget.consume_tool_latency(result.latency_s)
            if not result.success:
                faults += 1
                context.record("tool_fault", tool=tool_name, stderr=result.stderr)
                self._record_tool_failure(context, operation, tool_name, result.stderr)
                raise HardInvalidation(f"tool execution failed for {tool_name}: {result.stderr}")
            output = result.output
        tool = self.shell.tool_registry.get(tool_name)
        if operation.kind == "generated_expression":
            self._record_procedure(context, operation, tool_name)
        if created_tool and self.runtime.tooling.promote_tool(context, tool):
            context.record("tool_promoted", tool=tool_name)
        return output, tool_name, created_tool, faults

    def _record_tool_failure(self, context: PolicyContext, operation: Any, tool_name: str, stderr: str) -> None:
        candidate = MemoryNode(
            node_id=stable_hash(context.task.task_id, operation.op_id, tool_name, stderr)[:16],
            type="ToolFailure",
            label=tool_name,
            content=stderr,
            embedding=[],
            symbol_set=[operation.op_id],
            file_paths=[],
            source_task_id=context.task.task_id,
            verifier_support=0.0,
            timestamps={"created": now_ts()},
            provenance={"source": "tool_fault", "operation": operation.op_id},
            tombstoned=False,
        )
        self._promote_memory_candidate(context, candidate)

    def _record_procedure(self, context: PolicyContext, operation: Any, tool_name: str) -> None:
        expression = getattr(operation, "expression", None)
        if not expression:
            return
        candidate = MemoryNode(
            node_id=stable_hash(context.task.task_id, tool_name, expression)[:16],
            type="Procedure",
            label=tool_name,
            content=expression,
            embedding=[],
            symbol_set=[operation.op_id],
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

    def _maybe_verify(self, context: PolicyContext, artifact: Any, run_node_id: str | None) -> float:
        checkers = self.runtime.control.request_checks(
            context,
            artifact,
            exact_verifier_exists=True,
            irreversible=True,
            external_visible=context.task.externally_visible,
        )
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
        return verifier_score

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

    def _all_outputs_present(self, task: BenchmarkTask, artifacts: Mapping[str, Any]) -> bool:
        return all(op.output_key in artifacts for op in task.operations)

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
