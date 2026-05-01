from __future__ import annotations

import base64
import json
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, NamedTuple, Optional, Sequence
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from ..utils import cheap_embedding, now_ts, stable_hash

from .tracing import OpenAITraceContext

class OperationSpec(BaseModel):
    op_id: str
    kind: str
    output_key: str
    description: str
    tool_hint: Optional[str] = None
    expression: Optional[str] = None
    args: Dict[str, Any] = Field(default_factory=dict)
    requires_exact_symbol: Optional[str] = None
    dependencies: List[str] = Field(default_factory=list)
    externally_visible: bool = False


class InputBinding(BaseModel):
    target_arg: str
    source_kind: Literal["request_context", "request_file", "upstream_output", "plan_constant"]
    source_ref: str
    required: bool = True


class RequestFileRef(BaseModel):
    file_ref_id: str
    source_path: str
    runtime_path: str
    path_root: Literal["host_absolute", "runtime_workspace_relative"]
    host_path: Optional[str] = None
    workspace_relative_path: Optional[str] = None

    @model_validator(mode="after")
    def validate_request_file_ref(self) -> "RequestFileRef":
        path_root = str(self.path_root or "").strip()
        host_path = str(self.host_path or "").strip()
        workspace_relative_path = str(self.workspace_relative_path or "").strip()
        runtime_path = str(self.runtime_path or "").strip()
        if not runtime_path:
            raise ValueError("request file refs require runtime_path")
        if path_root == "host_absolute":
            if not host_path:
                raise ValueError("host_absolute request file refs require host_path")
            if workspace_relative_path:
                raise ValueError("host_absolute request file refs may not set workspace_relative_path")
        elif path_root == "runtime_workspace_relative":
            if not workspace_relative_path:
                raise ValueError("runtime_workspace_relative request file refs require workspace_relative_path")
            if host_path:
                raise ValueError("runtime_workspace_relative request file refs may not set host_path")
        else:
            raise ValueError(f"unsupported request file ref path_root {path_root!r}")
        return self


class PlanOrigin(BaseModel):
    origin_kind: Literal["benchmark", "user_request"]
    source_task_id: Optional[str] = None
    source_request_id: Optional[str] = None
    source_suite: Optional[str] = None
    adapter_kind: str
    adaptation_assumptions: List[str] = Field(default_factory=list)


PlanNodeKind = Literal[
    "builtin_op",
    "memory_lookup",
    "tool_call",
    "tool_synthesis",
    "direct_response",
    "repo_patch",
    "service_action",
    "merge",
    "verify",
]


class PlanNodeDescriptor(BaseModel):
    executor_name: str
    value_producing: bool = True
    branchable: bool = False
    requires_default_provider: bool = False
    prompt_local_only_allowed: bool = False
    provider_backed_metadata_key: Optional[str] = None
    validation_tags: List[str] = Field(default_factory=list)


class PlanNode(BaseModel):
    node_id: str
    op_id: str = ""
    node_kind: PlanNodeKind
    instruction: str
    kind: str = ""
    description: str = ""
    output_key: str
    args: Dict[str, Any] = Field(default_factory=dict)
    expression: Optional[str] = None
    dependencies: List[str] = Field(default_factory=list)
    tool_hint: Optional[str] = None
    allowed_tool_categories: List[str] = Field(default_factory=list)
    static_args: Dict[str, Any] = Field(default_factory=dict)
    input_bindings: List[InputBinding] = Field(default_factory=list)
    verification_required: bool = False
    externally_visible: bool = False
    frame_role: str = "worker"
    branch_group_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ExecutionPlanRequirements(BaseModel):
    request_mode: Literal["benchmark", "user_request"] = "benchmark"
    requires_default_provider: bool = False
    default_provider_nodes: List[str] = Field(default_factory=list)
    requires_network_access: bool = False
    network_nodes: List[str] = Field(default_factory=list)
    required_network_transports: List[str] = Field(default_factory=list)
    network_transport_nodes: Dict[str, List[str]] = Field(default_factory=dict)
    requires_filesystem_write: bool = False
    filesystem_write_nodes: List[str] = Field(default_factory=list)
    required_tool_categories: List[str] = Field(default_factory=list)


_SERVICE_ACTION_TRANSPORT_SCHEMES: Dict[str, tuple[str, ...]] = {
    "http": ("http", "https"),
}


_SERVICE_ACTION_CATEGORY_TO_TRANSPORT: Dict[str, str] = {
    "service/http": "http",
}


def normalize_capability_scope(scope: Any) -> str:
    return str(scope or "").strip().strip("/").lower()


def normalize_capability_scopes(scopes: Sequence[Any]) -> List[str]:
    normalized: List[str] = []
    seen: set[str] = set()
    for scope in scopes:
        key = normalize_capability_scope(scope)
        if not key or key in seen:
            continue
        seen.add(key)
        normalized.append(key)
    return normalized


def expand_capability_scopes(scopes: Sequence[Any]) -> List[str]:
    expanded: List[str] = []
    for scope in normalize_capability_scopes(scopes):
        if scope == "service/*":
            expanded.extend(sorted(_SERVICE_ACTION_CATEGORY_TO_TRANSPORT))
            continue
        expanded.append(scope)
    return normalize_capability_scopes(expanded)


def capability_scope_allows(granted_scopes: Sequence[Any], required_scope: Any) -> bool:
    normalized_required = normalize_capability_scope(required_scope)
    if not normalized_required:
        return True
    normalized_grants = normalize_capability_scopes(granted_scopes)
    if not normalized_grants:
        return True
    for granted_scope in normalized_grants:
        if granted_scope == normalized_required:
            return True
        if granted_scope.endswith("/*") and normalized_required.startswith(granted_scope[:-1]):
            return True
    return False


def capability_scope_requires_network_access(scope: Any) -> bool:
    return normalize_capability_scope(scope).startswith("service/")


def capability_scope_requires_filesystem_write(scope: Any) -> bool:
    return capability_scope_allows([scope], "filesystem/write") or capability_scope_allows([scope], "filesystem/patch")


def capability_scope_service_categories(scopes: Sequence[Any]) -> List[str]:
    return [scope for scope in expand_capability_scopes(scopes) if scope.startswith("service/")]


def normalize_service_transport(transport: Any) -> str:
    normalized = normalize_capability_scope(transport)
    if not normalized:
        return ""
    if normalized in _SERVICE_ACTION_TRANSPORT_SCHEMES:
        return normalized
    return _SERVICE_ACTION_CATEGORY_TO_TRANSPORT.get(normalized, "")


def normalize_service_transports(transports: Sequence[Any]) -> List[str]:
    normalized: List[str] = []
    seen: set[str] = set()
    for transport in transports:
        key = normalize_service_transport(transport)
        if not key or key in seen:
            continue
        seen.add(key)
        normalized.append(key)
    return normalized


def capability_scope_service_transports(scopes: Sequence[Any]) -> List[str]:
    return normalize_service_transports(capability_scope_service_categories(scopes))


def _service_transport_candidates(value: Any) -> List[str]:
    normalized = normalize_capability_scope(value)
    if not normalized:
        return []
    if normalized == "service/*":
        return normalize_service_transports(_SERVICE_ACTION_TRANSPORT_SCHEMES.keys())
    transport = normalize_service_transport(normalized)
    if transport:
        return [transport]
    raise ValueError(f"service_action declares unsupported transport hint {value!r}")


class ServiceActionTransportCompatibility(NamedTuple):
    transport: str
    allowed_schemes: tuple[str, ...]
    url_scheme: str


def service_action_transport_compatibility(
    *,
    url: str,
    service_transport: Any = None,
    category_hint: Any = None,
    allowed_tool_categories: Sequence[str] | None = None,
) -> ServiceActionTransportCompatibility:
    normalized_url = str(url or "").strip()
    if not normalized_url:
        raise ValueError("service_action url may not be empty")
    normalized_allowed_categories = normalize_capability_scopes(allowed_tool_categories or [])
    allowed_transports = set(capability_scope_service_transports(normalized_allowed_categories))
    if normalized_allowed_categories and not allowed_transports:
        raise ValueError(
            "service_action must declare a supported service transport via service_transport or service/* allowed_tool_categories"
        )

    explicit_transports: set[str] = set()
    for candidate in (service_transport, category_hint):
        explicit_transports.update(_service_transport_candidates(candidate))

    candidate_transports = explicit_transports or allowed_transports or set(_SERVICE_ACTION_TRANSPORT_SCHEMES)
    if allowed_transports:
        candidate_transports &= allowed_transports
    if explicit_transports and not candidate_transports:
        raise ValueError(
            "service_action declares a transport that is not permitted by "
            f"allowed_tool_categories {capability_scope_service_categories(normalized_allowed_categories)!r}"
        )
    if not candidate_transports:
        raise ValueError(
            "service_action must declare a supported service transport via service_transport or service/* allowed_tool_categories"
        )

    url_scheme = str(urlparse(normalized_url).scheme or "").strip().lower()
    viable_transports = [
        transport
        for transport in sorted(candidate_transports)
        if url_scheme in _SERVICE_ACTION_TRANSPORT_SCHEMES[transport]
    ]
    if not viable_transports:
        allowed_schemes = sorted(
            {
                scheme
                for transport in sorted(candidate_transports)
                for scheme in _SERVICE_ACTION_TRANSPORT_SCHEMES[transport]
            }
        )
        if len(candidate_transports) == 1:
            transport = next(iter(candidate_transports))
            raise ValueError(
                f"service_action transport {transport!r} only permits URL schemes {allowed_schemes!r}; got {normalized_url!r}"
            )
        raise ValueError(
            f"service_action transports {sorted(candidate_transports)!r} only permit URL schemes {allowed_schemes!r}; got {normalized_url!r}"
        )
    if len(viable_transports) != 1:
        raise ValueError(
            f"service_action declares conflicting transports {viable_transports!r}; declare exactly one transport family"
        )
    transport = viable_transports[0]
    return ServiceActionTransportCompatibility(
        transport=transport,
        allowed_schemes=_SERVICE_ACTION_TRANSPORT_SCHEMES[transport],
        url_scheme=url_scheme,
    )


PLAN_NODE_DESCRIPTOR_REGISTRY: Dict[str, PlanNodeDescriptor] = {
    "builtin_op": PlanNodeDescriptor(
        executor_name="_execute_builtin_node",
        value_producing=True,
        branchable=True,
        requires_default_provider=False,
        prompt_local_only_allowed=True,
    ),
    "memory_lookup": PlanNodeDescriptor(
        executor_name="_execute_memory_lookup_node",
        value_producing=True,
        branchable=True,
        requires_default_provider=False,
        prompt_local_only_allowed=True,
    ),
    "tool_call": PlanNodeDescriptor(
        executor_name="_execute_tool_call_node",
        value_producing=True,
        branchable=True,
        requires_default_provider=False,
        prompt_local_only_allowed=True,
        provider_backed_metadata_key="provider_backed",
        validation_tags=["tool_like"],
    ),
    "tool_synthesis": PlanNodeDescriptor(
        executor_name="_execute_tool_synthesis_node",
        value_producing=True,
        branchable=True,
        requires_default_provider=False,
        prompt_local_only_allowed=True,
        provider_backed_metadata_key="provider_backed",
        validation_tags=["tool_like"],
    ),
    "direct_response": PlanNodeDescriptor(
        executor_name="_execute_direct_response_node",
        value_producing=True,
        branchable=True,
        requires_default_provider=True,
        prompt_local_only_allowed=False,
    ),
    "repo_patch": PlanNodeDescriptor(
        executor_name="_execute_repo_patch_node",
        value_producing=True,
        branchable=True,
        requires_default_provider=False,
        prompt_local_only_allowed=False,
        provider_backed_metadata_key="provider_backed",
        validation_tags=["repo_patch"],
    ),
    "service_action": PlanNodeDescriptor(
        executor_name="_execute_service_action_node",
        value_producing=True,
        branchable=True,
        requires_default_provider=False,
        prompt_local_only_allowed=False,
        validation_tags=["service_action"],
    ),
    "merge": PlanNodeDescriptor(
        executor_name="_execute_merge_node",
        value_producing=True,
        branchable=False,
        requires_default_provider=False,
        prompt_local_only_allowed=True,
        validation_tags=["merge"],
    ),
    "verify": PlanNodeDescriptor(
        executor_name="_execute_verify_node",
        value_producing=False,
        branchable=False,
        requires_default_provider=False,
        prompt_local_only_allowed=True,
        validation_tags=["verify"],
    ),
}


def get_plan_node_descriptor(node_kind: str) -> PlanNodeDescriptor:
    normalized = str(node_kind or "").strip()
    if normalized not in PLAN_NODE_DESCRIPTOR_REGISTRY:
        raise ValueError(f"unsupported execution plan node kind {normalized!r}")
    return PLAN_NODE_DESCRIPTOR_REGISTRY[normalized]


def plan_node_requires_default_provider(node: PlanNode) -> bool:
    descriptor = get_plan_node_descriptor(str(node.node_kind))
    if descriptor.provider_backed_metadata_key:
        return bool(node.metadata.get(descriptor.provider_backed_metadata_key))
    return descriptor.requires_default_provider


def plan_node_allowed_in_prompt_mode_local_only(node: PlanNode) -> bool:
    descriptor = get_plan_node_descriptor(str(node.node_kind))
    if descriptor.provider_backed_metadata_key:
        return not bool(node.metadata.get(descriptor.provider_backed_metadata_key))
    return descriptor.prompt_local_only_allowed


def _validate_tool_like_node(node: PlanNode, node_map: Dict[str, PlanNode], plan_values: Dict[str, Any]) -> None:
    if not str(node.tool_hint or "").strip() and not node.allowed_tool_categories:
        category_hint = str(node.metadata.get("tool_category_hint", "") or "").strip()
        if not category_hint:
            raise ValueError(f"{node.node_kind} node {node.node_id!r} must declare a tool hint or category hint")
    if str(node.node_kind) == "tool_synthesis":
        expression = str(node.expression or "").strip()
        synthesis_template = str(node.metadata.get("synthesis_template", "") or "").strip()
        if not expression and not synthesis_template:
            raise ValueError(
                f"tool_synthesis node {node.node_id!r} must declare an expression or synthesis template metadata"
            )


def _validate_merge_node(node: PlanNode, node_map: Dict[str, PlanNode], plan_values: Dict[str, Any]) -> None:
    branch_group = str(node.metadata.get("consumes_branch_group", "") or "").strip()
    consumes_node_ids = [str(node_id).strip() for node_id in node.metadata.get("consumes_node_ids", []) if str(node_id).strip()]
    if not branch_group:
        raise ValueError(f"merge node {node.node_id!r} must declare metadata.consumes_branch_group")
    members = [candidate.node_id for candidate in node_map.values() if candidate.branch_group_id == branch_group]
    if not members:
        raise ValueError(f"merge node {node.node_id!r} references unknown branch group {branch_group!r}")
    if list(node.dependencies) != members:
        raise ValueError(
            f"merge node {node.node_id!r} must depend on every member of branch group {branch_group!r}"
        )
    if consumes_node_ids and consumes_node_ids != members:
        raise ValueError(f"merge node {node.node_id!r} consumes_node_ids must match branch-group members")


def _validate_verify_node(node: PlanNode, node_map: Dict[str, PlanNode], plan_values: Dict[str, Any]) -> None:
    if not node.dependencies:
        raise ValueError(f"verify node {node.node_id!r} must depend on at least one value-producing node")
    terminal_output_keys = set(plan_values.get("terminal_output_keys", []))
    if str(node.output_key or "").strip() in terminal_output_keys:
        raise ValueError(f"verify node {node.node_id!r} may not produce a terminal output key")
    for dependency_id in node.dependencies:
        dependency_node = node_map[dependency_id]
        if not get_plan_node_descriptor(str(dependency_node.node_kind)).value_producing:
            raise ValueError(
                f"verify node {node.node_id!r} must depend only on value-producing nodes, found {dependency_node.node_kind!r}"
            )


def _validate_repo_patch_node(node: PlanNode, node_map: Dict[str, PlanNode], plan_values: Dict[str, Any]) -> None:
    target_file_paths = [
        str(path).strip()
        for path in node.static_args.get("target_file_paths", node.metadata.get("target_file_paths", []))
        if str(path).strip()
    ]
    if not target_file_paths:
        raise ValueError(f"repo_patch node {node.node_id!r} must declare target_file_paths")


def _validate_service_action_node(node: PlanNode, node_map: Dict[str, PlanNode], plan_values: Dict[str, Any]) -> None:
    url = str(node.static_args.get("url", node.metadata.get("url", "")) or "").strip()
    if not url:
        raise ValueError(f"service_action node {node.node_id!r} must declare a target url")
    method = str(node.static_args.get("method", node.metadata.get("method", "GET")) or "GET").strip().upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise ValueError(f"service_action node {node.node_id!r} has unsupported method {method!r}")
    service_action_transport_compatibility(
        url=url,
        service_transport=node.static_args.get("service_transport", node.metadata.get("service_transport")),
        category_hint=node.metadata.get("tool_category_hint", node.metadata.get("service_category_hint")),
        allowed_tool_categories=node.allowed_tool_categories,
    )


PLAN_NODE_VALIDATION_HOOKS: Dict[str, Callable[[PlanNode, Dict[str, PlanNode], Dict[str, Any]], None]] = {
    "tool_like": _validate_tool_like_node,
    "repo_patch": _validate_repo_patch_node,
    "service_action": _validate_service_action_node,
    "merge": _validate_merge_node,
    "verify": _validate_verify_node,
}


class VerificationPlan(BaseModel):
    mode: str = "none"
    required: bool = False
    checker_ladder: List[str] = Field(default_factory=list)
    exact_verifier_required: bool = False
    artifact_contract: Dict[str, Any] = Field(default_factory=dict)
    terminal_nodes: List[str] = Field(default_factory=list)
    verifier_type: str = "none"
    expected: Any = None


class ExecutionFlags(BaseModel):
    allow_best_effort: bool = False
    allow_resume: bool = True
    allow_branching: bool = True
    allow_tool_synthesis: bool = True
    allow_async_handles: bool = True
    requires_terminal_verification: bool = False


class ExecutionPlan(BaseModel):
    plan_digest: str = ""
    plan_id: str
    request_id: str
    origin: PlanOrigin
    objective: str
    context_refs: List[Dict[str, Any]] = Field(default_factory=list)
    file_refs: List[str] = Field(default_factory=list)
    file_ref_specs: List[RequestFileRef] = Field(default_factory=list)
    plan_constants: Dict[str, Any] = Field(default_factory=dict)
    nodes: List[PlanNode] = Field(default_factory=list)
    root_node_ids: List[str] = Field(default_factory=list)
    terminal_output_keys: List[str] = Field(default_factory=list)
    verification_plan: VerificationPlan = Field(default_factory=VerificationPlan)
    execution_flags: ExecutionFlags = Field(default_factory=ExecutionFlags)
    allowed_tool_categories: List[str] = Field(default_factory=list)
    budget_overrides: Dict[str, Any] = Field(default_factory=dict)
    externally_visible: bool = True
    trace_context: Optional[OpenAITraceContext] = None
    lifecycle_state: Literal[
        "compiled",
        "validated",
        "loaded",
        "running",
        "completed",
        "cancelled",
        "failed",
    ] = "compiled"

    @model_validator(mode="after")
    def validate_execution_plan(self) -> "ExecutionPlan":
        def to_jsonable(value: Any) -> Any:
            if hasattr(value, "model_dump"):
                return to_jsonable(value.model_dump())
            if isinstance(value, dict):
                return {str(key): to_jsonable(item) for key, item in value.items()}
            if isinstance(value, list):
                return [to_jsonable(item) for item in value]
            return value

        values = self.model_dump()
        nodes = list(self.nodes)
        node_map = {node.node_id: node for node in nodes}
        if len(node_map) != len(nodes):
            raise ValueError("execution plan node_id values must be unique")

        verification_plan = self.verification_plan or VerificationPlan()

        for root_id in self.root_node_ids:
            if root_id not in node_map:
                raise ValueError(f"execution plan root node {root_id!r} does not exist")

        for terminal_id in verification_plan.terminal_nodes:
            if terminal_id not in node_map:
                raise ValueError(f"verification terminal node {terminal_id!r} does not exist")

        for node in nodes:
            descriptor = get_plan_node_descriptor(str(node.node_kind))
            for dep in node.dependencies:
                if dep not in node_map:
                    raise ValueError(f"execution plan dependency {dep!r} for node {node.node_id!r} does not exist")
            if node.branch_group_id and not descriptor.branchable:
                raise ValueError(
                    f"execution plan node {node.node_id!r} of kind {node.node_kind!r} may not declare branch_group_id"
                )
            for tag in descriptor.validation_tags:
                PLAN_NODE_VALIDATION_HOOKS[tag](node, node_map, values)

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visited:
                return
            if node_id in visiting:
                raise ValueError("execution plan graph must be acyclic")
            visiting.add(node_id)
            for dep_id in node_map[node_id].dependencies:
                visit(dep_id)
            visiting.remove(node_id)
            visited.add(node_id)

        for node in nodes:
            visit(node.node_id)

        reachable: set[str] = set()

        def mark(node_id: str) -> None:
            if node_id in reachable:
                return
            reachable.add(node_id)
            for child in nodes:
                if node_id in child.dependencies:
                    mark(child.node_id)

        for root_id in self.root_node_ids:
            mark(root_id)

        produced_outputs: Dict[str, PlanNode] = {}
        for node in nodes:
            if node.node_id not in reachable or not str(node.output_key).strip():
                continue
            if get_plan_node_descriptor(str(node.node_kind)).value_producing:
                if node.output_key in produced_outputs:
                    raise ValueError(
                        f"duplicate execution plan output_key {node.output_key!r} is not allowed"
                    )
                produced_outputs[node.output_key] = node
        for terminal_key in self.terminal_output_keys:
            producer = produced_outputs.get(terminal_key)
            if producer is None:
                raise ValueError(
                    f"terminal output key {terminal_key!r} is not produced by a reachable value-producing node"
                )
            if str(producer.node_kind) == "verify":
                raise ValueError(f"terminal output key {terminal_key!r} may not be produced by a verify node")

        branch_groups: Dict[str, List[PlanNode]] = {}
        for node in nodes:
            if node.branch_group_id:
                branch_groups.setdefault(node.branch_group_id, []).append(node)
        for branch_group_id, grouped_nodes in branch_groups.items():
            dependency_signatures = {tuple(node.dependencies) for node in grouped_nodes}
            if len(dependency_signatures) > 1:
                raise ValueError(
                    f"branch group {branch_group_id!r} must be reachable from one live frontier with identical dependencies"
                )

        if verification_plan.required or verification_plan.exact_verifier_required:
            if not any(str(node.node_kind) == "verify" for node in nodes):
                raise ValueError("execution plan requires an explicit verify node when terminal verification is required")

        if not self.plan_digest:
            digest_payload = {key: value for key, value in values.items() if key != "plan_digest"}
            digest_payload["request_id"] = None
            digest_payload["trace_context"] = None
            self.plan_digest = stable_hash(to_jsonable(digest_payload))
        return self
