from __future__ import annotations
from pathlib import Path
import textwrap, json
ROOT = Path('/mnt/data/agintor_full_plan_patch/new_files')
files: dict[str, str] = {}

def add(path: str, content: str) -> None:
    files[path] = textwrap.dedent(content).lstrip()

add('agintor/contracts/runtime_spec.py', r'''
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
''')

add('agintor/contracts/spec_actions.py', r'''
from __future__ import annotations

import copy
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..utils import now_ts, stable_hash
from .runtime_spec import (
    AgentSpec,
    GraphEdgeSpec,
    MemoryPolicy,
    ModelPolicy,
    MutationActionRef,
    RuntimeSpec,
    ScopeName,
    ToolSpec,
    runtime_spec_digest,
)

ActionType = Literal[
    "add_agent",
    "remove_agent",
    "update_agent",
    "set_edge",
    "remove_edge",
    "set_tool_policy",
    "set_model_policy",
    "set_memory_policy",
    "set_budget_policy",
    "set_routing_policy",
    "set_prompt",
]


class SpecActionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class SpecAction(SpecActionModel):
    action_id: str
    action_type: ActionType
    target_ids: list[str] = Field(default_factory=list)
    scope: list[ScopeName] = Field(default_factory=list)
    rationale: str = ""
    expected_effect: str = ""
    patch: dict[str, Any] = Field(default_factory=dict)
    created_at: float = Field(default_factory=now_ts)

    @model_validator(mode="after")
    def validate_action_shape(self) -> "SpecAction":
        if not self.action_id.strip():
            raise ValueError("SpecAction.action_id may not be empty")
        if not self.scope:
            raise ValueError("SpecAction.scope must identify at least one runtime scope")
        if self.action_type not in {"add_agent", "set_memory_policy", "set_budget_policy"} and not self.target_ids:
            raise ValueError(f"{self.action_type} requires target_ids")
        return self

    @property
    def action_digest(self) -> str:
        return stable_hash(self.model_dump(mode="json", exclude_none=True))


class SpecActionBatch(SpecActionModel):
    batch_id: str
    parent_spec_digest: str = ""
    actions: list[SpecAction] = Field(default_factory=list)
    created_at: float = Field(default_factory=now_ts)

    @property
    def batch_digest(self) -> str:
        return stable_hash(self.model_dump(mode="json", exclude_none=True))


class SpecActionApplication(SpecActionModel):
    parent_spec_digest: str
    child_spec_digest: str
    actions: list[SpecAction]
    mutation_refs: list[MutationActionRef]
    changed: bool


def _merge_mapping(base: Mapping[str, Any], updates: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in updates.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _merge_mapping(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _replace_by_id(items: Sequence[Any], attr_name: str, item_id: str, updates: Mapping[str, Any]) -> list[Any]:
    replaced: list[Any] = []
    found = False
    for item in items:
        raw = item.model_dump(mode="json", exclude_none=True) if hasattr(item, "model_dump") else dict(item)
        if str(raw.get(attr_name, "")) == str(item_id):
            raw = _merge_mapping(raw, updates)
            found = True
        replaced.append(raw)
    if not found:
        raise ValueError(f"target id {item_id!r} not found for {attr_name}")
    return replaced


def _remove_by_id(items: Sequence[Any], attr_name: str, item_id: str) -> list[Any]:
    filtered = []
    found = False
    for item in items:
        raw = item.model_dump(mode="json", exclude_none=True) if hasattr(item, "model_dump") else dict(item)
        if str(raw.get(attr_name, "")) == str(item_id):
            found = True
            continue
        filtered.append(raw)
    if not found:
        raise ValueError(f"target id {item_id!r} not found for {attr_name}")
    return filtered


def _apply_one(spec: RuntimeSpec, action: SpecAction) -> RuntimeSpec:
    payload = spec.model_dump(mode="json", exclude_none=True)
    patch = dict(action.patch or {})
    if action.action_type == "add_agent":
        agent_payload = dict(patch.get("agent") or patch)
        agent = AgentSpec.model_validate(agent_payload)
        payload["agents"] = [*payload.get("agents", []), agent.model_dump(mode="json", exclude_none=True)]
    elif action.action_type == "remove_agent":
        agent_id = action.target_ids[0]
        payload["agents"] = _remove_by_id(payload.get("agents", []), "agent_id", agent_id)
        for node in payload.get("graph", {}).get("nodes", []):
            if node.get("agent_id") == agent_id:
                node["agent_id"] = None
    elif action.action_type == "update_agent":
        agent_id = action.target_ids[0]
        payload["agents"] = _replace_by_id(payload.get("agents", []), "agent_id", agent_id, patch)
    elif action.action_type == "set_prompt":
        agent_id = action.target_ids[0]
        payload["agents"] = _replace_by_id(payload.get("agents", []), "agent_id", agent_id, {"prompt": str(patch.get("prompt", ""))})
    elif action.action_type == "set_edge":
        edge_payload = dict(patch.get("edge") or patch)
        edge = GraphEdgeSpec.model_validate(edge_payload)
        edges = [edge for edge in payload.get("graph", {}).get("edges", []) if edge.get("edge_id") != edge_payload["edge_id"]]
        edges.append(edge.model_dump(mode="json", exclude_none=True))
        payload.setdefault("graph", {})["edges"] = edges
    elif action.action_type == "remove_edge":
        edge_id = action.target_ids[0]
        payload.setdefault("graph", {})["edges"] = _remove_by_id(payload.get("graph", {}).get("edges", []), "edge_id", edge_id)
    elif action.action_type == "set_tool_policy":
        tool_id = action.target_ids[0]
        if any(str(tool.get("tool_id")) == tool_id for tool in payload.get("tools", [])):
            payload["tools"] = _replace_by_id(payload.get("tools", []), "tool_id", tool_id, patch)
        else:
            tool = ToolSpec.model_validate({"tool_id": tool_id, **patch})
            payload["tools"] = [*payload.get("tools", []), tool.model_dump(mode="json", exclude_none=True)]
    elif action.action_type == "set_model_policy":
        policy_id = action.target_ids[0]
        if any(str(model.get("model_policy_id")) == policy_id for model in payload.get("models", [])):
            payload["models"] = _replace_by_id(payload.get("models", []), "model_policy_id", policy_id, patch)
        else:
            model = ModelPolicy.model_validate({"model_policy_id": policy_id, **patch})
            payload["models"] = [*payload.get("models", []), model.model_dump(mode="json", exclude_none=True)]
    elif action.action_type == "set_memory_policy":
        payload["memory"] = _merge_mapping(payload.get("memory", {}), MemoryPolicy.model_validate(patch).model_dump(mode="json", exclude_none=True))
    elif action.action_type == "set_budget_policy":
        payload["execution"] = _merge_mapping(payload.get("execution", {}), patch)
    elif action.action_type == "set_routing_policy":
        payload["graph"] = _merge_mapping(payload.get("graph", {}), patch)
    else:  # pragma: no cover - pydantic protects this
        raise ValueError(f"unsupported action type {action.action_type!r}")
    return RuntimeSpec.model_validate(payload)


def apply_spec_actions(parent: RuntimeSpec, actions: Sequence[SpecAction]) -> SpecActionApplication:
    current = parent
    parent_digest = runtime_spec_digest(parent)
    refs: list[MutationActionRef] = []
    for action in actions:
        before = runtime_spec_digest(current)
        current = _apply_one(current, action)
        after = runtime_spec_digest(current)
        refs.append(
            MutationActionRef(
                action_id=action.action_id,
                action_type=action.action_type,
                action_digest=action.action_digest,
                parent_spec_digest=before,
                child_spec_digest=after,
            )
        )
    child_payload = current.model_dump(mode="json", exclude_none=True)
    child_payload["parent_spec_digest"] = parent_digest
    child_payload["mutation_history"] = [
        *[ref.model_dump(mode="json", exclude_none=True) for ref in parent.mutation_history],
        *[ref.model_dump(mode="json", exclude_none=True) for ref in refs],
    ]
    child = RuntimeSpec.model_validate(child_payload)
    return SpecActionApplication(
        parent_spec_digest=parent_digest,
        child_spec_digest=runtime_spec_digest(child),
        actions=list(actions),
        mutation_refs=refs,
        changed=runtime_spec_digest(child) != parent_digest,
    )


def action_ledger_rows(application: SpecActionApplication) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for action, ref in zip(application.actions, application.mutation_refs):
        rows.append(
            {
                "action": action.model_dump(mode="json", exclude_none=True),
                "mutation_ref": ref.model_dump(mode="json", exclude_none=True),
                "parent_spec_digest": application.parent_spec_digest,
                "child_spec_digest": application.child_spec_digest,
            }
        )
    return rows


__all__ = [
    "ActionType",
    "SpecAction",
    "SpecActionApplication",
    "SpecActionBatch",
    "action_ledger_rows",
    "apply_spec_actions",
]
''')

add('agintor/contracts/oracle.py', r'''
from __future__ import annotations

from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..utils import now_ts, stable_hash
from .evidence import DomainEvidenceContract, EvidenceRef

AuthorityName = Literal["A0", "A1", "A2", "A3", "A4", "A5"]
ValidatorVisibility = Literal["public", "private", "sealed"]


class OracleModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ValidationIntent(OracleModel):
    task_classes: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    user_weights: dict[str, float] = Field(default_factory=dict)
    hard_failures: list[str] = Field(default_factory=list)
    acceptable_tradeoffs: list[str] = Field(default_factory=list)
    authority_floor: AuthorityName | str = "A4"
    unverifiable_residual_policy: Literal["abstain", "human_audit", "diagnostic_only"] = "abstain"


class ClaimSpec(OracleModel):
    claim_id: str
    text: str
    claim_type: Literal[
        "outcome",
        "state",
        "process",
        "safety",
        "factual",
        "semantic",
        "architecture",
        "cost",
    ] = "outcome"
    criticality: Literal["hard", "major", "minor", "diagnostic"] = "major"
    weight: float = 1.0
    minimum_authority: AuthorityName | str = "A4"
    dependencies: list[str] = Field(default_factory=list)
    unverifiable_reason: str = ""


class ClaimGraph(OracleModel):
    graph_id: str
    claims: list[ClaimSpec] = Field(default_factory=list)
    edges: list[dict[str, str]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_claim_graph(self) -> "ClaimGraph":
        claim_ids = [claim.claim_id for claim in self.claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("claim ids must be unique")
        missing = sorted({dep for claim in self.claims for dep in claim.dependencies} - set(claim_ids))
        if missing:
            raise ValueError(f"claims reference missing dependencies: {missing}")
        return self


class ProofObligation(OracleModel):
    obligation_id: str
    claim_ids: list[str]
    description: str
    required_authority: AuthorityName | str = "A4"
    validator_family_hints: list[str] = Field(default_factory=list)
    failure_action: Literal["reject", "abstain", "quarantine", "diagnostic"] = "abstain"


class ValidatorSpec(OracleModel):
    validator_id: str
    family_id: str
    claim_ids: list[str]
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs_schema: dict[str, Any] = Field(default_factory=dict)
    authority_ceiling: AuthorityName | str = "A4"
    visibility: ValidatorVisibility = "sealed"
    independence_group: str = "default"
    leakage_risk: str = "low"
    health_tests: list[str] = Field(default_factory=list)
    failure_action: Literal["reject", "abstain", "quarantine", "diagnostic"] = "abstain"


class OracleTask(OracleModel):
    task_id: str
    public_prompt: str
    public_inputs: dict[str, Any] = Field(default_factory=dict)
    public_fixture_refs: list[EvidenceRef] = Field(default_factory=list)
    sealed_inputs: dict[str, Any] = Field(default_factory=dict)
    sealed_fixture_refs: list[EvidenceRef] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    validator_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class OracleTaskSet(OracleModel):
    task_set_id: str
    partition: Literal["train", "validation", "confirmatory", "heldout", "proxy", "val", "test"] = "train"
    tasks: list[OracleTask] = Field(default_factory=list)
    public: bool = True
    frozen: bool = True


class FixtureBundleRef(OracleModel):
    bundle_id: str
    uri: str = ""
    digest: str = ""
    visibility: ValidatorVisibility = "sealed"
    description: str = ""


class ScoringProjection(OracleModel):
    projection_id: str
    axis_map: dict[str, list[str]] = Field(default_factory=dict)
    weights: dict[str, float] = Field(default_factory=dict)
    promotion_axes: list[str] = Field(default_factory=list)
    efficiency_axes: list[str] = Field(default_factory=list)


class AuthorityPolicy(OracleModel):
    authority_floor: AuthorityName | str = "A4"
    weak_validator_ceiling: AuthorityName | str = "A2"
    require_independent_groups_for_promotion: int = 1
    allow_model_judge_promotion_alone: bool = False
    critical_claim_policy: Literal["all_verified", "weighted", "diagnostic"] = "all_verified"


class LeakagePolicy(OracleModel):
    status_required: bool = True
    forbidden_public_keys: list[str] = Field(default_factory=lambda: [
        "private_expected",
        "private_answer",
        "private_answer_ref",
        "sealed_inputs",
        "sealed_fixture_refs",
        "hidden_tests",
        "promotion_thresholds",
        "private_rubric",
    ])
    sealed_validator_visibility: bool = True
    runtime_visible_projection_required: bool = True


class AbstentionPolicy(OracleModel):
    insufficient_authority_action: Literal["abstain", "human_audit", "diagnostic_only"] = "abstain"
    missing_critical_validator_action: Literal["abstain", "quarantine"] = "abstain"
    invalid_package_action: Literal["quarantine", "abstain"] = "quarantine"
    min_evidence_count: int = 1


class OraclePackage(OracleModel):
    package_id: str
    oracle_family_id: str
    package_hash: str = ""
    goal_id: str
    runtime_spec_digest: str = ""
    validation_intent: ValidationIntent
    claim_graph: ClaimGraph
    proof_obligations: list[ProofObligation] = Field(default_factory=list)
    validator_specs: list[ValidatorSpec] = Field(default_factory=list)
    task_sets: list[OracleTaskSet] = Field(default_factory=list)
    fixture_bundle_refs: list[FixtureBundleRef] = Field(default_factory=list)
    evidence_contract: DomainEvidenceContract
    scoring_projection: ScoringProjection
    authority_policy: AuthorityPolicy = Field(default_factory=AuthorityPolicy)
    leakage_policy: LeakagePolicy = Field(default_factory=LeakagePolicy)
    abstention_policy: AbstentionPolicy = Field(default_factory=AbstentionPolicy)
    qa_report_ref: str = ""
    public_view_hash: str = ""
    sealed_view_hash: str = ""
    frozen: bool = True
    created_at: float = Field(default_factory=now_ts)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_oracle_package(self) -> "OraclePackage":
        claim_ids = {claim.claim_id for claim in self.claim_graph.claims}
        validator_ids = [validator.validator_id for validator in self.validator_specs]
        if len(validator_ids) != len(set(validator_ids)):
            raise ValueError("validator ids must be unique")
        missing_validator_claims = sorted(
            {claim_id for validator in self.validator_specs for claim_id in validator.claim_ids} - claim_ids
        )
        if missing_validator_claims:
            raise ValueError(f"validators reference missing claims: {missing_validator_claims}")
        obligation_claims = sorted(
            {claim_id for obligation in self.proof_obligations for claim_id in obligation.claim_ids} - claim_ids
        )
        if obligation_claims:
            raise ValueError(f"proof obligations reference missing claims: {obligation_claims}")
        return self


class ValidatorResult(OracleModel):
    validator_id: str
    claim_ids: list[str]
    status: Literal["pass", "fail", "error", "abstain"]
    authority_used: AuthorityName | str = "A0"
    health_status: dict[str, Any] = Field(default_factory=dict)
    observations: dict[str, Any] = Field(default_factory=dict)
    evidence_digest: str = ""
    created_at: float = Field(default_factory=now_ts)

    @model_validator(mode="after")
    def fill_digest(self) -> "ValidatorResult":
        if not self.evidence_digest:
            self.evidence_digest = stable_hash(self.model_dump(mode="json", exclude_none=True))
        return self


class ClaimResult(OracleModel):
    claim_id: str
    satisfied: bool | None = None
    posterior_lower: float | None = None
    posterior_upper: float | None = None
    authority_mass: dict[str, float] = Field(default_factory=dict)
    coverage: float = 0.0
    residual_unverified: str = ""
    validator_result_refs: list[str] = Field(default_factory=list)


class OracleEvaluationSummary(OracleModel):
    package_id: str
    package_hash: str
    runtime_hash: str = ""
    runtime_spec_digest: str = ""
    task_ids: list[str] = Field(default_factory=list)
    validator_results: list[ValidatorResult] = Field(default_factory=list)
    claim_results: list[ClaimResult] = Field(default_factory=list)
    evidence_digest: str = ""
    critical_claims_verified: bool = False
    invalid_reason: str = ""

    @model_validator(mode="after")
    def fill_summary_digest(self) -> "OracleEvaluationSummary":
        if not self.evidence_digest:
            self.evidence_digest = stable_hash(self.model_dump(mode="json", exclude_none=True))
        return self


def oracle_package_identity_payload(package: OraclePackage | Mapping[str, Any]) -> dict[str, Any]:
    payload = package.model_dump(mode="json", exclude_none=True) if isinstance(package, OraclePackage) else dict(package)
    payload.pop("package_hash", None)
    payload.pop("public_view_hash", None)
    payload.pop("sealed_view_hash", None)
    return payload


__all__ = [
    "AbstentionPolicy",
    "AuthorityPolicy",
    "ClaimGraph",
    "ClaimResult",
    "ClaimSpec",
    "FixtureBundleRef",
    "LeakagePolicy",
    "OracleEvaluationSummary",
    "OraclePackage",
    "OracleTask",
    "OracleTaskSet",
    "ProofObligation",
    "ScoringProjection",
    "ValidationIntent",
    "ValidatorResult",
    "ValidatorSpec",
    "oracle_package_identity_payload",
]
''')

# write files
for path, content in files.items():
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding='utf-8')
print(f'wrote {len(files)} files')
