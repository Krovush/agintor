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
    _category_allowed,
    execution_plan_requires_default_provider,
)
from .plan_nodes import (
    _build_file_read_operations,
    _compile_plan_nodes,
    _request_file_binding_overrides,
    _task_terminal_output_keys,
    _terminal_dependency_node_ids,
)
from .prompt_intent import (
    _MODULUS_RE,
    _SYMBOL_RE,
    _find_file_owner,
    _find_symbol_value,
    _median,
    _number_value,
    _parse_number_list,
    _prompt_request_metadata,
    _prompt_requests_file_inspection,
    _prompt_requests_repo_patch,
    _prompt_template_allowed_categories,
    _service_request_spec,
    _verification_policy,
)
from .request_loading import (
    _compiled_request_file_refs,
    compile_request_file_ref,
)
from .tracing import (
    benchmark_task_episode_kind,
    benchmark_task_episode_step_index,
    runtime_trace_context,
    trace_context_field,
)

def solve_request_to_task(request: SolveRequest) -> BenchmarkTask:
    prompt = request.prompt
    prompt_lower = prompt.lower()
    file_ref_specs = _compiled_request_file_refs(request)
    source_file_paths = [file_ref.source_path for file_ref in file_ref_specs]
    runtime_file_paths = [file_ref.runtime_path for file_ref in file_ref_specs]
    request_meta = {
        **_prompt_request_metadata(
            request,
            template_kind="direct_answer",
            file_paths=source_file_paths,
        ),
        "request_file_refs": [(file_ref).model_dump() for file_ref in file_ref_specs],
        "request_file_runtime_paths": runtime_file_paths,
    }
    numbers = _parse_number_list(prompt)
    if numbers and "sum" in prompt_lower and "product" in prompt_lower and _category_allowed(request.allowed_tool_categories, "math/basic"):
        verification_required, allow_best_effort = _verification_policy(
            request.verification_preference,
            exact_verifier_exists=True,
        )
        expected = {
            "sum": _number_value(sum(numbers)),
            "product": _number_value(__import__("math").prod(numbers)),
        }
        return BenchmarkTask(
            task_id=f"user.{request.request_id}.sum_product",
            family="top",
            prompt=prompt,
            task_type="structured_ops",
            allowed_tool_categories=list(request.allowed_tool_categories),
            operations=[
                OperationSpec(
                    op_id="sum",
                    kind="builtin",
                    output_key="sum",
                    description="Compute sum of numbers",
                    tool_hint="math/basic/sum_numbers",
                    args={"numbers": [_number_value(value) for value in numbers]},
                ),
                OperationSpec(
                    op_id="product",
                    kind="builtin",
                    output_key="product",
                    description="Compute product of numbers",
                    tool_hint="math/basic/product_numbers",
                    args={"numbers": [_number_value(value) for value in numbers]},
                ),
            ],
            expected=expected,
            verifier_type="json_numeric",
            verification_required=verification_required,
            allow_best_effort=allow_best_effort,
            metadata=request_meta,
        )
    if numbers and "min" in prompt_lower and "max" in prompt_lower and _category_allowed(request.allowed_tool_categories, "math/basic"):
        verification_required, allow_best_effort = _verification_policy(
            request.verification_preference,
            exact_verifier_exists=True,
        )
        expected = {
            "min": _number_value(min(numbers)),
            "max": _number_value(max(numbers)),
        }
        return BenchmarkTask(
            task_id=f"user.{request.request_id}.min_max",
            family="top",
            prompt=prompt,
            task_type="structured_ops",
            allowed_tool_categories=list(request.allowed_tool_categories),
            operations=[
                OperationSpec(
                    op_id="min",
                    kind="builtin",
                    output_key="min",
                    description="Compute minimum number",
                    tool_hint="math/basic/min_number",
                    args={"numbers": [_number_value(value) for value in numbers]},
                ),
                OperationSpec(
                    op_id="max",
                    kind="builtin",
                    output_key="max",
                    description="Compute maximum number",
                    tool_hint="math/basic/max_number",
                    args={"numbers": [_number_value(value) for value in numbers]},
                ),
            ],
            expected=expected,
            verifier_type="json_numeric",
            verification_required=verification_required,
            allow_best_effort=allow_best_effort,
            metadata=request_meta,
        )
    if numbers and "max" in prompt_lower and "median" in prompt_lower and _category_allowed(request.allowed_tool_categories, "math/basic"):
        verification_required, allow_best_effort = _verification_policy(
            request.verification_preference,
            exact_verifier_exists=True,
        )
        expected = {
            "max": _number_value(max(numbers)),
            "median": _median(numbers),
        }
        return BenchmarkTask(
            task_id=f"user.{request.request_id}.max_median",
            family="top",
            prompt=prompt,
            task_type="structured_ops",
            allowed_tool_categories=list(request.allowed_tool_categories),
            operations=[
                OperationSpec(
                    op_id="max",
                    kind="builtin",
                    output_key="max",
                    description="Compute maximum number",
                    tool_hint="math/basic/max_number",
                    args={"numbers": [_number_value(value) for value in numbers]},
                ),
                OperationSpec(
                    op_id="median",
                    kind="builtin",
                    output_key="median",
                    description="Compute median number",
                    tool_hint="math/basic/median_number",
                    args={"numbers": [_number_value(value) for value in numbers]},
                ),
            ],
            expected=expected,
            verifier_type="json_numeric",
            verification_required=verification_required,
            allow_best_effort=allow_best_effort,
            metadata=request_meta,
        )
    modulus_match = _MODULUS_RE.search(prompt)
    if (
        numbers
        and modulus_match
        and any(token in prompt_lower for token in ("sum of squares", "squared", "square"))
        and _category_allowed(request.allowed_tool_categories, "generated/local")
    ):
        verification_required, allow_best_effort = _verification_policy(
            request.verification_preference,
            exact_verifier_exists=True,
        )
        modulus = float(modulus_match.group(1))
        expected_value = _number_value(sum(value * value for value in numbers) % modulus)
        return BenchmarkTask(
            task_id=f"user.{request.request_id}.sum_squares_mod",
            family="tool",
            prompt=prompt,
            task_type="tool_expression",
            allowed_tool_categories=list(request.allowed_tool_categories),
            operations=[
                OperationSpec(
                    op_id="expr",
                    kind="generated_expression",
                    output_key="value",
                    description="Compute the sum of squared numbers modulo the provided modulus",
                    expression="sum(x*x for x in numbers) % modulus",
                    args={"numbers": [_number_value(value) for value in numbers], "modulus": _number_value(modulus)},
                )
            ],
            expected=expected_value,
            verifier_type="number_exact",
            verification_required=verification_required,
            allow_best_effort=allow_best_effort,
            metadata=request_meta,
        )
    symbol_match = _SYMBOL_RE.search(prompt)
    if symbol_match:
        symbol = symbol_match.group(1)
        expected_symbol_value = _find_symbol_value(request, symbol)
        if expected_symbol_value is not None:
            verification_required, allow_best_effort = _verification_policy(
                request.verification_preference,
                exact_verifier_exists=True,
            )
            return BenchmarkTask(
                task_id=f"user.{request.request_id}.symbol_lookup",
                family="mem",
                prompt=prompt,
                task_type="memory_query",
                symbolic_seeds=[symbol],
                context_items=list(request.context_items),
                allowed_tool_categories=list(request.allowed_tool_categories),
                operations=[
                    OperationSpec(
                        op_id="lookup",
                        kind="memory_lookup",
                        output_key="answer",
                        description="Lookup exact symbol value",
                        requires_exact_symbol=symbol,
                    )
                ],
                expected=expected_symbol_value,
                verifier_type="string_exact",
            verification_required=verification_required,
            allow_best_effort=allow_best_effort,
            metadata=request_meta,
        )
    if source_file_paths:
        expected_owner = _find_file_owner(request, source_file_paths[0])
        if expected_owner is not None:
            verification_required, allow_best_effort = _verification_policy(
                request.verification_preference,
                exact_verifier_exists=True,
            )
            return BenchmarkTask(
                task_id=f"user.{request.request_id}.file_lookup",
                family="mem",
                prompt=prompt,
                task_type="memory_query",
                file_paths=runtime_file_paths,
                context_items=list(request.context_items),
                allowed_tool_categories=list(request.allowed_tool_categories),
                operations=[
                    OperationSpec(
                        op_id="owner",
                        kind="memory_lookup",
                        output_key="answer",
                        description="Lookup exact file path owner",
                    )
                ],
                expected=expected_owner,
                verifier_type="string_exact",
                verification_required=verification_required,
                allow_best_effort=allow_best_effort,
                metadata=request_meta,
            )
    if (
        _prompt_requests_repo_patch(prompt_lower, source_file_paths)
        and _category_allowed(request.allowed_tool_categories, "filesystem/read")
        and _category_allowed(request.allowed_tool_categories, "filesystem/patch")
    ):
        verification_required, allow_best_effort = _verification_policy(
            request.verification_preference,
            exact_verifier_exists=False,
        )
        read_operations = _build_file_read_operations(file_ref_specs)
        return BenchmarkTask(
            task_id=f"user.{request.request_id}.repo_patch",
            family="tool",
            prompt=prompt,
            task_type="bounded_repo_patch",
            file_paths=list(runtime_file_paths),
            context_items=list(request.context_items),
            allowed_tool_categories=_prompt_template_allowed_categories(
                request,
                ["filesystem/read", "filesystem/patch"],
            ),
            operations=[
                *read_operations,
                OperationSpec(
                    op_id="apply_patch",
                    kind="repo_patch",
                    output_key="patch_result",
                    description=prompt,
                    args={
                        "request_id": request.request_id,
                        "output_schema": request.output_schema,
                        "target_file_paths": list(runtime_file_paths),
                    },
                    dependencies=[operation.op_id for operation in read_operations],
                    externally_visible=True,
                ),
            ],
            expected=None,
            verifier_type="none",
            verification_required=verification_required,
            allow_best_effort=allow_best_effort,
            metadata={
                **_prompt_request_metadata(
                    request,
                    template_kind="bounded_repo_patch",
                    file_paths=source_file_paths,
                ),
                "input_binding_overrides": _request_file_binding_overrides(file_ref_specs),
            },
        )
    service_request = _service_request_spec(request, prompt, prompt_lower)
    if service_request is not None and _category_allowed(request.allowed_tool_categories, "service/http"):
        verification_required, allow_best_effort = _verification_policy(
            request.verification_preference,
            exact_verifier_exists=False,
        )
        return BenchmarkTask(
            task_id=f"user.{request.request_id}.service_action",
            family="e2e",
            prompt=prompt,
            task_type="bounded_service_action",
            file_paths=list(runtime_file_paths),
            context_items=list(request.context_items),
            allowed_tool_categories=_prompt_template_allowed_categories(
                request,
                ["service/http"],
            ),
            operations=[
                OperationSpec(
                    op_id="service_call",
                    kind="service_action",
                    output_key="service_result",
                    description=prompt,
                    args={
                        "request_id": request.request_id,
                        "output_schema": request.output_schema,
                        **service_request,
                    },
                    externally_visible=True,
                )
            ],
            expected=None,
            verifier_type="none",
            verification_required=verification_required,
            allow_best_effort=allow_best_effort,
            metadata=_prompt_request_metadata(
                request,
                template_kind="bounded_service_action",
                file_paths=source_file_paths,
            ),
        )
    if source_file_paths and _prompt_requests_file_inspection(prompt_lower, source_file_paths) and _category_allowed(
        request.allowed_tool_categories,
        "filesystem/read",
    ):
        verification_required, allow_best_effort = _verification_policy(
            request.verification_preference,
            exact_verifier_exists=False,
        )
        read_operations = _build_file_read_operations(file_ref_specs)
        return BenchmarkTask(
            task_id=f"user.{request.request_id}.file_inspection",
            family="mem",
            prompt=prompt,
            task_type="file_inspection",
            file_paths=list(runtime_file_paths),
            context_items=list(request.context_items),
            allowed_tool_categories=_prompt_template_allowed_categories(
                request,
                ["filesystem/read"],
            ),
            operations=[
                *read_operations,
                OperationSpec(
                    op_id="respond",
                    kind="direct_response",
                    output_key="response",
                    description=prompt,
                    args={
                        "request_id": request.request_id,
                        "output_schema": request.output_schema,
                        "file_paths": list(source_file_paths),
                    },
                    dependencies=[operation.op_id for operation in read_operations],
                    externally_visible=True,
                ),
            ],
            expected=None,
            verifier_type="none",
            verification_required=verification_required,
            allow_best_effort=allow_best_effort,
            metadata={
                **_prompt_request_metadata(
                    request,
                    template_kind="file_inspection",
                    file_paths=source_file_paths,
                ),
                "input_binding_overrides": _request_file_binding_overrides(file_ref_specs),
            },
        )
    verification_required, allow_best_effort = _verification_policy(
        request.verification_preference,
        exact_verifier_exists=False,
    )
    return BenchmarkTask(
        task_id=f"user.{request.request_id}.best_effort",
        family="e2e",
        prompt=prompt,
        task_type="direct_answer",
        file_paths=list(runtime_file_paths),
        allowed_tool_categories=list(request.allowed_tool_categories),
        context_items=list(request.context_items),
        operations=[
            OperationSpec(
                op_id="respond",
                kind="direct_response",
                output_key="response",
                description=prompt,
                args={
                    "request_id": request.request_id,
                    "output_schema": request.output_schema,
                },
                externally_visible=True,
            )
        ],
        expected=None,
        verifier_type="none",
        verification_required=verification_required,
        allow_best_effort=allow_best_effort,
        metadata=_prompt_request_metadata(
            request,
            template_kind="direct_answer",
            file_paths=source_file_paths,
        ),
    )


def compile_execution_plan_from_task(
    task: BenchmarkTask,
    *,
    request_id: str,
    seed: int,
    runtime_hash: str,
    runtime_dir: str,
    trace_context: OpenAITraceContext | None = None,
    source_suite: str | None = None,
    origin_kind: str = "benchmark",
    adapter_kind: str | None = None,
    source_request_id: str | None = None,
    budget_overrides: Mapping[str, Any] | None = None,
) -> ExecutionPlan:
    nodes, plan_constants = _compile_plan_nodes(task)
    terminal_output_keys = _task_terminal_output_keys(task)
    has_terminal_outputs = bool(terminal_output_keys)
    root_node_ids = [node.node_id for node in nodes if not node.dependencies]
    file_ref_specs = _task_file_ref_specs(task)
    if origin_kind == "benchmark" and benchmark_task_episode_kind(task) == "transfer_episode":
        episode_kind = trace_context_field(trace_context, "episode_kind") or "transfer_episode"
        episode_step_index = trace_context_field(trace_context, "episode_step_index")
        if episode_step_index is None:
            episode_step_index = benchmark_task_episode_step_index(task)
    else:
        episode_kind = None
        episode_step_index = None
    plan_trace_context = runtime_trace_context(
        trace_context,
        request_id=request_id,
        runtime_hash=runtime_hash,
        runtime_dir=runtime_dir,
        task_id=task.task_id,
        seed=seed,
        evaluation_unit_id=trace_context_field(trace_context, "evaluation_unit_id") or request_id,
        request_mode="benchmark" if origin_kind == "benchmark" else "user_request",
        episode_kind=episode_kind,
        episode_step_index=episode_step_index,
        objective=task.prompt,
    )
    exact_verifier_exists = str(task.verifier_type or "").strip().lower() not in {"", "none"}
    verification_terminal_nodes = [node.node_id for node in nodes if str(node.node_kind) == "verify"]
    if not verification_terminal_nodes:
        verification_terminal_nodes = _terminal_dependency_node_ids(
            nodes,
            [operation.output_key for operation in task.operations],
        )
    verification_plan = VerificationPlan(
        mode="benchmark" if exact_verifier_exists and has_terminal_outputs else "none",
        required=bool(task.verification_required and has_terminal_outputs),
        checker_ladder=["local", "subtree", "repo", "benchmark"],
        exact_verifier_required=bool(task.externally_visible and exact_verifier_exists and has_terminal_outputs),
        artifact_contract={
            "task_id": task.task_id,
            "task_type": task.task_type,
            "output_keys": terminal_output_keys,
            "family": task.family,
        },
        terminal_nodes=verification_terminal_nodes,
        verifier_type=task.verifier_type,
        expected=task.expected,
    )
    return ExecutionPlan(
        plan_id=f"plan.{stable_hash(request_id, task.task_id, seed)[:12]}",
        request_id=request_id,
        origin=PlanOrigin(
            origin_kind="benchmark" if origin_kind == "benchmark" else "user_request",
            source_task_id=task.task_id,
            source_request_id=source_request_id,
            source_suite=source_suite,
            adapter_kind=adapter_kind or task.task_type,
            adaptation_assumptions=list(task.metadata.get("adaptation_assumptions", [])),
        ),
        objective=task.prompt,
        context_refs=[dict(item) for item in task.context_items],
        file_refs=[file_ref.runtime_path for file_ref in file_ref_specs],
        file_ref_specs=file_ref_specs,
        plan_constants=plan_constants,
        nodes=nodes,
        root_node_ids=root_node_ids,
        terminal_output_keys=terminal_output_keys,
        verification_plan=verification_plan,
        execution_flags=ExecutionFlags(
            allow_best_effort=bool(task.allow_best_effort),
            allow_resume=True,
            allow_branching=any(str(node.node_kind) == "merge" for node in nodes),
            allow_tool_synthesis=any(str(node.node_kind) == "tool_synthesis" for node in nodes),
            allow_async_handles=True,
            requires_terminal_verification=any(str(node.node_kind) == "verify" for node in nodes),
        ),
        allowed_tool_categories=list(task.allowed_tool_categories),
        budget_overrides=dict(budget_overrides or {}),
        externally_visible=bool(task.externally_visible),
        trace_context=plan_trace_context,
    )


def _task_file_ref_specs(task: BenchmarkTask) -> list[RequestFileRef]:
    payload = task.metadata.get("request_file_refs", [])
    if isinstance(payload, list) and payload:
        return [
            (RequestFileRef).model_validate(row)
            for row in payload
            if isinstance(row, Mapping)
        ]
    return [compile_request_file_ref(path) for path in task.file_paths]


def compile_execution_plan_from_solve_request(
    solve_request: SolveRequest,
    *,
    seed: int,
    runtime_hash: str,
    runtime_dir: str,
    trace_context: OpenAITraceContext | None = None,
) -> tuple[BenchmarkTask, ExecutionPlan]:
    task = solve_request_to_task(solve_request)
    plan_trace_context = runtime_trace_context(
        trace_context,
        request_id=solve_request.request_id,
        runtime_hash=runtime_hash,
        runtime_dir=runtime_dir,
        task_id=task.task_id,
        seed=seed,
        evaluation_unit_id=getattr(trace_context, "evaluation_unit_id", None) or solve_request.request_id,
        request_mode="user_request",
        episode_kind=None,
        episode_step_index=None,
        objective=solve_request.prompt,
    )
    return (
        task,
        compile_execution_plan_from_task(
            task,
            request_id=solve_request.request_id,
            seed=seed,
            runtime_hash=runtime_hash,
            runtime_dir=runtime_dir,
            trace_context=plan_trace_context,
            origin_kind="user_request",
            adapter_kind=task.task_type,
            source_request_id=solve_request.request_id,
            budget_overrides=solve_request.budget_overrides,
        ),
    )


def prompt_mode_request_requires_default_provider(
    request: RuntimeSolveRequest,
    *,
    runtime_dir: str | Path,
    runtime_hash: str = "",
) -> bool:
    if request.mode != "user_request" or request.solve_request is None:
        return False
    _, execution_plan = compile_execution_plan_from_solve_request(
        request.solve_request,
        seed=request.seed,
        runtime_hash=runtime_hash,
        runtime_dir=str(runtime_dir),
        trace_context=request.trace_context,
    )
    return execution_plan_requires_default_provider(execution_plan)
