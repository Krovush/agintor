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

from .request_loading import (
    _compiled_request_file_refs,
    _request_file_source_paths,
)
from .results import _run_result_failure_kind
from .tracing import (
    benchmark_task_episode_kind,
    benchmark_task_episode_step_index,
    evaluation_unit_id_for_invocation,
    normalize_benchmark_request_id,
    runtime_trace_context,
    trace_context_field,
)

def synthesize_blocked_episode_run(
    invocation: RuntimeTaskInvocation,
    *,
    run_id: str,
    run_root: str,
    attempt_id: str,
    blocking_run: RunResult,
) -> RunResult:
    return RunResult(
        request_id=invocation.request_id,
        plan_id="",
        run_id=run_id,
        run_root=run_root,
        attempt_id=attempt_id,
        runtime_hash=blocking_run.runtime_hash,
        task_id=invocation.task.task_id,
        seed=invocation.seed,
        artifact={
            "error": "blocked_by_prior_episode_failure",
            "blocked_by_request_id": blocking_run.request_id,
            "blocking_failure_kind": _run_result_failure_kind(blocking_run),
        },
        verifier_score=0.0,
        cost=0.0,
        latency=0.0,
        faults=0,
        trace=[],
        trace_context=invocation.trace_context,
        hard_invalid=False,
        invalid_reason=None,
        failure_kind="blocked_by_prior_episode_failure",
        mode="benchmark",
        lifecycle_state="blocked",
        provider_usage={},
        runtime_backend=invocation.runtime_backend,
    )


def inspect_request_for_runtime(
    *,
    request_id: str,
    requested_backend: str,
    runtime_contract_version: str,
) -> InspectRequest:
    return InspectRequest(
        request_id=request_id,
        requested_backend=requested_backend,
        expected_runtime_contract_version=runtime_contract_version,
    )


def runtime_solve_request_for_task(
    *,
    runtime_backend: str,
    seed: int,
    task: BenchmarkTask,
    budget_overrides: dict[str, Any] | None = None,
    request_id: str | None = None,
    trace_context: OpenAITraceContext | None = None,
) -> RuntimeSolveRequest:
    normalized_request_id = request_id or normalize_benchmark_request_id(task.task_id, seed)
    if benchmark_task_episode_kind(task) == "transfer_episode":
        episode_kind = trace_context_field(trace_context, "episode_kind") or "transfer_episode"
        episode_step_index = trace_context_field(trace_context, "episode_step_index")
        if episode_step_index is None:
            episode_step_index = benchmark_task_episode_step_index(task)
    else:
        episode_kind = None
        episode_step_index = None
    evaluation_unit_id = (
        trace_context_field(trace_context, "evaluation_unit_id")
        or evaluation_unit_id_for_invocation(task, seed, episode_kind=episode_kind)
    )
    return RuntimeSolveRequest(
        request_id=normalized_request_id,
        evaluation_unit_id=evaluation_unit_id,
        runtime_backend=runtime_backend,
        mode="benchmark",
        seed=int(seed),
        task=task,
        budget_overrides=dict(budget_overrides or {}),
        trace_context=runtime_trace_context(
            trace_context,
            request_id=normalized_request_id,
            evaluation_unit_id=evaluation_unit_id,
            request_mode="benchmark",
            episode_kind=episode_kind,
            episode_step_index=episode_step_index,
            task_id=task.task_id,
            seed=seed,
            objective=task.prompt,
        ),
    )


def runtime_solve_request_for_user_request(
    *,
    runtime_backend: str,
    seed: int,
    solve_request: SolveRequest,
    trace_context: OpenAITraceContext | None = None,
    runtime_session_id: str | None = None,
    runtime_message_id: str | None = None,
    runtime_message_index: int | None = None,
    session_seed: RuntimeSessionSeed | None = None,
) -> RuntimeSolveRequest:
    effective_solve_request = solve_request
    if not solve_request.request_file_refs:
        effective_solve_request = solve_request.model_copy(
            update={
                "request_file_refs": _compiled_request_file_refs(solve_request),
                "file_paths": _request_file_source_paths(solve_request),
            }
        )
    if session_seed is not None and runtime_session_id is None:
        runtime_session_id = session_seed.session_id
    if session_seed is not None and runtime_message_index is None:
        runtime_message_index = session_seed.message_index
    return RuntimeSolveRequest(
        request_id=effective_solve_request.request_id,
        evaluation_unit_id=effective_solve_request.request_id,
        runtime_backend=runtime_backend,
        mode="user_request",
        seed=int(seed),
        solve_request=effective_solve_request,
        session_seed=session_seed,
        budget_overrides=dict(effective_solve_request.budget_overrides),
        trace_context=runtime_trace_context(
            trace_context,
            request_id=effective_solve_request.request_id,
            evaluation_unit_id=effective_solve_request.request_id,
            request_mode="user_request",
            seed=seed,
            objective=effective_solve_request.prompt,
            runtime_session_id=runtime_session_id,
            runtime_message_id=runtime_message_id,
            runtime_message_index=runtime_message_index,
        ),
    )


def runtime_batch_request_for_tasks(
    *,
    request_id: str,
    runtime_backend: str,
    task_runs: list[tuple[BenchmarkTask, int]],
    budget_overrides: dict[str, Any] | None = None,
    trace_context: OpenAITraceContext | None = None,
) -> RuntimeBatchRequest:
    duplicate_counts: dict[tuple[str, int], int] = {}
    total_counts: dict[tuple[str, int], int] = {}
    for task, raw_seed in task_runs:
        key = (task.task_id, int(raw_seed))
        total_counts[key] = total_counts.get(key, 0) + 1
    invocations: list[RuntimeTaskInvocation] = []
    for task, raw_seed in task_runs:
        seed = int(raw_seed)
        duplicate_key = (task.task_id, seed)
        episode_id = str(task.episode_id or "").strip()
        if task.transfer_scored and episode_id:
            episode_kind = "transfer_episode"
            request_key = normalize_benchmark_request_id(task.task_id, seed)
            evaluation_unit_id = evaluation_unit_id_for_invocation(
                task,
                seed,
                episode_kind=episode_kind,
            )
            episode_step_index = int(getattr(task, "episode_order", 0) or 0)
        else:
            episode_kind = None
            duplicate_counts[duplicate_key] = duplicate_counts.get(duplicate_key, 0) + 1
            duplicate_ordinal = duplicate_counts[duplicate_key] - 1
            request_key = normalize_benchmark_request_id(
                task.task_id,
                seed,
                duplicate_ordinal=duplicate_ordinal or None,
            )
            evaluation_unit_id = request_key
            episode_step_index = None
        invocations.append(
            RuntimeTaskInvocation(
                request_id=request_key,
                evaluation_unit_id=evaluation_unit_id,
                episode_kind=episode_kind,
                episode_step_index=episode_step_index,
                runtime_backend=runtime_backend,
                seed=seed,
                task=task,
                trace_context=runtime_trace_context(
                    trace_context,
                    request_id=request_key,
                    evaluation_unit_id=evaluation_unit_id,
                    request_mode="benchmark",
                    episode_kind=episode_kind,
                    episode_step_index=episode_step_index,
                    task_id=task.task_id,
                    seed=seed,
                    objective=task.prompt,
                ),
            )
        )
    return RuntimeBatchRequest(
        request_id=request_id,
        runtime_backend=runtime_backend,
        budget_overrides=dict(budget_overrides or {}),
        invocations=invocations,
        trace_context=runtime_trace_context(
            trace_context,
            request_id=request_id,
            evaluation_unit_id=request_id,
            request_mode="batch",
        ),
    )
