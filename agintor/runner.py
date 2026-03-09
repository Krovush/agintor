from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

from .benchmarks import BenchmarkTask
from .exceptions import HardInvalidation
from .memory_graph import LongTermGraph, ShortTermGraph
from .providers import ModelProvider
from .runtime_api import AgentFrame, PolicyContext, RuntimeBudget, RuntimeState
from .runtime_loader import LoadedRuntime
from .pydantic_compat import model_dump
from .schemas import AgentTemplate, BenchmarkTask, Checkpoint, ChildSpec, MemoryNode, ModelRequest, RunResult, SummaryRecord
from .shell import FixedShell
from .utils import cheap_embedding, count_tokens_rough, ensure_directory, lexical_overlap, mean, now_ts, stable_hash
from .verifiers import verify_task


class TaskRuntime:
    def __init__(self, runtime: LoadedRuntime, shell: FixedShell, provider: ModelProvider) -> None:
        self.runtime = runtime
        self.shell = shell
        self.provider = provider

    def run_task(self, task: BenchmarkTask, seed: int) -> RunResult:
        self.shell.reset_for_task(transfer_scored=task.transfer_scored)
        budget = RuntimeBudget()
        state = RuntimeState(visible_tool_names=sorted(self.shell.tool_registry.tools))
        trace: list[dict[str, Any]] = []
        context = PolicyContext(
            runtime_dir=self.runtime.runtime_dir,
            shell=self.shell,
            task=task,
            provider=self.provider,
            seed=seed,
            state=state,
            budget=budget,
            trace=trace,
            objective=task.prompt,
        )
        root = self.shell.agent_pool.clone("root")
        state.queue.append(AgentFrame(agent=root, objective=task.prompt, operation_ids=[op.op_id for op in task.operations], depth=0, role="root", tool_scope=state.visible_tool_names, model_class="medium"))
        artifact: Any = None
        faults = 0
        verifier_score = 0.0
        prev_best = 0.0
        verified_terminal = False
        start = time.perf_counter()
        self._ingest_context(context)
        try:
            step = 0
            while state.queue and step < 64:
                step += 1
                self.shell.validate_invariants(transfer_scored=task.transfer_scored)
                self._compact_if_needed(context)
                frame = state.queue.pop(0)
                self.shell.agent_pool.assert_clone(frame.agent)
                context.record("agent_start", step=step, agent_id=frame.agent.agent_id, role=frame.role, depth=frame.depth, op_ids=frame.operation_ids)
                if frame.role == "merge_vertical":
                    artifact = {op.output_key: state.artifacts.get(op.output_key) for op in task.operations}
                    if self._all_outputs_present(task, state.artifacts):
                        verifier_score = self._maybe_verify(context, artifact)
                        verified_terminal = verifier_score >= 1.0
                    context.record("merge_vertical", artifact=artifact)
                elif frame.role == "merge_horizontal":
                    worker_outputs = frame.metadata.get("worker_outputs", [])
                    artifact = self.runtime.topology.merge_ensemble(context, worker_outputs)
                    verifier_score = self._maybe_verify(context, artifact)
                    verified_terminal = verifier_score >= 1.0
                    context.record("merge_horizontal", artifact=artifact)
                else:
                    if frame.depth == 0:
                        mode = self.runtime.topology.select_mode(context, frame, task.operations)
                        state.mode = mode
                        context.record("mode_selected", mode=mode)
                        if mode == "single":
                            artifact, local_faults = self._execute_operations(context, frame, task.operations)
                            faults += local_faults
                            verifier_score = self._maybe_verify(context, artifact)
                            verified_terminal = verifier_score >= 1.0
                        elif mode == "vertical":
                            children = self.runtime.topology.propose_children(context, frame, task.operations)
                            for child in children:
                                agent = self._resolve_agent(context, child)
                                tool_scope = self.runtime.topology.assign_scope(context, child, state.visible_tool_names)
                                state.queue.append(AgentFrame(agent=agent, objective=child.instruction, operation_ids=[child.init_summary.get("op_id", child.child_id)], depth=frame.depth + 1, parent_id=frame.agent.agent_id, role=child.role, tool_scope=tool_scope, model_class=child.model_class, metadata={"child_spec": model_dump(child)}))
                            state.queue.append(AgentFrame(agent=self.shell.agent_pool.clone("root"), objective="merge", operation_ids=[], depth=frame.depth, role="merge_vertical", tool_scope=[]))
                        else:
                            workers = self.runtime.topology.select_workers(context, frame, task.operations)
                            worker_outputs = []
                            for worker in workers:
                                op_order = worker["op_ids"]
                                worker_frame = AgentFrame(agent=self.shell.agent_pool.clone(worker.get("agent_id", "root")), objective=worker["instruction"], operation_ids=op_order, depth=frame.depth + 1, role="worker", worker_id=worker["worker_id"], tool_scope=worker.get("tool_scope", state.visible_tool_names), model_class=worker.get("model_class", "small"), metadata=worker)
                                output, local_faults = self._execute_operations(context, worker_frame, [self._operation_by_id(task, op_id) for op_id in op_order])
                                worker_outputs.append({"worker_id": worker_frame.worker_id, "artifact": output, "verifier_support": self._worker_support(task, output), "predicted_solve": worker.get("predicted_solve", 0.5), "unresolved_critical": 0 if output else 1})
                                faults += local_faults
                                self.shell.message_board.append(worker_frame.worker_id or "worker", {"artifact": output})
                            state.queue.append(AgentFrame(agent=self.shell.agent_pool.clone("root"), objective="merge", operation_ids=[], depth=frame.depth, role="merge_horizontal", metadata={"worker_outputs": worker_outputs}))
                    else:
                        child_spec = frame.metadata.get("child_spec", {})
                        operations = [self._operation_by_id(task, op_id) for op_id in frame.operation_ids]
                        output, local_faults = self._execute_operations(context, frame, operations)
                        faults += local_faults
                        if len(operations) == 1:
                            state.artifacts[operations[0].output_key] = output if not isinstance(output, dict) else output.get(operations[0].output_key, output)
                        else:
                            for key, value in output.items():
                                state.artifacts[key] = value
                        checkpoint = self.runtime.topology.make_checkpoint(context, frame, state.artifacts, state.unresolved_goals, state.open_handle_ids)
                        state.checkpoints[frame.agent.agent_id + ":" + ",".join(frame.operation_ids)] = checkpoint
                        context.record("child_complete", role=frame.role, outputs=list(state.artifacts.keys()))
                unresolved = [op.output_key for op in task.operations if op.output_key not in state.artifacts and not (isinstance(artifact, dict) and op.output_key in artifact)]
                state.unresolved_goals = unresolved
                best_optimistic = self._best_next_action_utility(context, unresolved, verified_terminal)
                if self.runtime.control.stop_policy(context, best_optimistic, prev_best, len(unresolved), verified_terminal):
                    context.record("stop", unresolved=unresolved, verified=verified_terminal, best_optimistic=best_optimistic)
                    break
                prev_best = best_optimistic
                context.record("agent_end", step=step, unresolved=unresolved, verified=verified_terminal)
            if artifact is None and state.artifacts:
                artifact = {op.output_key: state.artifacts.get(op.output_key) for op in task.operations}
                verifier_score = self._maybe_verify(context, artifact)
                verified_terminal = verifier_score >= 1.0
            if artifact is None and not task.allow_best_effort:
                artifact = {"error": "controlled_failure"}
        except HardInvalidation as exc:
            trace_path = str(self.shell.save_trace(task.task_id, seed, trace))
            return RunResult(task_id=task.task_id, seed=seed, artifact={"error": str(exc)}, verifier_score=0.0, cost=budget.cost, latency=time.perf_counter() - start + budget.latency, faults=faults, trace_path=trace_path, hard_invalid=True, invalid_reason=str(exc), mode=state.mode, created_tools=state.created_tools, promoted_nodes=state.promoted_nodes, checks_used=state.checks_used)
        trace_path = str(self.shell.save_trace(task.task_id, seed, trace))
        return RunResult(task_id=task.task_id, seed=seed, artifact=artifact, verifier_score=verifier_score, cost=budget.cost, latency=time.perf_counter() - start + budget.latency, faults=faults, trace_path=trace_path, hard_invalid=False, mode=state.mode, created_tools=state.created_tools, promoted_nodes=state.promoted_nodes, checks_used=state.checks_used)

    def _ingest_context(self, context: PolicyContext) -> None:
        task = context.task
        for item in task.context_items:
            raw_id = self.shell.short_term.add_node("RawBlob", "context", item)
            context.record("context_ingested", raw_id=raw_id, item=item)
            candidate = None
            if "symbol" in item:
                candidate = MemoryNode(node_id=stable_hash(task.task_id, item["symbol"], item.get("value"))[:16], type="Symbol", label=item["symbol"], content=str(item.get("value")), embedding=[], symbol_set=[item["symbol"]], file_paths=[], source_task_id=task.task_id, verifier_support=1.0, timestamps={"created": now_ts()}, provenance={"source": "task_context"}, tombstoned=False)
            elif "file_path" in item:
                candidate = MemoryNode(node_id=stable_hash(task.task_id, item["file_path"], item.get("owner"))[:16], type="File", label=item["file_path"], content=str(item.get("owner")), embedding=[], symbol_set=[], file_paths=[item["file_path"]], source_task_id=task.task_id, verifier_support=1.0, timestamps={"created": now_ts()}, provenance={"source": "task_context"}, tombstoned=False)
            elif "rows" in item:
                candidate = MemoryNode(node_id=stable_hash(task.task_id, stable_hash(item))[:16], type="TaskNote", label="rows", content=json.dumps(item["rows"], sort_keys=True), embedding=[], symbol_set=[], file_paths=[], source_task_id=task.task_id, verifier_support=0.5, timestamps={"created": now_ts()}, provenance={"source": "task_context"}, tombstoned=False)
            if candidate is not None:
                score = self.runtime.memory.score_memory_unit(context, candidate, self.shell.long_term.all_nodes())
                if self.runtime.memory.should_promote(context, candidate, score):
                    action, target_id = self.runtime.memory.dedup_candidates(context, candidate, self.shell.long_term.all_nodes())
                    self.runtime.memory.upsert_memory(context, candidate, action, target_id)
                    context.state.promoted_nodes += 1
                    context.record("memory_promoted", node_id=candidate.node_id, node_type=candidate.type, action=action)

    def _compact_if_needed(self, context: PolicyContext) -> None:
        short_term = self.shell.short_term
        total_text = " ".join(str(node["content"]) for node in short_term.nodes.values())
        used_tokens = count_tokens_rough(total_text)
        fraction = used_tokens / 512.0
        if fraction <= 0.75:
            return
        span_ids = [node_id for node_id, node in short_term.nodes.items() if node["type"] in {"Event", "RawBlob"}]
        if not span_ids:
            return
        selected = self.runtime.memory.select_spans_for_compaction(context, span_ids, fraction)
        for group in selected:
            summary = self.runtime.memory.summarize_span(context, [short_term.nodes[node_id] for node_id in group])
            short_term.summary_replace(group, summary)
            context.record("compaction", node_ids=group, summary=summary.dict())

    def _resolve_agent(self, context: PolicyContext, child: ChildSpec) -> AgentTemplate:
        best_agent = None
        best_score = -1e9
        for agent in self.shell.agent_pool.list():
            score = self.runtime.topology.score_agent(context, agent, child)
            if score > best_score:
                best_score = score
                best_agent = agent
        if best_score < getattr(self.runtime.topology, "THETA_CREATE", 0.58):
            ephemeral = AgentTemplate(agent_id=child.child_id, description=child.instruction, capability_set=child.required_capabilities, symbol_set=[], default_tool_scope=child.tool_scope, success_stats={}, staleness_clock=0, model_policy_tag=child.model_class)
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
        for operation in operations:
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
                output = self._execute_memory_lookup(context, operation)
                results[operation.output_key] = output
                context.state.artifacts[operation.output_key] = output
            elif operation.kind in {"builtin", "generated_expression"}:
                output, used_tool, created_tool, local_faults = self._execute_tool_operation(context, operation, resolved_args)
                faults += local_faults
                results[operation.output_key] = output
                context.state.artifacts[operation.output_key] = output
                context.record("tool_operation", op_id=operation.op_id, tool=used_tool, created=created_tool, output=output)
            else:
                results[operation.output_key] = resolved_args
            context.state.unresolved_goals = [op.output_key for op in context.task.operations if op.output_key not in context.state.artifacts]
        if len(results) == 1:
            return next(iter(results.values())), faults
        return results, faults

    def _execute_memory_lookup(self, context: PolicyContext, operation: Any) -> Any:
        exact_symbols = [operation.requires_exact_symbol] if operation.requires_exact_symbol else context.task.symbolic_seeds
        candidates = self.shell.long_term.retrieve_candidates(context.task.prompt, exact_symbols, context.task.file_paths)
        ranked = self.runtime.memory.retrieve_long_term(context, context.task.prompt, exact_symbols, context.task.file_paths, candidates)
        if not ranked:
            raise HardInvalidation("memory retrieval returned no candidates for exact symbol/path query")
        node = ranked[0]
        self.shell.short_term.add_node("VerifierEvidence", operation.output_key, {"retrieved": node.node_id, "label": node.label})
        def _coerce(value):
            if isinstance(value, (int, float)):
                return value
            if isinstance(value, str):
                try:
                    import json
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
        if node.type == "Symbol":
            return _coerce(node.content)
        if node.type == "File":
            return _coerce(node.content)
        return _coerce(node.content)

    def _execute_tool_operation(self, context: PolicyContext, operation: Any, args: Mapping[str, Any]) -> tuple[Any, str, bool, int]:
        faults = 0
        categories = self.runtime.tooling.rank_categories(context, operation, self.shell.tool_registry.category_summaries)
        candidate_tools = []
        inspected_categories = categories[: getattr(self.runtime.tooling, "K_C", 3)]
        for category in inspected_categories:
            candidate_tools.extend(self.shell.tool_registry.tools_in_category(category))
        ranked_tool_names = self.runtime.tooling.rank_tools(context, operation, candidate_tools)
        created_tool = False
        tool_name = operation.tool_hint if operation.tool_hint and operation.tool_hint in context.state.visible_tool_names else (ranked_tool_names[0] if ranked_tool_names else None)
        if operation.kind == "generated_expression" and self.runtime.tooling.should_create_tool(context, operation, ranked_tool_names):
            spec, source, executor = self.runtime.tooling.propose_tool_spec(context, operation)
            if self.runtime.tooling.validate_tool(context, spec, source):
                self.shell.tool_registry.register_generated_tool(spec, source, executor=executor)
                tool_name = spec.name
                created_tool = True
                context.state.created_tools += 1
                context.state.visible_tool_names.append(tool_name)
        if tool_name is None:
            raise HardInvalidation("no tool available after category-first discovery")
        dispatch_meta = self.runtime.tooling.dispatch_tool(context, tool_name, args)
        if dispatch_meta.get("async"):
            handle = self.shell.tool_executor.launch_async(tool_name, args, ensure_directory(self.shell.workspace / "handles"))
            self.shell.open_handles.add(handle)
            context.state.open_handle_ids.append(handle.handle_id)
            self.shell.open_handles.update_state(handle.handle_id, "completed")
            output = json.loads(Path(handle.stdout_path).read_text(encoding="utf-8") or "null")
        else:
            result = self.shell.tool_executor.run_tool(tool_name, args, context.task.task_id)
            if not result.success:
                faults += 1
                context.record("tool_fault", tool=tool_name, stderr=result.stderr)
                raise HardInvalidation(f"tool execution failed for {tool_name}: {result.stderr}")
            output = result.output
        tool = self.shell.tool_registry.get(tool_name)
        if created_tool and self.runtime.tooling.promote_tool(context, tool):
            context.record("tool_promoted", tool=tool_name)
        return output, tool_name, created_tool, faults

    def _maybe_verify(self, context: PolicyContext, artifact: Any) -> float:
        checkers = self.runtime.control.request_checks(context, artifact, exact_verifier_exists=True, irreversible=True, external_visible=context.task.externally_visible)
        context.state.checks_used += len(checkers)
        if "benchmark" in checkers:
            return verify_task(context.task, artifact, context.trace)
        return 0.0

    def _best_next_action_utility(self, context: PolicyContext, unresolved: Sequence[str], verified_terminal: bool) -> float:
        if not unresolved and verified_terminal:
            return -0.1
        remaining_budget = 1.0 - max(context.budget.normalized().values())
        return max(-0.5, 0.4 * remaining_budget - 0.2 * len(unresolved))

    def _worker_support(self, task: BenchmarkTask, artifact: Any) -> float:
        return verify_task(task, artifact, [])

    def _all_outputs_present(self, task: BenchmarkTask, artifacts: Mapping[str, Any]) -> bool:
        return all(op.output_key in artifacts for op in task.operations)

    def _operation_by_id(self, task: BenchmarkTask, op_id: str):
        for operation in task.operations:
            if operation.op_id == op_id:
                return operation
        raise KeyError(op_id)
