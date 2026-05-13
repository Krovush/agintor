from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..utils import ensure_directory, now_ts, stable_hash
from .runtime_spec import (
    AgentSpec,
    ExecutionPolicy,
    GraphEdgeSpec,
    GraphNodeSpec,
    MemoryPolicy,
    ModelPolicy,
    MutationActionRef,
    RuntimeScope,
    RuntimeSpec,
    ToolSpec,
    runtime_spec_digest,
)


SpecActionType = Literal[
    "add_agent",
    "remove_agent",
    "update_agent",
    "add_node",
    "remove_node",
    "update_node",
    "set_edge",
    "remove_edge",
    "add_tool",
    "remove_tool",
    "set_tool_policy",
    "set_model_policy",
    "set_memory_policy",
    "set_budget_policy",
    "set_routing_policy",
    "set_prompt",
]


class SpecActionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class SpecAction(SpecActionModel):
    action_id: str
    action_type: SpecActionType
    target_ids: list[str] = Field(default_factory=list)
    scope: list[RuntimeScope] = Field(default_factory=list)
    rationale: str = ""
    expected_effect: str = ""
    patch: dict[str, Any] = Field(default_factory=dict)
    created_at: float = Field(default_factory=now_ts)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_action(self) -> "SpecAction":
        if not self.action_id.strip():
            raise ValueError("SpecAction requires action_id")
        if not self.scope:
            raise ValueError("SpecAction requires at least one scope")
        rendered_patch = json.dumps(self.patch, sort_keys=True, default=str).lower()
        forbidden = ["private_", "sealed", "oracle_secret", "hidden_test", "promotion_threshold"]
        if any(marker in rendered_patch for marker in forbidden):
            raise ValueError("SpecAction patch may not introduce private/sealed oracle material")
        return self


class SpecActionResult(SpecActionModel):
    action_id: str
    parent_spec_digest: str
    child_spec_digest: str
    applied: bool
    changed: bool = False
    reason: str = ""
    resulting_runtime_id: str = ""
    created_at: float = Field(default_factory=now_ts)


class SpecMutationLedgerEntry(SpecActionModel):
    action: SpecAction
    result: SpecActionResult
    parent_runtime_hash: str = ""
    child_runtime_hash: str = ""
    oracle_package_hash: str = ""
    evidence_digest: str = ""
    created_at: float = Field(default_factory=now_ts)


class SpecActionValidationError(ValueError):
    pass


def _replace_by_id(items: list[Any], attr: str, value: Any) -> tuple[list[Any], bool]:
    target_id = getattr(value, attr)
    replaced = False
    updated: list[Any] = []
    for item in items:
        if getattr(item, attr) == target_id:
            updated.append(value)
            replaced = True
        else:
            updated.append(item)
    if not replaced:
        updated.append(value)
    return updated, replaced


def _remove_by_id(items: list[Any], attr: str, target_id: str) -> tuple[list[Any], bool]:
    updated = [item for item in items if getattr(item, attr) != target_id]
    return updated, len(updated) != len(items)


def _node_ids(spec: RuntimeSpec) -> set[str]:
    return {node.node_id for node in spec.graph.nodes}


def validate_spec_action(parent: RuntimeSpec, action: SpecAction) -> None:
    action = SpecAction.model_validate(action)
    agent_ids = {agent.agent_id for agent in parent.agents}
    tool_ids = {tool.tool_id for tool in parent.tools}
    node_ids = _node_ids(parent)
    if action.action_type in {"remove_agent", "update_agent", "set_prompt"}:
        missing = sorted(set(action.target_ids) - agent_ids)
        if missing:
            raise SpecActionValidationError(f"action references missing agents {missing}")
    if action.action_type in {"remove_tool", "set_tool_policy"}:
        missing = sorted(set(action.target_ids) - tool_ids)
        if missing:
            raise SpecActionValidationError(f"action references missing tools {missing}")
    if action.action_type in {"remove_node", "update_node", "set_routing_policy"}:
        missing = sorted(set(action.target_ids) - node_ids)
        if missing:
            raise SpecActionValidationError(f"action references missing graph nodes {missing}")
    if action.action_type == "remove_agent":
        referenced = sorted(
            node.node_id
            for node in parent.graph.nodes
            if node.agent_id in set(action.target_ids)
        )
        if referenced:
            raise SpecActionValidationError(f"cannot remove agents still referenced by graph nodes {referenced}")
    if action.action_type == "remove_tool":
        referenced_nodes = sorted(node.node_id for node in parent.graph.nodes if node.tool_id in set(action.target_ids))
        referenced_agents = sorted(agent.agent_id for agent in parent.agents if set(agent.tool_ids) & set(action.target_ids))
        if referenced_nodes or referenced_agents:
            raise SpecActionValidationError(
                f"cannot remove tools still referenced by graph nodes {referenced_nodes} or agents {referenced_agents}"
            )


def apply_spec_action(parent: RuntimeSpec, action: SpecAction) -> tuple[RuntimeSpec, SpecActionResult]:
    parent = RuntimeSpec.model_validate(parent)
    action = SpecAction.model_validate(action)
    validate_spec_action(parent, action)
    parent_digest = runtime_spec_digest(parent)
    payload = parent.model_dump(mode="json", exclude_none=True)
    changed = False

    if action.action_type == "add_agent":
        agent = AgentSpec.model_validate(action.patch.get("agent", action.patch))
        payload["agents"], replaced = _replace_by_id(list(parent.agents), "agent_id", agent)
        payload["agents"] = [item.model_dump(mode="json") for item in payload["agents"]]
        changed = True
        if replaced:
            action.metadata.setdefault("replaced_existing", True)
    elif action.action_type == "update_agent":
        target_id = action.target_ids[0]
        agents = []
        for agent in parent.agents:
            if agent.agent_id == target_id:
                agents.append(agent.model_copy(update=dict(action.patch), deep=True))
                changed = True
            else:
                agents.append(agent)
        payload["agents"] = [agent.model_dump(mode="json") for agent in agents]
    elif action.action_type == "remove_agent":
        for target_id in action.target_ids:
            agents, removed = _remove_by_id(list(RuntimeSpec.model_validate(payload).agents), "agent_id", target_id)
            payload["agents"] = [agent.model_dump(mode="json") for agent in agents]
            changed = changed or removed
    elif action.action_type == "add_node":
        node = GraphNodeSpec.model_validate(action.patch.get("node", action.patch))
        nodes, _ = _replace_by_id(list(parent.graph.nodes), "node_id", node)
        graph = parent.graph.model_copy(update={"nodes": nodes}, deep=True)
        payload["graph"] = graph.model_dump(mode="json")
        changed = True
    elif action.action_type == "update_node":
        target_id = action.target_ids[0]
        nodes = []
        for node in parent.graph.nodes:
            if node.node_id == target_id:
                nodes.append(node.model_copy(update=dict(action.patch), deep=True))
                changed = True
            else:
                nodes.append(node)
        graph = parent.graph.model_copy(update={"nodes": nodes}, deep=True)
        payload["graph"] = graph.model_dump(mode="json")
    elif action.action_type == "remove_node":
        remove_ids = set(action.target_ids)
        nodes = [node for node in parent.graph.nodes if node.node_id not in remove_ids]
        edges = [edge for edge in parent.graph.edges if edge.source not in remove_ids and edge.target not in remove_ids]
        terminals = [node_id for node_id in parent.graph.terminal_nodes if node_id not in remove_ids]
        graph = parent.graph.model_copy(update={"nodes": nodes, "edges": edges, "terminal_nodes": terminals}, deep=True)
        payload["graph"] = graph.model_dump(mode="json")
        changed = len(nodes) != len(parent.graph.nodes)
    elif action.action_type == "set_edge":
        edge = GraphEdgeSpec.model_validate(action.patch.get("edge", action.patch))
        edges = [e for e in parent.graph.edges if not (e.source == edge.source and e.target == edge.target)]
        edges.append(edge)
        graph = parent.graph.model_copy(update={"edges": edges}, deep=True)
        payload["graph"] = graph.model_dump(mode="json")
        changed = True
    elif action.action_type == "remove_edge":
        source = str(action.patch.get("source", action.target_ids[0] if action.target_ids else ""))
        target = str(action.patch.get("target", action.target_ids[1] if len(action.target_ids) > 1 else ""))
        edges = [edge for edge in parent.graph.edges if not (edge.source == source and edge.target == target)]
        graph = parent.graph.model_copy(update={"edges": edges}, deep=True)
        payload["graph"] = graph.model_dump(mode="json")
        changed = len(edges) != len(parent.graph.edges)
    elif action.action_type == "add_tool":
        tool = ToolSpec.model_validate(action.patch.get("tool", action.patch))
        payload["tools"], _ = _replace_by_id(list(parent.tools), "tool_id", tool)
        payload["tools"] = [item.model_dump(mode="json") for item in payload["tools"]]
        changed = True
    elif action.action_type == "remove_tool":
        tools = list(parent.tools)
        for target_id in action.target_ids:
            tools, removed = _remove_by_id(tools, "tool_id", target_id)
            changed = changed or removed
        payload["tools"] = [tool.model_dump(mode="json") for tool in tools]
    elif action.action_type == "set_tool_policy":
        target_id = action.target_ids[0]
        tools = []
        for tool in parent.tools:
            if tool.tool_id == target_id:
                tools.append(tool.model_copy(update=dict(action.patch), deep=True))
                changed = True
            else:
                tools.append(tool)
        payload["tools"] = [tool.model_dump(mode="json") for tool in tools]
    elif action.action_type == "set_model_policy":
        model = ModelPolicy.model_validate(action.patch.get("model", action.patch))
        models, _ = _replace_by_id(list(parent.models), "model_policy_id", model)
        payload["models"] = [item.model_dump(mode="json") for item in models]
        changed = True
    elif action.action_type == "set_memory_policy":
        payload["memory"] = MemoryPolicy.model_validate(action.patch.get("memory", action.patch)).model_dump(mode="json")
        changed = True
    elif action.action_type == "set_budget_policy":
        payload["execution"] = parent.execution.model_copy(update=dict(action.patch), deep=True).model_dump(mode="json")
        changed = True
    elif action.action_type == "set_routing_policy":
        graph = parent.graph.model_copy(update={"metadata": {**dict(parent.graph.metadata), **dict(action.patch)}}, deep=True)
        payload["graph"] = graph.model_dump(mode="json")
        changed = True
    elif action.action_type == "set_prompt":
        target_id = action.target_ids[0]
        agents = []
        for agent in parent.agents:
            if agent.agent_id == target_id:
                prompt = agent.prompt.model_copy(update=dict(action.patch), deep=True)
                agents.append(agent.model_copy(update={"prompt": prompt}, deep=True))
                changed = True
            else:
                agents.append(agent)
        payload["agents"] = [agent.model_dump(mode="json") for agent in agents]
    else:
        raise SpecActionValidationError(f"unsupported action_type {action.action_type!r}")

    payload["parent_spec_digest"] = parent_digest
    payload["mutation_history"] = list(payload.get("mutation_history", []))
    child = RuntimeSpec.model_validate(payload)
    child_digest = runtime_spec_digest(child)
    action_ref = MutationActionRef(
        action_id=action.action_id,
        action_type=action.action_type,
        parent_spec_digest=parent_digest,
        child_spec_digest=child_digest,
        metadata={"scope": list(action.scope), "expected_effect": action.expected_effect},
    )
    child = child.model_copy(update={"mutation_history": [*child.mutation_history, action_ref]}, deep=True)
    child_digest = runtime_spec_digest(child)
    return child, SpecActionResult(
        action_id=action.action_id,
        parent_spec_digest=parent_digest,
        child_spec_digest=child_digest,
        applied=True,
        changed=changed,
        resulting_runtime_id=child.runtime_id,
        reason="applied",
    )


def apply_spec_actions(parent: RuntimeSpec, actions: Sequence[SpecAction]) -> tuple[RuntimeSpec, list[SpecActionResult]]:
    current = RuntimeSpec.model_validate(parent)
    results: list[SpecActionResult] = []
    for action in actions:
        current, result = apply_spec_action(current, SpecAction.model_validate(action))
        results.append(result)
    return current, results


def write_mutation_ledger(path: Path, entries: Sequence[SpecMutationLedgerEntry]) -> None:
    ensure_directory(path.parent)
    with path.open("a", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry.model_dump(mode="json", exclude_none=True), sort_keys=True, separators=(",", ":")) + "\n")


def action_id_for(action_type: str, patch: dict[str, Any], *, seed: Any = "") -> str:
    return f"spec-action.{stable_hash(action_type, patch, seed)[:16]}"


__all__ = [
    "SpecAction",
    "SpecActionResult",
    "SpecActionType",
    "SpecActionValidationError",
    "SpecMutationLedgerEntry",
    "action_id_for",
    "apply_spec_action",
    "apply_spec_actions",
    "validate_spec_action",
    "write_mutation_ledger",
]
