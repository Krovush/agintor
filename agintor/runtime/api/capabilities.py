from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ...core.exceptions import BranchCancelled, HardInvalidation, PromptAdaptationError
from ...tracing import resolve_trace_session_id
from ...providers import ModelProvider
from ..profile import RuntimeProfile, default_runtime_profile
from ...contracts import (
    AgentTemplate,
    BenchmarkTask,
    BranchResumeSnapshot,
    CapabilityExchange,
    CheckpointEnvelope,
    ExecutionFlags,
    ExecutionPlan,
    ExecutionPlanRequirements,
    InputBinding,
    Checkpoint,
    OpenAITraceContext,
    PlanNode,
    PlanOrigin,
    InspectRequest,
    ModelRequest,
    ModelResponse,
    OperationSpec,
    RequestFileRef,
    RunResult,
    RuntimeBatchRequest,
    RuntimeEvent,
    RuntimeSessionSeed,
    RuntimeSolveResponse,
    RuntimeSolveRequest,
    RuntimeTaskInvocation,
    SideEffectReceipt,
    SolveRequest,
    SolveResult,
    VerificationPlan,
    capability_scope_allows,
    capability_scope_requires_filesystem_write,
    capability_scope_requires_network_access,
    capability_scope_service_categories,
    capability_scope_service_transports,
    expand_capability_scopes,
    get_plan_node_descriptor,
    is_terminal_receipt,
    normalize_capability_scopes,
    normalize_service_transports,
    plan_node_allowed_in_prompt_mode_local_only,
    plan_node_requires_default_provider,
    service_action_transport_compatibility,
)
from ...utils import now_ts, stable_hash

def _category_allowed(allowed_categories: list[str], required_category: str | None) -> bool:
    return capability_scope_allows(allowed_categories, required_category)


def _tool_category_hint(tool_hint: str | None) -> str:
    parts = [part for part in str(tool_hint or "").split("/") if part]
    if len(parts) >= 2:
        return "/".join(parts[:2])
    return parts[0] if parts else ""


def _dedupe_tool_categories(categories: Sequence[str]) -> list[str]:
    return normalize_capability_scopes(categories)


def _capability_intent(
    *,
    required_tool_categories: Sequence[str] = (),
    requires_default_provider: bool = False,
    requires_network_access: bool = False,
    network_transports: Sequence[str] = (),
    requires_filesystem_write: bool = False,
) -> dict[str, Any]:
    return {
        "required_tool_categories": expand_capability_scopes(required_tool_categories),
        "requires_default_provider": bool(requires_default_provider),
        "requires_network_access": bool(requires_network_access),
        "network_transports": normalize_service_transports(network_transports),
        "requires_filesystem_write": bool(requires_filesystem_write),
    }


def execution_plan_requires_default_provider(plan: ExecutionPlan) -> bool:
    return any(plan_node_requires_default_provider(node) for node in plan.nodes)


def execution_plan_is_prompt_local_only(plan: ExecutionPlan) -> bool:
    return all(plan_node_allowed_in_prompt_mode_local_only(node) for node in plan.nodes)


def _dedupe_network_transports(transports: Sequence[str]) -> list[str]:
    return normalize_service_transports(transports)


def _tool_category_network_transport(category: str) -> str | None:
    transports = capability_scope_service_transports([category])
    if len(transports) != 1:
        return None
    return transports[0]


def _tool_category_requires_network_access(category: str) -> bool:
    return capability_scope_requires_network_access(category)


def _tool_category_requires_filesystem_write(category: str) -> bool:
    return capability_scope_requires_filesystem_write(category)


def _plan_node_capability_intent(node: PlanNode) -> dict[str, Any]:
    payload = node.metadata.get("capability_intent")
    if isinstance(payload, Mapping):
        return dict(payload)
    return {}


def execution_plan_requirements(plan: ExecutionPlan) -> ExecutionPlanRequirements:
    request_mode = "user_request" if plan.origin.origin_kind == "user_request" else "benchmark"
    required_tool_categories: list[str] = []
    default_provider_nodes: list[str] = []
    network_nodes: list[str] = []
    network_transport_nodes: dict[str, list[str]] = {}
    filesystem_write_nodes: list[str] = []

    for node in plan.nodes:
        capability_intent = _plan_node_capability_intent(node)
        node_tool_categories = expand_capability_scopes(capability_intent.get("required_tool_categories", []))
        if not node_tool_categories:
            fallback_categories: list[str] = []
            tool_category_hint = str(node.metadata.get("tool_category_hint", "") or "").strip()
            if tool_category_hint:
                fallback_categories.append(tool_category_hint)
            elif str(node.node_kind) in {"builtin_op", "tool_call", "tool_synthesis", "repo_patch", "service_action"}:
                fallback_categories.extend(node.allowed_tool_categories)
            node_tool_categories = expand_capability_scopes(fallback_categories)
        required_tool_categories.extend(node_tool_categories)

        requires_default_provider = (
            bool(capability_intent.get("requires_default_provider"))
            if "requires_default_provider" in capability_intent
            else plan_node_requires_default_provider(node)
        )
        if requires_default_provider:
            default_provider_nodes.append(node.node_id)

        requires_network_access = (
            bool(capability_intent.get("requires_network_access"))
            if "requires_network_access" in capability_intent
            else str(node.node_kind) == "service_action"
            or any(_tool_category_requires_network_access(category) for category in node_tool_categories)
        )
        node_network_transports = normalize_service_transports(capability_intent.get("network_transports", []))
        if not node_network_transports:
            fallback_network_transports: list[str] = []
            service_transport = str(node.metadata.get("service_transport", "") or "").strip().lower()
            if service_transport:
                fallback_network_transports.append(service_transport)
            fallback_network_transports.extend(capability_scope_service_transports(node_tool_categories))
            node_network_transports = normalize_service_transports(fallback_network_transports)
        if requires_network_access and not node_network_transports:
            node_network_transports = ["generic"]
        if requires_network_access:
            network_nodes.append(node.node_id)
            for transport in node_network_transports:
                network_transport_nodes.setdefault(transport, []).append(node.node_id)

        requires_filesystem_write = (
            bool(capability_intent.get("requires_filesystem_write"))
            if "requires_filesystem_write" in capability_intent
            else str(node.node_kind) == "repo_patch"
            or any(_tool_category_requires_filesystem_write(category) for category in node_tool_categories)
        )
        if requires_filesystem_write:
            filesystem_write_nodes.append(node.node_id)

    return ExecutionPlanRequirements(
        request_mode=request_mode,
        requires_default_provider=bool(default_provider_nodes),
        default_provider_nodes=default_provider_nodes,
        requires_network_access=bool(network_nodes),
        network_nodes=network_nodes,
        required_network_transports=sorted(network_transport_nodes),
        network_transport_nodes={key: list(value) for key, value in sorted(network_transport_nodes.items())},
        requires_filesystem_write=bool(filesystem_write_nodes),
        filesystem_write_nodes=filesystem_write_nodes,
        required_tool_categories=expand_capability_scopes(required_tool_categories),
    )
