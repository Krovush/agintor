from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ...contracts import GraphNodeSpec, RuntimeSpec
from .state import LangGraphRuntimeState


class RuntimeOperationProtocol(Protocol):
    def execute_node(self, runtime_spec: RuntimeSpec, node: GraphNodeSpec, state: LangGraphRuntimeState) -> LangGraphRuntimeState: ...


@dataclass
class RuntimeOperationService:
    """Shared operation bridge used by generated LangGraph nodes.

    The service keeps the graph layer thin: graph nodes select the RuntimeSpec
    node, and this object performs deterministic dispatch or delegates to the
    existing runtime kernel once wired by RuntimeHost.
    """

    delegate: RuntimeOperationProtocol | None = None

    def execute_node(self, runtime_spec: RuntimeSpec, node: GraphNodeSpec, state: LangGraphRuntimeState) -> LangGraphRuntimeState:
        if self.delegate is not None:
            return self.delegate.execute_node(runtime_spec, node, state)
        completed = list(state.get("completed_node_ids", []))
        if node.node_id not in completed:
            completed.append(node.node_id)
        artifacts = dict(state.get("artifacts", {}))
        if node.outputs:
            for output in node.outputs:
                artifacts.setdefault(output, {"node_id": node.node_id, "status": "completed"})
        trace_rows = list(state.get("trace_rows", []))
        trace_rows.append({"event": "langgraph_node_completed", "node_id": node.node_id, "node_kind": node.node_kind})
        return {
            **state,
            "current_node_id": node.node_id,
            "completed_node_ids": completed,
            "artifacts": artifacts,
            "trace_rows": trace_rows,
        }


__all__ = ["RuntimeOperationProtocol", "RuntimeOperationService"]
