from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ...contracts import GraphNodeSpec, RuntimeSpec
from ...contracts.runtime_spec import runtime_spec_digest
from ...utils import ensure_directory
from .operation_service import RuntimeOperationService
from .state import LangGraphRuntimeState, initial_langgraph_state


@dataclass
class CompiledRuntimeGraph:
    runtime_spec: RuntimeSpec
    invoke: Callable[[LangGraphRuntimeState], LangGraphRuntimeState]
    backend: str = "sequential-fallback"


class LangGraphRuntimeCompiler:
    def __init__(self, operation_service: RuntimeOperationService | None = None) -> None:
        self.operation_service = operation_service or RuntimeOperationService()

    def compile(self, runtime_spec: RuntimeSpec) -> CompiledRuntimeGraph:
        # Import lazily. If LangGraph is not installed, use the deterministic
        # sequential fallback so pre-MVP tests do not require the dependency.
        try:
            from langgraph.graph import StateGraph  # type: ignore
        except Exception:
            return self._compile_sequential(runtime_spec)
        graph = StateGraph(dict)
        node_by_id = {node.node_id: node for node in runtime_spec.graph.nodes}
        for node in runtime_spec.graph.nodes:
            graph.add_node(node.node_id, self._node_callable(runtime_spec, node))
        graph.set_entry_point(runtime_spec.graph.entry_node_id)
        for edge in runtime_spec.graph.edges:
            graph.add_edge(edge.source, edge.target)
        for terminal_id in runtime_spec.graph.terminal_node_ids:
            graph.set_finish_point(terminal_id)
        app = graph.compile()
        return CompiledRuntimeGraph(runtime_spec=runtime_spec, invoke=lambda state: dict(app.invoke(dict(state))), backend="langgraph")

    def _compile_sequential(self, runtime_spec: RuntimeSpec) -> CompiledRuntimeGraph:
        ordered_nodes = self._topological_order(runtime_spec)

        def invoke(state: LangGraphRuntimeState) -> LangGraphRuntimeState:
            current = dict(state)
            for node in ordered_nodes:
                current = self.operation_service.execute_node(runtime_spec, node, current)
            return current

        return CompiledRuntimeGraph(runtime_spec=runtime_spec, invoke=invoke)

    def _node_callable(self, runtime_spec: RuntimeSpec, node: GraphNodeSpec):
        def run(state: dict[str, Any]) -> dict[str, Any]:
            return dict(self.operation_service.execute_node(runtime_spec, node, dict(state)))
        return run

    @staticmethod
    def _topological_order(runtime_spec: RuntimeSpec) -> list[GraphNodeSpec]:
        nodes = {node.node_id: node for node in runtime_spec.graph.nodes}
        incoming = {node_id: set() for node_id in nodes}
        outgoing = {node_id: set() for node_id in nodes}
        for edge in runtime_spec.graph.edges:
            incoming[edge.target].add(edge.source)
            outgoing[edge.source].add(edge.target)
        queue = [runtime_spec.graph.entry_node_id]
        seen: set[str] = set()
        ordered: list[GraphNodeSpec] = []
        while queue:
            node_id = queue.pop(0)
            if node_id in seen:
                continue
            seen.add(node_id)
            ordered.append(nodes[node_id])
            for target in sorted(outgoing[node_id]):
                if incoming[target].issubset(seen):
                    queue.append(target)
        for node_id in sorted(set(nodes) - seen):
            ordered.append(nodes[node_id])
        return ordered

    def smoke_run(self, runtime_spec: RuntimeSpec) -> LangGraphRuntimeState:
        compiled = self.compile(runtime_spec)
        return compiled.invoke(initial_langgraph_state(
            request_id="smoke",
            plan_id="smoke",
            runtime_id=runtime_spec.runtime_id,
            runtime_spec_digest=runtime_spec_digest(runtime_spec),
        ))

    def export_generated_app(self, runtime_spec: RuntimeSpec, output_dir: str | Path) -> Path:
        root = ensure_directory(output_dir)
        (root / "runtime_spec.json").write_text(json.dumps(runtime_spec.model_dump(mode="json", exclude_none=True), indent=2, sort_keys=True), encoding="utf-8")
        app_path = root / "langgraph_app.py"
        app_path.write_text(
            "from agintor.runtime.langgraph.compiler import LangGraphRuntimeCompiler\n"
            "from agintor.contracts import RuntimeSpec\n"
            "import json, pathlib\n\n"
            "def load_app(runtime_dir):\n"
            "    spec = RuntimeSpec.model_validate(json.loads((pathlib.Path(runtime_dir) / 'runtime_spec.json').read_text()))\n"
            "    return LangGraphRuntimeCompiler().compile(spec)\n",
            encoding="utf-8",
        )
        return app_path


__all__ = ["CompiledRuntimeGraph", "LangGraphRuntimeCompiler"]
