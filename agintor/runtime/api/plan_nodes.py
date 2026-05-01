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

from .capabilities import (
    _capability_intent,
    _tool_category_hint,
)

def _request_file_binding_overrides(file_refs: Sequence[RequestFileRef]) -> dict[str, list[dict[str, Any]]]:
    overrides: dict[str, list[dict[str, Any]]] = {}
    for index, file_ref in enumerate(file_refs):
        overrides[f"read_file_{index}"] = [
            {
                "target_arg": "path",
                "source_kind": "request_file",
                "source_ref": file_ref.runtime_path,
                "required": True,
            }
        ]
    return overrides


def _build_file_read_operations(file_refs: Sequence[RequestFileRef]) -> list[OperationSpec]:
    operations: list[OperationSpec] = []
    for index, file_ref in enumerate(file_refs):
        operations.append(
            OperationSpec(
                op_id=f"read_file_{index}",
                kind="tool_call",
                output_key=f"file_{index}",
                description=f"Read the contents of {file_ref.source_path}",
                tool_hint="filesystem/read_text_file",
                args={},
                externally_visible=False,
            )
        )
    return operations


def _operation_node_payload(operation: OperationSpec, task: BenchmarkTask) -> dict[str, Any]:
    kind = str(operation.kind or "").strip().lower()
    metadata = {
        "operation_kind": kind or str(operation.kind or ""),
        "task_type": task.task_type,
        "family": task.family,
    }
    if kind == "memory_lookup":
        return {
            "node_kind": "memory_lookup",
            "tool_hint": operation.tool_hint,
            "allowed_tool_categories": list(task.allowed_tool_categories),
            "metadata": {
                **metadata,
                "capability_intent": _capability_intent(),
            },
        }
    if kind == "builtin":
        tool_category_hint = _tool_category_hint(operation.tool_hint)
        return {
            "node_kind": "builtin_op",
            "tool_hint": operation.tool_hint,
            "allowed_tool_categories": list(task.allowed_tool_categories),
            "metadata": {
                **metadata,
                "provider_backed": False,
                "tool_category_hint": tool_category_hint,
                "capability_intent": _capability_intent(
                    required_tool_categories=[tool_category_hint] if tool_category_hint else [],
                ),
            },
        }
    if kind in {"generated_expression", "tool_synthesis"}:
        provider_backed = not bool(str(operation.expression or "").strip())
        tool_category_hint = "generated/provider" if provider_backed else "generated/local"
        return {
            "node_kind": "tool_synthesis",
            "tool_hint": operation.tool_hint
            or ("generated/provider/synthesize" if provider_backed else "generated/local/eval_expression"),
            "allowed_tool_categories": list(task.allowed_tool_categories) or [tool_category_hint],
            "metadata": {
                **metadata,
                "provider_backed": provider_backed,
                "tool_category_hint": tool_category_hint,
                "synthesis_template": "provider_assisted" if provider_backed else "deterministic_expression",
                "capability_intent": _capability_intent(
                    required_tool_categories=[tool_category_hint],
                    requires_default_provider=provider_backed,
                ),
            },
        }
    if kind == "direct_response":
        return {
            "node_kind": "direct_response",
            "tool_hint": operation.tool_hint,
            "allowed_tool_categories": list(task.allowed_tool_categories),
            "metadata": {
                **metadata,
                "provider_backed": True,
                "capability_intent": _capability_intent(requires_default_provider=True),
            },
        }
    if kind == "repo_patch":
        repo_categories = ["filesystem/read", "filesystem/patch"]
        return {
            "node_kind": "repo_patch",
            "tool_hint": operation.tool_hint,
            "allowed_tool_categories": list(task.allowed_tool_categories) or repo_categories,
            "metadata": {
                **metadata,
                "provider_backed": True,
                "tool_category_hint": "filesystem/patch",
                "target_file_paths": list(task.file_paths),
                "capability_intent": _capability_intent(
                    required_tool_categories=repo_categories,
                    requires_default_provider=True,
                    requires_filesystem_write=True,
                ),
            },
        }
    if kind == "service_action":
        service_compatibility = service_action_transport_compatibility(
            url=str(operation.args.get("url", "") or ""),
            service_transport=operation.args.get("service_transport", task.metadata.get("service_transport")),
            category_hint=task.metadata.get("service_category_hint"),
            allowed_tool_categories=list(task.allowed_tool_categories) or ["service/http"],
        )
        return {
            "node_kind": "service_action",
            "tool_hint": operation.tool_hint,
            "allowed_tool_categories": list(task.allowed_tool_categories) or ["service/http"],
            "metadata": {
                **metadata,
                "provider_backed": False,
                "service_transport": service_compatibility.transport,
                "tool_category_hint": f"service/{service_compatibility.transport}",
                "capability_intent": _capability_intent(
                    required_tool_categories=[f"service/{service_compatibility.transport}"],
                    requires_network_access=True,
                    network_transports=[service_compatibility.transport],
                ),
            },
        }
    if kind == "tool_call":
        tool_category_hint = _tool_category_hint(operation.tool_hint)
        capability_categories = expand_capability_scopes([tool_category_hint] if tool_category_hint else [])
        provider_backed = bool(operation.args.get("provider_backed", False))
        return {
            "node_kind": "tool_call",
            "tool_hint": operation.tool_hint,
            "allowed_tool_categories": list(task.allowed_tool_categories) or capability_categories,
            "metadata": {
                **metadata,
                "provider_backed": provider_backed,
                "tool_category_hint": tool_category_hint,
                "tool_call_kind": kind,
                "capability_intent": _capability_intent(
                    required_tool_categories=capability_categories,
                    requires_default_provider=provider_backed,
                    requires_network_access=any(
                        capability_scope_requires_network_access(category)
                        for category in capability_categories
                    ),
                    network_transports=capability_scope_service_transports(capability_categories),
                    requires_filesystem_write=any(
                        capability_scope_requires_filesystem_write(category)
                        for category in capability_categories
                    ),
                ),
            },
        }
    raise ValueError(f"unsupported_operation:{operation.kind}")


def _plan_constant_key(node_id: str, arg_name: str) -> str:
    return f"{node_id}.{arg_name}"


def _attach_branch_groups(nodes: Sequence[PlanNode]) -> tuple[list[PlanNode], dict[str, list[PlanNode]]]:
    frontier_groups: dict[tuple[str, ...], list[PlanNode]] = {}
    for node in nodes:
        if not get_plan_node_descriptor(str(node.node_kind)).branchable:
            continue
        frontier_groups.setdefault(tuple(node.dependencies), []).append(node)
    grouped_members: dict[str, list[PlanNode]] = {}
    branch_assignments: dict[str, str] = {}
    for dependency_signature, grouped_nodes in frontier_groups.items():
        if len(grouped_nodes) <= 1:
            continue
        group_id = f"branch.{stable_hash(dependency_signature, [node.node_id for node in grouped_nodes])[:12]}"
        grouped_members[group_id] = grouped_nodes
        for node in grouped_nodes:
            branch_assignments[node.node_id] = group_id
    updated_nodes = [
        (node).model_copy(update={"branch_group_id": branch_assignments.get(node.node_id)})
        for node in nodes
    ]
    return updated_nodes, grouped_members


def _append_merge_nodes(nodes: Sequence[PlanNode], grouped_members: Mapping[str, Sequence[PlanNode]]) -> list[PlanNode]:
    merged_nodes = list(nodes)
    merge_dependency_map: dict[str, str] = {}
    for branch_group_id, members in grouped_members.items():
        member_ids = [member.node_id for member in members]
        merge_node_id = f"merge.{branch_group_id}"
        merged_nodes.append(
            PlanNode(
                node_id=merge_node_id,
                op_id=merge_node_id,
                node_kind="merge",
                instruction=f"Merge outputs for {branch_group_id}",
                kind="merge",
                description=f"Deterministically merge branch group {branch_group_id}",
                output_key=f"{merge_node_id}.output",
                dependencies=member_ids,
                input_bindings=[],
                verification_required=False,
                externally_visible=False,
                frame_role="merge",
                metadata={
                    "consumes_branch_group": branch_group_id,
                    "member_output_keys": [member.output_key for member in members],
                },
            )
        )
        for member_id in member_ids:
            merge_dependency_map[member_id] = merge_node_id

    if not merge_dependency_map:
        return merged_nodes

    adjusted_nodes: list[PlanNode] = []
    for node in merged_nodes:
        if str(node.node_kind) == "merge":
            adjusted_nodes.append(node)
            continue
        extra_dependencies = [
            merge_dependency_map[dependency_id]
            for dependency_id in node.dependencies
            if dependency_id in merge_dependency_map
        ]
        if extra_dependencies:
            merged_dependencies = list(node.dependencies)
            for merge_dependency in extra_dependencies:
                if merge_dependency not in merged_dependencies:
                    merged_dependencies.append(merge_dependency)
            adjusted_nodes.append((node).model_copy(update={"dependencies": merged_dependencies}))
        else:
            adjusted_nodes.append(node)
    return adjusted_nodes


def _terminal_dependency_node_ids(nodes: Sequence[PlanNode], terminal_output_keys: Sequence[str]) -> list[str]:
    producer_by_output = {node.output_key: node.node_id for node in nodes if str(node.output_key).strip()}
    merge_by_member = {
        dependency_id: node.node_id
        for node in nodes
        if str(node.node_kind) == "merge"
        for dependency_id in node.dependencies
    }
    ordered_dependencies: list[str] = []
    for output_key in terminal_output_keys:
        producer_id = producer_by_output.get(output_key)
        dependency_id = merge_by_member.get(producer_id, producer_id)
        if dependency_id and dependency_id not in ordered_dependencies:
            ordered_dependencies.append(dependency_id)
    return ordered_dependencies


def _task_terminal_output_keys(task: BenchmarkTask) -> list[str]:
    metadata_terminal_keys = [
        str(output_key).strip()
        for output_key in task.metadata.get("terminal_output_keys", [])
        if str(output_key).strip()
    ]
    if metadata_terminal_keys:
        return metadata_terminal_keys
    visible_output_keys = [
        str(operation.output_key).strip()
        for operation in task.operations
        if operation.externally_visible and str(operation.output_key).strip()
    ]
    if visible_output_keys:
        return visible_output_keys
    return [str(operation.output_key).strip() for operation in task.operations if str(operation.output_key).strip()]


def _append_verify_node(nodes: Sequence[PlanNode], task: BenchmarkTask, terminal_output_keys: Sequence[str]) -> list[PlanNode]:
    exact_verifier_exists = str(task.verifier_type or "").strip().lower() not in {"", "none"}
    needs_verify = bool(task.verification_required or (task.externally_visible and exact_verifier_exists))
    if not needs_verify:
        return list(nodes)
    if not terminal_output_keys:
        return list(nodes)
    terminal_dependencies = _terminal_dependency_node_ids(nodes, terminal_output_keys)
    if not terminal_dependencies:
        return list(nodes)
    verify_node_id = f"verify.{stable_hash(task.task_id, terminal_output_keys)[:12]}"
    return [
        *nodes,
        PlanNode(
            node_id=verify_node_id,
            op_id=verify_node_id,
            node_kind="verify",
            instruction="Verify the terminal artifact before completion",
            kind="verify",
            description="Execute the terminal verification step for the produced artifact",
            output_key=f"{verify_node_id}.status",
            dependencies=terminal_dependencies,
            input_bindings=[],
            verification_required=True,
            externally_visible=False,
            frame_role="verify",
            metadata={
                "verifier_type": task.verifier_type,
                "artifact_contract": {
                    "task_id": task.task_id,
                    "task_type": task.task_type,
                    "output_keys": terminal_output_keys,
                    "family": task.family,
                },
                "terminal_output_keys": terminal_output_keys,
            },
        ),
    ]


def _compile_plan_nodes(task: BenchmarkTask) -> tuple[list[PlanNode], dict[str, Any]]:
    dependency_to_output = {operation.op_id: operation.output_key for operation in task.operations}
    binding_overrides_by_node = {
        str(node_id): list(bindings)
        for node_id, bindings in dict(task.metadata.get("input_binding_overrides", {})).items()
        if isinstance(bindings, list)
    }
    nodes: list[PlanNode] = []
    plan_constants: dict[str, Any] = {}
    for operation in task.operations:
        node_payload = _operation_node_payload(operation, task)
        static_args = dict(operation.args)
        if operation.requires_exact_symbol:
            static_args["requires_exact_symbol"] = operation.requires_exact_symbol
        input_bindings = [
            InputBinding(
                target_arg=arg_name,
                source_kind="plan_constant",
                source_ref=_plan_constant_key(operation.op_id, arg_name),
                required=True,
            )
            for arg_name in static_args
        ]
        for arg_name, value in static_args.items():
            plan_constants[_plan_constant_key(operation.op_id, arg_name)] = value
        for dependency_id in operation.dependencies:
            input_bindings.append(
                InputBinding(
                    target_arg=dependency_to_output.get(dependency_id, dependency_id),
                    source_kind="upstream_output",
                    source_ref=dependency_id,
                    required=True,
                )
            )
        for binding_payload in binding_overrides_by_node.get(operation.op_id, []):
            input_bindings.append((InputBinding).model_validate(binding_payload))
        nodes.append(
            PlanNode(
                node_id=operation.op_id,
                op_id=operation.op_id,
                node_kind=node_payload["node_kind"],
                instruction=operation.description,
                kind=operation.kind,
                description=operation.description,
                output_key=operation.output_key,
                args=dict(operation.args),
                expression=operation.expression,
                dependencies=list(operation.dependencies),
                tool_hint=node_payload["tool_hint"],
                allowed_tool_categories=list(node_payload["allowed_tool_categories"]),
                static_args=dict(static_args),
                input_bindings=input_bindings,
                verification_required=bool(task.verification_required),
                externally_visible=bool(operation.externally_visible or task.externally_visible),
                frame_role="worker",
                metadata=dict(node_payload["metadata"]),
            )
        )
    terminal_output_keys = _task_terminal_output_keys(task)
    nodes, grouped_members = _attach_branch_groups(nodes)
    nodes = _append_merge_nodes(nodes, grouped_members)
    nodes = _append_verify_node(nodes, task, terminal_output_keys)
    return nodes, plan_constants
