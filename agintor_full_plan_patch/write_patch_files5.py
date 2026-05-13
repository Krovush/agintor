from __future__ import annotations
from pathlib import Path
import textwrap
ROOT=Path('/mnt/data/agintor_full_plan_patch/new_files'); files={}
def add(p,c): files[p]=textwrap.dedent(c).lstrip()

add('agintor/runtime/langgraph/__init__.py', r'''
from __future__ import annotations

from .compiler import *  # noqa: F401,F403
from .state import *  # noqa: F401,F403
from .operation_service import *  # noqa: F401,F403
from .checkpointing import *  # noqa: F401,F403
from .adapters import *  # noqa: F401,F403
''')

add('agintor/runtime/langgraph/state.py', r'''
from __future__ import annotations

from typing import Any, TypedDict


class LangGraphRuntimeState(TypedDict, total=False):
    request_id: str
    plan_id: str
    runtime_id: str
    runtime_spec_digest: str
    current_node_id: str
    completed_node_ids: list[str]
    artifacts: dict[str, Any]
    trace_rows: list[dict[str, Any]]
    side_effect_receipts: list[dict[str, Any]]
    budget: dict[str, Any]
    checkpoint_ref: str
    error: str


def initial_langgraph_state(*, request_id: str, plan_id: str, runtime_id: str, runtime_spec_digest: str) -> LangGraphRuntimeState:
    return {
        "request_id": request_id,
        "plan_id": plan_id,
        "runtime_id": runtime_id,
        "runtime_spec_digest": runtime_spec_digest,
        "completed_node_ids": [],
        "artifacts": {},
        "trace_rows": [],
        "side_effect_receipts": [],
        "budget": {},
    }


__all__ = ["LangGraphRuntimeState", "initial_langgraph_state"]
''')

add('agintor/runtime/langgraph/operation_service.py', r'''
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
''')

add('agintor/runtime/langgraph/checkpointing.py', r'''
from __future__ import annotations

from typing import Any

from ...contracts import CheckpointEnvelope
from ...utils import stable_hash
from .state import LangGraphRuntimeState


def langgraph_state_digest(state: LangGraphRuntimeState) -> str:
    return stable_hash(dict(state))


def state_to_checkpoint_payload(state: LangGraphRuntimeState) -> dict[str, Any]:
    return {"langgraph_state": dict(state), "state_digest": langgraph_state_digest(state)}


def state_from_checkpoint_payload(payload: dict[str, Any]) -> LangGraphRuntimeState:
    return dict(payload.get("langgraph_state", {}))


def embed_langgraph_state_in_checkpoint(envelope: CheckpointEnvelope, state: LangGraphRuntimeState) -> CheckpointEnvelope:
    payload = envelope.model_dump(mode="json", exclude_none=True)
    payload.setdefault("metadata", {})["langgraph_state"] = state_to_checkpoint_payload(state)
    return CheckpointEnvelope.model_validate(payload)


__all__ = [
    "embed_langgraph_state_in_checkpoint",
    "langgraph_state_digest",
    "state_from_checkpoint_payload",
    "state_to_checkpoint_payload",
]
''')

add('agintor/runtime/langgraph/adapters.py', r'''
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ...contracts import ModelPolicy, ToolSpec


@dataclass(frozen=True)
class LangChainToolBinding:
    tool_id: str
    name: str
    category: str
    callable_ref: Callable[..., Any] | None = None
    metadata: dict[str, Any] | None = None


def tool_spec_to_binding(tool: ToolSpec) -> LangChainToolBinding:
    return LangChainToolBinding(
        tool_id=tool.tool_id,
        name=tool.name,
        category=tool.category,
        callable_ref=None,
        metadata={"binding": dict(tool.binding), "side_effect_kind": tool.side_effect_kind},
    )


def model_policy_to_langchain_config(policy: ModelPolicy) -> dict[str, Any]:
    return {
        "provider_name": policy.provider_name,
        "model_class": policy.model_class,
        "temperature": policy.temperature,
        "max_output_tokens": policy.max_output_tokens,
        "metadata": dict(policy.metadata),
    }


__all__ = ["LangChainToolBinding", "model_policy_to_langchain_config", "tool_spec_to_binding"]
''')

add('agintor/runtime/langgraph/compiler.py', r'''
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
''')

for p,c in files.items():
    t=ROOT/p; t.parent.mkdir(parents=True, exist_ok=True); t.write_text(c, encoding='utf-8')
print('wrote', len(files), 'files')
