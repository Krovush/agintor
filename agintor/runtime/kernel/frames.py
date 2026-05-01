from __future__ import annotations

from typing import Any, Mapping, Sequence
from .memory_graph import ShortTermGraph
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


class FramesMixin:
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
            summary_id = graph.add_node("Summary", checkpoint.summary.objective, (checkpoint.summary).model_dump(), source="checkpoint")
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
        shell_snapshot: Any | None = None
        if isolate_runtime_state:
            isolated_state = self._make_isolated_state(parent_state)
            context.state = isolated_state
            shell_snapshot = self.shell.snapshot_checkpoint_shell_state(boundary="isolated_frame")
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
            if isolate_runtime_state:
                local_state = context.state
                context.state = parent_state
                if shell_snapshot is not None:
                    self.shell.restore_checkpoint_shell_state(shell_snapshot)
                if isolated_state is not None:
                    parent_state.created_tools += isolated_state.created_tools
                    parent_state.promoted_nodes += isolated_state.promoted_nodes
                    parent_state.checks_used += isolated_state.checks_used
            else:
                self.shell.short_term = parent_short_term
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

    def _publish_checkpoint_summary(self, frame: AgentFrame, checkpoint: Checkpoint) -> None:
        summary_id = self.shell.short_term.add_node(
            "Summary",
            checkpoint.summary.objective,
            (checkpoint.summary).model_dump(),
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
                handle_node_id = self.shell.short_term.add_node("OpenHandle", handle.tool_name, (handle).model_dump())
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
