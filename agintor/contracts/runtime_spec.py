from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..utils import now_ts, stable_hash


RuntimeKind = Literal["policy_modules", "langgraph_spec", "tradingagents_langgraph"]
RuntimeScope = Literal["top", "mem", "tool", "ctl"]
FORBIDDEN_PRIVATE_KEY_PREFIXES = ("private_", "sealed_", "hidden_", "oracle_private_")
FORBIDDEN_PRIVATE_KEY_NAMES = {
    "private_expected",
    "private_answer",
    "private_answer_ref",
    "hidden_tests",
    "private_rubric",
    "promotion_threshold",
    "sealed_inputs",
    "sealed_fixture_refs",
}
_DIGEST_EXCLUDE_KEYS = {"created_at", "completed_at", "metadata", "parent_spec_digest", "child_spec_digest", "spec_digest"}


class RuntimeSpecModel(BaseModel):
    """Base model for the spec-backed runtime genome.

    The runtime genome is JSON, not live LangChain/LangGraph objects and not
    generated Python code. Generated code is a compilation artifact of this
    spec.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=True)


def _as_plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    return value


def assert_no_private_or_sealed_keys(value: Any, *, path: str = "<root>") -> None:
    value = _as_plain(value)
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key)
            child_path = f"{path}.{key}" if path else key
            if key in FORBIDDEN_PRIVATE_KEY_NAMES or key.startswith(FORBIDDEN_PRIVATE_KEY_PREFIXES):
                raise ValueError(f"private/sealed key {key!r} is not allowed at {child_path}")
            if key == "authority_boundary" and str(item) == "sealed_validator":
                raise ValueError(f"sealed validator authority is not allowed at {child_path}")
            assert_no_private_or_sealed_keys(item, path=child_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            assert_no_private_or_sealed_keys(item, path=f"{path}[{index}]")


def _digest_payload(value: Any) -> Any:
    value = _as_plain(value)
    if isinstance(value, Mapping):
        return {
            str(key): _digest_payload(item)
            for key, item in sorted(value.items())
            if str(key) not in _DIGEST_EXCLUDE_KEYS
        }
    if isinstance(value, list):
        return [_digest_payload(item) for item in value]
    return value


class PromptSpec(RuntimeSpecModel):
    system: str = ""
    developer: str = ""
    task_template: str = "{prompt}"
    output_instructions: str = "Return the requested answer."
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentSpec(RuntimeSpecModel):
    agent_id: str
    role: str
    description: str = ""
    prompt: PromptSpec = Field(default_factory=PromptSpec)
    model_policy_id: str = "default"
    tool_ids: list[str] = Field(default_factory=list)
    memory_policy_id: str = "default"
    max_turns: int = 1
    temperature: float = 0.0
    scope: list[RuntimeScope] = Field(default_factory=lambda: ["top"])
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_agent(self) -> "AgentSpec":
        if not self.agent_id.strip():
            raise ValueError("agent_id may not be empty")
        if self.max_turns < 1:
            raise ValueError("agent max_turns must be >= 1")
        return self


class GraphNodeSpec(RuntimeSpecModel):
    node_id: str
    node_type: Literal[
        "agent",
        "router",
        "tool",
        "memory",
        "merge",
        "verify",
        "direct_response",
        "builtin",
        "service_action",
        "repo_patch",
    ]
    agent_id: Optional[str] = None
    tool_id: Optional[str] = None
    description: str = ""
    input_keys: list[str] = Field(default_factory=list)
    output_key: str = ""
    static_args: dict[str, Any] = Field(default_factory=dict)
    condition: str = "always"
    externally_visible: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_node(self) -> "GraphNodeSpec":
        if not self.node_id.strip():
            raise ValueError("graph node_id may not be empty")
        if self.node_type == "agent" and not self.agent_id:
            raise ValueError(f"agent node {self.node_id!r} requires agent_id")
        if self.node_type == "tool" and not self.tool_id:
            raise ValueError(f"tool node {self.node_id!r} requires tool_id")
        return self


class GraphEdgeSpec(RuntimeSpecModel):
    source: str
    target: str
    condition: str = "always"
    priority: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class GraphSpec(RuntimeSpecModel):
    graph_id: str = "main"
    entry_node: str
    terminal_nodes: list[str] = Field(default_factory=list)
    nodes: list[GraphNodeSpec] = Field(default_factory=list)
    edges: list[GraphEdgeSpec] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_graph(self) -> "GraphSpec":
        node_ids = [node.node_id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("graph node_id values must be unique")
        node_set = set(node_ids)
        if self.entry_node not in node_set:
            raise ValueError(f"entry_node {self.entry_node!r} is not present in graph nodes")
        missing_terminals = sorted(set(self.terminal_nodes) - node_set)
        if missing_terminals:
            raise ValueError(f"terminal_nodes missing from graph: {missing_terminals}")
        for edge in self.edges:
            if edge.source not in node_set:
                raise ValueError(f"edge source {edge.source!r} is not present in graph nodes")
            if edge.target not in node_set:
                raise ValueError(f"edge target {edge.target!r} is not present in graph nodes")
        return self


class ToolSpec(RuntimeSpecModel):
    tool_id: str
    name: str
    family: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    runtime_visible: bool = True
    side_effect_kind: Literal["none", "filesystem_write", "service_action", "repo_patch"] = "none"
    authority_boundary: Literal["runtime", "host", "sealed_validator"] = "runtime"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModelPolicy(RuntimeSpecModel):
    model_policy_id: str
    provider_name: str = "runtime_provider"
    model_class: str = "small"
    temperature: float = 0.0
    max_output_tokens: int = 1024
    fallback_model_policy_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryPolicy(RuntimeSpecModel):
    memory_policy_id: str = "default"
    mode: Literal["none", "episodic", "vector", "graph"] = "none"
    read_scopes: list[str] = Field(default_factory=list)
    write_scopes: list[str] = Field(default_factory=list)
    retention_policy: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExecutionPolicy(RuntimeSpecModel):
    max_steps: int = 16
    max_model_calls: int = 8
    max_tool_calls: int = 8
    max_latency_s: float = 120.0
    max_cost: float = 10.0
    allow_branching: bool = True
    allow_parallel: bool = False
    fail_closed_on_unknown_node: bool = True
    side_effect_policy: Literal["disallow", "receipt_required", "host_gated"] = "receipt_required"
    metadata: dict[str, Any] = Field(default_factory=dict)


class TracingPolicy(RuntimeSpecModel):
    trace_runtime_state: bool = True
    trace_graph_events: bool = True
    trace_model_calls: bool = True
    trace_tool_calls: bool = True
    redact_keys: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MutationActionRef(RuntimeSpecModel):
    action_id: str
    action_type: str
    parent_spec_digest: str = ""
    child_spec_digest: str = ""
    created_at: float = Field(default_factory=now_ts)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RuntimeSpec(RuntimeSpecModel):
    runtime_id: str
    runtime_kind: RuntimeKind = "langgraph_spec"
    name: str
    description: str = ""
    agents: list[AgentSpec] = Field(default_factory=list)
    graph: GraphSpec
    tools: list[ToolSpec] = Field(default_factory=list)
    models: list[ModelPolicy] = Field(default_factory=lambda: [ModelPolicy(model_policy_id="default")])
    memory: MemoryPolicy = Field(default_factory=MemoryPolicy)
    execution: ExecutionPolicy = Field(default_factory=ExecutionPolicy)
    tracing: TracingPolicy = Field(default_factory=TracingPolicy)
    mutation_history: list[MutationActionRef] = Field(default_factory=list)
    parent_spec_digest: str | None = None
    created_at: float = Field(default_factory=now_ts)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_runtime_spec(self) -> "RuntimeSpec":
        agent_ids = [agent.agent_id for agent in self.agents]
        if len(agent_ids) != len(set(agent_ids)):
            raise ValueError("agent_id values must be unique")
        tool_ids = [tool.tool_id for tool in self.tools]
        if len(tool_ids) != len(set(tool_ids)):
            raise ValueError("tool_id values must be unique")
        model_ids = [model.model_policy_id for model in self.models]
        if len(model_ids) != len(set(model_ids)):
            raise ValueError("model_policy_id values must be unique")
        agent_id_set = set(agent_ids)
        tool_id_set = set(tool_ids)
        model_id_set = set(model_ids)
        assert_no_private_or_sealed_keys(self.model_dump(mode="json", exclude_none=True))
        sealed_tools = sorted(tool.tool_id for tool in self.tools if tool.authority_boundary == "sealed_validator")
        if sealed_tools:
            raise ValueError(f"RuntimeSpec cannot expose sealed validator tools: {sealed_tools}")
        for agent in self.agents:
            if agent.model_policy_id not in model_id_set:
                raise ValueError(f"agent {agent.agent_id!r} references missing model_policy_id {agent.model_policy_id!r}")
            missing_tools = sorted(set(agent.tool_ids) - tool_id_set)
            if missing_tools:
                raise ValueError(f"agent {agent.agent_id!r} references missing tools {missing_tools}")
        for node in self.graph.nodes:
            if node.agent_id and node.agent_id not in agent_id_set:
                raise ValueError(f"graph node {node.node_id!r} references missing agent_id {node.agent_id!r}")
            if node.tool_id and node.tool_id not in tool_id_set:
                raise ValueError(f"graph node {node.node_id!r} references missing tool_id {node.tool_id!r}")
        return self

    @property
    def spec_digest(self) -> str:
        return runtime_spec_digest(self)

    def public_summary(self) -> dict[str, Any]:
        return {
            "runtime_id": self.runtime_id,
            "runtime_kind": self.runtime_kind,
            "name": self.name,
            "description": self.description,
            "spec_digest": self.spec_digest,
            "agents": [
                {
                    "agent_id": agent.agent_id,
                    "role": agent.role,
                    "description": agent.description,
                    "model_policy_id": agent.model_policy_id,
                    "tool_ids": list(agent.tool_ids),
                    "scope": list(agent.scope),
                }
                for agent in self.agents
            ],
            "graph": {
                "graph_id": self.graph.graph_id,
                "entry_node": self.graph.entry_node,
                "terminal_nodes": list(self.graph.terminal_nodes),
                "nodes": [
                    {
                        "node_id": node.node_id,
                        "node_type": node.node_type,
                        "agent_id": node.agent_id,
                        "tool_id": node.tool_id,
                        "output_key": node.output_key,
                    }
                    for node in self.graph.nodes
                    if node.externally_visible
                ],
                "edges": [edge.model_dump(mode="json") for edge in self.graph.edges],
            },
            "tools": [tool.model_dump(mode="json") for tool in self.tools if tool.runtime_visible],
            "execution": self.execution.model_dump(mode="json"),
        }


class TradingAgentsRuntimeSpec(RuntimeSpec):
    runtime_kind: Literal["tradingagents_langgraph"] = "tradingagents_langgraph"
    selected_analysts: list[str] = Field(default_factory=lambda: ["market", "news", "fundamentals"])
    deep_think_model: str = "runtime_provider/deep"
    quick_think_model: str = "runtime_provider/quick"
    debate_rounds: int = 1
    risk_discussion_rounds: int = 1
    data_vendor_policy: dict[str, Any] = Field(default_factory=dict)
    action_mapping_policy_id: str = "bounded_order_intent.v1"
    risk_policy_id: str = "default_risk.v1"


def runtime_spec_model_for_kind(runtime_kind: str) -> type[RuntimeSpec]:
    normalized = str(runtime_kind or "langgraph_spec").strip()
    if normalized == "tradingagents_langgraph":
        return TradingAgentsRuntimeSpec
    return RuntimeSpec


def validate_runtime_spec_payload(spec: RuntimeSpec | dict[str, Any]) -> RuntimeSpec:
    payload = spec.model_dump(mode="json", exclude_none=True) if isinstance(spec, RuntimeSpec) else dict(spec)
    model = runtime_spec_model_for_kind(str(payload.get("runtime_kind") or "langgraph_spec"))
    return model.model_validate(payload)


def runtime_spec_payload(spec: RuntimeSpec | dict[str, Any]) -> dict[str, Any]:
    normalized = validate_runtime_spec_payload(spec)
    return _digest_payload(normalized.model_dump(mode="json", exclude_none=True))


def runtime_spec_digest(spec: RuntimeSpec | dict[str, Any]) -> str:
    return stable_hash("agintor.runtime_spec", runtime_spec_payload(spec))


def baseline_langgraph_runtime_spec(
    *,
    runtime_id: str,
    name: str = "Baseline LangGraph Runtime",
) -> RuntimeSpec:
    return RuntimeSpec(
        runtime_id=runtime_id,
        runtime_kind="langgraph_spec",
        name=name,
        description="Baseline spec-backed runtime; one direct-response agent terminating immediately.",
        agents=[
            AgentSpec(
                agent_id="agent.default",
                role="worker",
                prompt=PromptSpec(task_template="{prompt}"),
                model_policy_id="default",
                tool_ids=[],
                scope=["top"],
            )
        ],
        graph=GraphSpec(
            graph_id="runtime_graph",
            entry_node="node.default",
            terminal_nodes=["node.terminal"],
            nodes=[
                GraphNodeSpec(
                    node_id="node.default",
                    node_type="direct_response",
                    agent_id="agent.default",
                    output_key="answer",
                ),
                GraphNodeSpec(
                    node_id="node.terminal",
                    node_type="verify",
                    input_keys=["answer"],
                )
            ],
            edges=[GraphEdgeSpec(source="node.default", target="node.terminal")],
        ),
        tools=[],
        models=[ModelPolicy(model_policy_id="default", provider_name="runtime_default", model_class="small")],
        memory=MemoryPolicy(memory_policy_id="default", mode="none"),
        execution=ExecutionPolicy(max_steps=32, side_effect_policy="receipt_required"),
        tracing=TracingPolicy(),
        mutation_history=[],
        metadata={"template": "baseline_runtime_langgraph"},
    )


__all__ = [
    "AgentSpec",
    "ExecutionPolicy",
    "GraphEdgeSpec",
    "GraphNodeSpec",
    "GraphSpec",
    "MemoryPolicy",
    "ModelPolicy",
    "MutationActionRef",
    "PromptSpec",
    "RuntimeKind",
    "RuntimeScope",
    "RuntimeSpec",
    "RuntimeSpecModel",
    "ToolSpec",
    "TracingPolicy",
    "TradingAgentsRuntimeSpec",
    "assert_no_private_or_sealed_keys",
    "baseline_langgraph_runtime_spec",
    "runtime_spec_digest",
    "runtime_spec_model_for_kind",
    "runtime_spec_payload",
    "validate_runtime_spec_payload",
]
