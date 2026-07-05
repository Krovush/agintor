from __future__ import annotations

from typing import Any

from ...contracts import GraphNodeSpec, RuntimeSpec, validate_runtime_spec_payload
from ...utils import stable_hash
from .operation_service import RuntimeOperationService
from .state import LangGraphRuntimeState

RUNTIME_SPEC_FILE = "runtime_spec.json"
GENERATED_APP_FILE = "generated_langgraph_app.py"


def _initial_state(
    runtime_spec: RuntimeSpec,
    prompt: str,
    *,
    request_id: str = "",
    task_id: str = "",
    seed: int = 0,
    runtime_hash: str = "",
    trace_context: Any | None = None,
) -> LangGraphRuntimeState:
    if hasattr(trace_context, "model_dump"):
        trace_context = trace_context.model_dump(mode="json", exclude_none=True)
    return LangGraphRuntimeState(
        request_id=request_id,
        task_id=task_id,
        seed=seed,
        prompt=prompt,
        runtime_hash=runtime_hash,
        runtime_spec_digest=runtime_spec.spec_digest,
        trace_context=trace_context,
        budget={"model_calls": 0, "tool_calls": 0},
    )


class CompiledSpecRuntime:
    """Pass-1 spec executor.

    LangGraph usage is deliberately limited to `StateGraph(dict)`, `add_node`,
    `set_entry_point`, `add_edge`, `set_finish_point`, `compile`, and `invoke`.
    Conditional edges, parallel branches, subgraphs, interrupts/resume, and
    LangGraph-native checkpointers are pass-2 work.
    """

    def __init__(self, runtime_spec: RuntimeSpec, *, provider: Any | None = None, provider_override: Any | None = None) -> None:
        self.runtime_spec = validate_runtime_spec_payload(runtime_spec)
        self.service = RuntimeOperationService(self.runtime_spec, provider=provider_override or provider)
        self._lg_app = self._build_langgraph_app()
        self.backend = "langgraph" if self._lg_app is not None else "sequential"

    def _build_langgraph_app(self) -> Any | None:
        try:
            from langgraph.graph import StateGraph
        except Exception:
            return None

        graph = StateGraph(dict)
        for node in self.runtime_spec.graph.nodes:
            graph.add_node(node.node_id, self._lg_callable(node))
        graph.set_entry_point(self.runtime_spec.graph.entry_node)
        for edge in self.runtime_spec.graph.edges:
            graph.add_edge(edge.source, edge.target)
        for terminal in self.runtime_spec.graph.terminal_nodes:
            graph.set_finish_point(terminal)
        return graph.compile()

    def _lg_callable(self, node: GraphNodeSpec):
        def run(raw_state: dict[str, Any]) -> dict[str, Any]:
            state = LangGraphRuntimeState.model_validate(raw_state)
            result = self.service.run_node(state, node)
            if result.status == "failed":
                state.status = "failed"
            return state.model_dump(mode="json", exclude_none=True)

        return run

    def invoke(
        self,
        prompt: str,
        *,
        request_id: str = "",
        task_id: str = "",
        seed: int = 0,
        runtime_hash: str = "",
        trace_context: Any | None = None,
    ) -> LangGraphRuntimeState:
        state = _initial_state(
            self.runtime_spec,
            prompt,
            request_id=request_id,
            task_id=task_id,
            seed=seed,
            runtime_hash=runtime_hash,
            trace_context=trace_context,
        )
        if self._lg_app is not None:
            raw = self._lg_app.invoke(state.model_dump(mode="json", exclude_none=True))
            return LangGraphRuntimeState.model_validate(raw)
        return self._invoke_sequential(state)

    def _invoke_sequential(self, state: LangGraphRuntimeState) -> LangGraphRuntimeState:
        node_by_id = {node.node_id: node for node in self.runtime_spec.graph.nodes}
        current = self.runtime_spec.graph.entry_node
        visited: set[str] = set()
        terminals = set(self.runtime_spec.graph.terminal_nodes)
        while current and current not in visited:
            visited.add(current)
            node = node_by_id[current]
            result = self.service.run_node(state, node)
            if result.status == "failed":
                break
            if current in terminals:
                state.status = "completed"
                break
            outgoing = [edge for edge in self.runtime_spec.graph.edges if edge.source == current]
            if not outgoing:
                state.status = "completed"
                break
            outgoing.sort(key=lambda edge: edge.priority)
            current = outgoing[0].target
        if state.status == "running":
            state.status = "completed"
        return state


def compile_runtime_spec(runtime_spec: RuntimeSpec | dict[str, Any], *, provider: Any | None = None, provider_override: Any | None = None) -> CompiledSpecRuntime:
    return CompiledSpecRuntime(validate_runtime_spec_payload(runtime_spec), provider=provider, provider_override=provider_override)


def runtime_spec_code_hash(spec: RuntimeSpec) -> str:
    return stable_hash("langgraph_spec", spec.spec_digest)


__all__ = ["CompiledSpecRuntime", "GENERATED_APP_FILE", "RUNTIME_SPEC_FILE", "compile_runtime_spec", "runtime_spec_code_hash"]
