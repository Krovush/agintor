from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..utils import now_ts, stable_hash


RuntimeKind = Literal["langgraph_spec_v2", "tradingagents_langgraph_v1"]
RuntimeScope = Literal["top", "mem", "tool", "ctl"]


class RuntimeSpecModel(BaseModel):
    """Base model for the spec-backed runtime genome.

    The runtime genome is JSON, not live LangChain/LangGraph objects and not
    generated Python code. Generated code is a compilation artifact of this
    spec.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=True)


def _stable_id(prefix: str, payload: Any) -> str:
    return f"{prefix}.{stable_hash(payload)[:16]}"


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
    provider: str = "runtime_provider"
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
        private_markers = ["private_", "sealed", "oracle_secret", "hidden_test", "promotion_threshold"]
        rendered = str(self.model_dump(mode="json")).lower()
        if any(marker in rendered for marker in private_markers):
            raise ValueError("RuntimeSpec may not contain private/sealed oracle material")
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
            "schema_version": self.schema_version,
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


def runtime_spec_payload(spec: RuntimeSpec | dict[str, Any]) -> dict[str, Any]:
    normalized = spec if isinstance(spec, RuntimeSpec) else RuntimeSpec.model_validate(spec)
    payload = normalized.model_dump(mode="json", exclude_none=True)
    payload.pop("metadata", None)
    return payload


def runtime_spec_digest(spec: RuntimeSpec | dict[str, Any]) -> str:
    return stable_hash("agintor.runtime_spec.v2", runtime_spec_payload(spec))


def baseline_langgraph_runtime_spec(*, runtime_id: str = "runtime.langgraph.baseline", name: str = "Baseline LangGraph Runtime") -> RuntimeSpec:
    return RuntimeSpec(
        runtime_id=runtime_id,
        runtime_kind="langgraph_spec_v2",
        name=name,
        description="Spec-backed LangGraph/LangChain runtime generated by Agintor.",
        agents=[
            AgentSpec(
                agent_id="root",
                role="coordinator",
                description="Plan and answer bounded tasks.",
                prompt=PromptSpec(
                    system="You are a bounded Agintor runtime agent.",
                    task_template="{prompt}",
                    output_instructions="Return a concise, structured answer.",
                ),
                model_policy_id="default",
                tool_ids=[],
                scope=["top", "ctl"],
            )
        ],
        graph=GraphSpec(
            graph_id="main",
            entry_node="root_agent",
            terminal_nodes=["root_agent"],
            nodes=[
                GraphNodeSpec(
                    node_id="root_agent",
                    node_type="agent",
                    agent_id="root",
                    output_key="answer",
                    description="Root bounded agent node.",
                )
            ],
            edges=[],
        ),
        tools=[],
        models=[ModelPolicy(model_policy_id="default", provider="runtime_provider", model_class="small")],
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
    "baseline_langgraph_runtime_spec",
    "runtime_spec_digest",
    "runtime_spec_payload",
]
