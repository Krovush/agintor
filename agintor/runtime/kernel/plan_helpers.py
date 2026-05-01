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
from ...utils import count_tokens_rough, ensure_directory, merge_provider_usage, now_ts, stable_hash


class PlanHelpersMixin:
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

    def _store_output_artifacts(self, state: RuntimeState, operations: Sequence[Any], output: Any) -> None:
        if len(operations) == 1:
            state.artifacts[operations[0].output_key] = output if not isinstance(output, dict) else output.get(operations[0].output_key, output)
            return
        if isinstance(output, dict):
            for key, value in output.items():
                state.artifacts[key] = value

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

    def _validate_execution_plan(self, plan: ExecutionPlan) -> ExecutionPlan:
        return (ExecutionPlan).model_validate((plan).model_dump())

    @staticmethod
    def _node_operation_kind(node: PlanNode) -> str:
        return str(node.metadata.get("operation_kind", node.node_kind))

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
