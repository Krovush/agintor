from __future__ import annotations

from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..utils import now_ts, stable_hash

RuntimeKind = Literal["langgraph_spec_v2", "tradingagents_langgraph_v1"]
ScopeName = Literal["top", "mem", "tool", "ctl"]

_PRIVATE_KEY_PREFIXES = ("private_", "sealed_", "hidden_", "oracle_private_")
_PRIVATE_KEY_NAMES = {
    "private_expected",
    "private_answer",
    "private_answer_ref",
    "expected_digest",
    "sealed_fixture",
    "sealed_fixtures",
    "hidden_tests",
    "promotion_thresholds",
    "private_rubric",
}


def _canonical(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _canonical(value.model_dump(mode="json", exclude_none=True))
    if isinstance(value, Mapping):
        return {str(key): _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def _contains_private_keys(value: Any, *, path: str = "") -> list[str]:
    issues: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}" if path else key_text
            normalized = key_text.strip().lower()
            if normalized in _PRIVATE_KEY_NAMES or any(normalized.startswith(prefix) for prefix in _PRIVATE_KEY_PREFIXES):
                issues.append(child_path)
            issues.extend(_contains_private_keys(item, path=child_path))
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            issues.extend(_contains_private_keys(item, path=f"{path}[{idx}]"))
    return issues


class RuntimeSpecModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class MutationActionRef(RuntimeSpecModel):
    action_id: str
    action_type: str
    action_digest: str = ""
    parent_spec_digest: str = ""
    child_spec_digest: str = ""
    created_at: float = Field(default_factory=now_ts)


class AgentSpec(RuntimeSpecModel):
    agent_id: str
    name: str
    role: str = "worker"
    description: str = ""
    prompt: str = ""
    model_policy_id: str = "default"
    tool_policy_ids: list[str] = Field(default_factory=list)
    memory_policy_id: str = "default"
    handoff_targets: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_no_private_agent_fields(self) -> "AgentSpec":
        issues = _contains_private_keys(self.metadata, path=f"agent:{self.agent_id}.metadata")
        if issues:
            raise ValueError(f"runtime agents may not contain private oracle fields: {issues}")
        return self


class GraphNodeSpec(RuntimeSpecModel):
    node_id: str
    agent_id: str | None = None
    node_kind: Literal["agent", "tool", "router", "merge", "verify", "terminal"] = "agent"
    label: str = ""
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class GraphEdgeSpec(RuntimeSpecModel):
    edge_id: str
    source: str
    target: str
    condition: str = "always"
    priority: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class GraphSpec(RuntimeSpecModel):
    graph_id: str = "runtime_graph"
    entry_node_id: str
    terminal_node_ids: list[str] = Field(default_factory=list)
    nodes: list[GraphNodeSpec] = Field(default_factory=list)
    edges: list[GraphEdgeSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_graph_integrity(self) -> "GraphSpec":
        node_ids = [node.node_id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("runtime graph node_id values must be unique")
        if self.entry_node_id not in set(node_ids):
            raise ValueError(f"entry node {self.entry_node_id!r} is not present in graph nodes")
        missing_terminal = [node_id for node_id in self.terminal_node_ids if node_id not in set(node_ids)]
        if missing_terminal:
            raise ValueError(f"terminal nodes are not present in graph nodes: {missing_terminal}")
        edge_ids = [edge.edge_id for edge in self.edges]
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("runtime graph edge_id values must be unique")
        missing_edge_refs = [
            edge.edge_id
            for edge in self.edges
            if edge.source not in set(node_ids) or edge.target not in set(node_ids)
        ]
        if missing_edge_refs:
            raise ValueError(f"runtime graph edges reference missing nodes: {missing_edge_refs}")
        return self


class ToolSpec(RuntimeSpecModel):
    tool_id: str
    name: str
    category: str
    description: str = ""
    binding: dict[str, Any] = Field(default_factory=dict)
    allowed_scopes: list[str] = Field(default_factory=list)
    side_effect_kind: Literal["none", "filesystem_write", "service_action", "provider_request", "tool_launch"] = "none"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModelPolicy(RuntimeSpecModel):
    model_policy_id: str
    provider_name: str = "runtime_default"
    model_class: str = "small"
    temperature: float = 0.0
    max_output_tokens: int = 2048
    fallback_policy_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryPolicy(RuntimeSpecModel):
    memory_policy_id: str = "default"
    memory_kind: Literal["none", "short_term", "episodic", "vector", "hybrid"] = "short_term"
    max_items: int = 128
    retention: dict[str, Any] = Field(default_factory=dict)
    retrieval: dict[str, Any] = Field(default_factory=dict)


class ExecutionPolicy(RuntimeSpecModel):
    max_steps: int = 32
    max_parallel_branches: int = 4
    timeout_s: float = 120.0
    cost_budget: float = 100.0
    allow_checkpointing: bool = True
    allow_resume: bool = True
    side_effect_policy: Literal["deny", "receipt_required", "consent_required"] = "receipt_required"
    required_runtime_guarantees: list[str] = Field(default_factory=list)


class TracingPolicy(RuntimeSpecModel):
    trace_level: Literal["none", "summary", "full"] = "full"
    redact_inputs: bool = False
    redact_outputs: bool = False
    emit_node_events: bool = True
    emit_side_effect_receipts: bool = True


class RuntimeSpec(RuntimeSpecModel):
    schema_version: Literal["agintor.runtime_spec.v2"] = "agintor.runtime_spec.v2"
    runtime_id: str
    runtime_kind: RuntimeKind = "langgraph_spec_v2"
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
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_runtime_spec(self) -> "RuntimeSpec":
        agent_ids = [agent.agent_id for agent in self.agents]
        if len(agent_ids) != len(set(agent_ids)):
            raise ValueError("runtime spec agent_id values must be unique")
        node_agent_ids = {node.agent_id for node in self.graph.nodes if node.agent_id}
        missing_agents = sorted(node_agent_ids - set(agent_ids))
        if missing_agents:
            raise ValueError(f"graph nodes reference missing agents: {missing_agents}")
        tool_ids = [tool.tool_id for tool in self.tools]
        if len(tool_ids) != len(set(tool_ids)):
            raise ValueError("runtime spec tool_id values must be unique")
        model_ids = [model.model_policy_id for model in self.models]
        if len(model_ids) != len(set(model_ids)):
            raise ValueError("runtime spec model_policy_id values must be unique")
        missing_model_ids = sorted({agent.model_policy_id for agent in self.agents} - set(model_ids))
        if missing_model_ids:
            raise ValueError(f"agents reference missing model policies: {missing_model_ids}")
        issues = _contains_private_keys(self.model_dump(mode="json", exclude_none=True))
        if issues:
            raise ValueError(f"RuntimeSpec may not contain private oracle fields: {issues}")
        return self

    @property
    def spec_digest(self) -> str:
        return runtime_spec_digest(self)

    def canonical_payload(self) -> dict[str, Any]:
        return canonical_runtime_spec_payload(self)


class RuntimeSpecDiff(RuntimeSpecModel):
    parent_spec_digest: str
    child_spec_digest: str
    changed_paths: list[str] = Field(default_factory=list)
    action_ids: list[str] = Field(default_factory=list)
    summary: str = ""


def canonical_runtime_spec_payload(spec: RuntimeSpec | Mapping[str, Any]) -> dict[str, Any]:
    payload = spec.model_dump(mode="json", exclude_none=True) if isinstance(spec, RuntimeSpec) else dict(spec)
    payload.pop("spec_digest", None)
    return _canonical(payload)


def runtime_spec_digest(spec: RuntimeSpec | Mapping[str, Any]) -> str:
    return stable_hash(canonical_runtime_spec_payload(spec))


def runtime_spec_public_projection(spec: RuntimeSpec) -> dict[str, Any]:
    payload = spec.model_dump(mode="json", exclude_none=True)
    payload.pop("mutation_history", None)
    payload.pop("parent_spec_digest", None)
    return _canonical(payload)


def default_langgraph_runtime_spec(*, runtime_id: str, name: str, description: str = "") -> RuntimeSpec:
    agent = AgentSpec(
        agent_id="agent.default",
        name="Default Agent",
        role="worker",
        description="Default deterministic solve agent",
        prompt="Solve the request using the provided execution plan and allowed tools.",
        model_policy_id="default",
    )
    graph = GraphSpec(
        entry_node_id="node.default",
        terminal_node_ids=["node.terminal"],
        nodes=[
            GraphNodeSpec(node_id="node.default", agent_id=agent.agent_id, node_kind="agent", label="default"),
            GraphNodeSpec(node_id="node.terminal", node_kind="terminal", label="terminal"),
        ],
        edges=[GraphEdgeSpec(edge_id="edge.default.terminal", source="node.default", target="node.terminal")],
    )
    return RuntimeSpec(
        runtime_id=runtime_id,
        name=name,
        description=description,
        agents=[agent],
        graph=graph,
        tools=[],
        metadata={"created_by": "default_langgraph_runtime_spec"},
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
    "RuntimeKind",
    "RuntimeSpec",
    "RuntimeSpecDiff",
    "ScopeName",
    "ToolSpec",
    "TracingPolicy",
    "canonical_runtime_spec_payload",
    "default_langgraph_runtime_spec",
    "runtime_spec_digest",
    "runtime_spec_public_projection",
]
