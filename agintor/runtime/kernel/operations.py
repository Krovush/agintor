from __future__ import annotations

import json
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


class OperationsMixin:
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

    @staticmethod
    def _decode_direct_response_output(raw_text: str) -> Any:
        try:
            return json.loads(raw_text)
        except Exception:
            return raw_text

    @classmethod
    def _jsonable_prompt_inputs(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(key): cls._jsonable_prompt_inputs(item) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._jsonable_prompt_inputs(item) for item in value]
        return value

    def _prompt_lines_with_session_carryover(self, context: PolicyContext) -> list[str]:
        prompt_lines = [context.task.prompt]
        session_carryover = self._session_carryover_rows(context)
        if session_carryover:
            prompt_lines.append("Session carryover:")
            prompt_lines.append(json.dumps(session_carryover, sort_keys=True, default=str))
        return prompt_lines

    def _direct_response_prompt(
        self,
        context: PolicyContext,
        resolved_args: Mapping[str, Any],
    ) -> str:
        prompt_lines = self._prompt_lines_with_session_carryover(context)
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

    @staticmethod
    def _session_carryover_rows(context: PolicyContext) -> list[dict[str, Any]]:
        message_board = getattr(getattr(context, "shell", None), "message_board", None)
        entries = getattr(message_board, "entries", [])
        rows: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            if str(entry.get("kind") or "") != "session_carryover":
                continue
            payload = entry.get("payload")
            rows.append(dict(payload) if isinstance(payload, Mapping) else dict(entry))
        return rows

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
