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
