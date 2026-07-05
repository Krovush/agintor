from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any
from typing import Literal

from ...contracts import GraphNodeSpec, RuntimeSpec, validate_runtime_spec_payload
from ...utils import stable_hash
from .operation_service import RuntimeOperationService
from .state import LangGraphRuntimeState

RUNTIME_SPEC_FILE = "runtime_spec.json"
GENERATED_APP_FILE = "generated_langgraph_app.py"
BackendPreference = Literal["auto", "langgraph", "sequential"]
SUPPORTED_NODE_TYPES = frozenset(
    {
        "agent",
        "tool",
        "merge",
        "verify",
        "direct_response",
        "builtin",
        "service_action",
        "repo_patch",
    }
)
UNSUPPORTED_METADATA_FEATURES = frozenset(
    {
        "conditional_edge",
        "conditional_edges",
        "parallel",
        "fanout",
        "fan_out",
        "interrupt",
        "resume",
        "subgraph",
        "subgraphs",
        "checkpointer",
        "checkpoint",
    }
)


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


def _truthy_feature(value: Any) -> bool:
    return value not in (False, None, "", (), [], {})


def _validate_metadata_subset(metadata: dict[str, Any], *, path: str) -> None:
    for raw_key, value in metadata.items():
        key = str(raw_key).strip().lower().replace("-", "_")
        if key in UNSUPPORTED_METADATA_FEATURES and _truthy_feature(value):
            raise ValueError(f"pass-1 LangGraph executor does not support {key!r} metadata at {path}")


def validate_pass1_supported_subset(runtime_spec: RuntimeSpec | dict[str, Any]) -> RuntimeSpec:
    """Validate the graph subset implemented by both pass-1 backends."""

    spec = validate_runtime_spec_payload(runtime_spec)
    graph = spec.graph
    _validate_metadata_subset(spec.metadata, path="runtime.metadata")
    _validate_metadata_subset(graph.metadata, path="graph.metadata")
    _validate_metadata_subset(spec.execution.metadata, path="execution.metadata")
    if spec.execution.allow_parallel:
        raise ValueError("pass-1 LangGraph executor does not support execution.allow_parallel=True")
    if len(graph.terminal_nodes) != 1:
        raise ValueError("pass-1 LangGraph executor requires exactly one terminal node")

    output_keys = [node.output_key or node.node_id for node in graph.nodes]
    duplicate_output_keys = sorted(key for key, count in Counter(output_keys).items() if count > 1)
    if duplicate_output_keys:
        raise ValueError(f"pass-1 LangGraph executor does not support duplicate graph output keys: {duplicate_output_keys}")

    for node in graph.nodes:
        if node.node_type not in SUPPORTED_NODE_TYPES:
            raise ValueError(f"pass-1 LangGraph executor does not support node_type {node.node_type!r} on {node.node_id!r}")
        if node.condition != "always":
            raise ValueError(f"pass-1 LangGraph executor does not support node condition {node.condition!r} on {node.node_id!r}")
        _validate_metadata_subset(node.metadata, path=f"graph.nodes.{node.node_id}.metadata")

    outgoing: dict[str, list[Any]] = defaultdict(list)
    for edge in graph.edges:
        if edge.condition != "always":
            raise ValueError(
                f"pass-1 LangGraph executor does not support edge condition {edge.condition!r} on {edge.source!r}->{edge.target!r}"
            )
        _validate_metadata_subset(edge.metadata, path=f"graph.edges.{edge.source}->{edge.target}.metadata")
        outgoing[edge.source].append(edge)
    fanout_sources = sorted(source for source, edges in outgoing.items() if len(edges) > 1)
    if fanout_sources:
        raise ValueError(f"pass-1 LangGraph executor does not support parallel fan-out from nodes: {fanout_sources}")

    terminal = graph.terminal_nodes[0]
    terminal_outgoing = [f"{edge.source}->{edge.target}" for edge in outgoing.get(terminal, [])]
    if terminal_outgoing:
        raise ValueError(f"pass-1 LangGraph executor does not support outgoing edges from terminal nodes: {terminal_outgoing}")

    current = graph.entry_node
    visited: set[str] = set()
    while current:
        if current in visited:
            raise ValueError(f"pass-1 LangGraph executor does not support graph cycles at {current!r}")
        visited.add(current)
        if current == terminal:
            break
        next_edges = outgoing.get(current, [])
        if not next_edges:
            raise ValueError(f"pass-1 LangGraph executor requires a path from entry_node to terminal node; dead end at {current!r}")
        current = next_edges[0].target
    if current != terminal:
        raise ValueError("pass-1 LangGraph executor requires the entry path to reach the terminal node")

    node_ids = {node.node_id for node in graph.nodes}
    unreachable = sorted(node_ids - visited)
    if unreachable:
        raise ValueError(f"pass-1 LangGraph executor does not support disconnected graph nodes: {unreachable}")
    return spec


class CompiledSpecRuntime:
    """Pass-1 spec executor.

    LangGraph usage is deliberately limited to `StateGraph(dict)`, `add_node`,
    `set_entry_point`, `add_edge`, `set_finish_point`, `compile`, and `invoke`.
    Conditional edges, parallel branches, subgraphs, interrupts/resume, and
    LangGraph-native checkpointers are pass-2 work.
    """

    def __init__(
        self,
        runtime_spec: RuntimeSpec,
        *,
        provider: Any | None = None,
        provider_override: Any | None = None,
        backend: BackendPreference = "auto",
    ) -> None:
        if backend not in {"auto", "langgraph", "sequential"}:
            raise ValueError(f"unsupported spec runtime backend preference {backend!r}")
        self.runtime_spec = validate_pass1_supported_subset(runtime_spec)
        self.service = RuntimeOperationService(self.runtime_spec, provider=provider_override or provider)
        self._lg_app = None if backend == "sequential" else self._build_langgraph_app(required=backend == "langgraph")
        self.backend = "langgraph" if self._lg_app is not None else "sequential"

    def _build_langgraph_app(self, *, required: bool = False) -> Any | None:
        try:
            from langgraph.graph import StateGraph
        except Exception as exc:
            if required:
                raise RuntimeError("langgraph backend requested but langgraph.graph.StateGraph is unavailable") from exc
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
            if state.status == "failed":
                return state.model_dump(mode="json", exclude_none=True)
            result = self.service.run_node(state, node)
            if result.status == "failed":
                state.status = "failed"
            elif node.node_id in self.runtime_spec.graph.terminal_nodes:
                state.status = "completed"
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


def compile_runtime_spec(
    runtime_spec: RuntimeSpec | dict[str, Any],
    *,
    provider: Any | None = None,
    provider_override: Any | None = None,
    backend: BackendPreference = "auto",
) -> CompiledSpecRuntime:
    return CompiledSpecRuntime(
        validate_runtime_spec_payload(runtime_spec),
        provider=provider,
        provider_override=provider_override,
        backend=backend,
    )


def runtime_spec_code_hash(spec: RuntimeSpec) -> str:
    return stable_hash("langgraph_spec", spec.spec_digest)


__all__ = [
    "BackendPreference",
    "CompiledSpecRuntime",
    "GENERATED_APP_FILE",
    "RUNTIME_SPEC_FILE",
    "compile_runtime_spec",
    "runtime_spec_code_hash",
    "validate_pass1_supported_subset",
]
