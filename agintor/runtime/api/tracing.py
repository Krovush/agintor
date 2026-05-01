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

_PROVIDER_IDEMPOTENCY_TRACE_FIELDS = frozenset(
    {
        "task_id",
        "seed",
        "evaluation_unit_id",
        "request_mode",
        "episode_kind",
        "episode_step_index",
        "worker_id",
        "op_id",
    }
)


_RESUME_TRACE_CONTEXT_OVERRIDE_FIELDS = frozenset(
    {
        "session_id",
        "provider_role",
        "build_id",
        "runtime_hash",
        "runtime_dir",
        "runtime_session_id",
        "runtime_message_id",
        "runtime_message_index",
        "task_id",
        "seed",
        "evaluation_unit_id",
        "request_mode",
        "episode_kind",
        "episode_step_index",
    }
)


def normalize_benchmark_request_id(task_id: str, seed: int, *, duplicate_ordinal: int | None = None) -> str:
    base = f"benchmark.{task_id}.seed_{seed}"
    if duplicate_ordinal is None or duplicate_ordinal <= 0:
        return base
    return f"{base}.dup_{duplicate_ordinal:02d}"


def evaluation_unit_id_for_invocation(
    task: BenchmarkTask,
    seed: int,
    *,
    duplicate_ordinal: int | None = None,
    episode_kind: str | None = None,
) -> str:
    resolved_episode_kind = str(episode_kind or "").strip()
    episode_id = str(task.episode_id or "").strip()
    if not resolved_episode_kind and task.transfer_scored and episode_id:
        resolved_episode_kind = "transfer_episode"
    if resolved_episode_kind == "transfer_episode" and episode_id:
        return f"episode.{episode_id}.seed_{int(seed)}"
    return normalize_benchmark_request_id(task.task_id, seed, duplicate_ordinal=duplicate_ordinal)


def benchmark_task_episode_kind(task: BenchmarkTask) -> str | None:
    return "transfer_episode" if task.transfer_scored and str(task.episode_id or "").strip() else None


def benchmark_task_episode_step_index(task: BenchmarkTask) -> int | None:
    if benchmark_task_episode_kind(task) != "transfer_episode":
        return None
    return int(getattr(task, "episode_order", 0) or 0)


def trace_context_field(parent: OpenAITraceContext | None, field_name: str) -> Any:
    if parent is None:
        return None
    return getattr(parent, field_name, None)


def _trace_context_subset(
    trace_context: OpenAITraceContext | Mapping[str, Any] | None,
    allowed_fields: frozenset[str],
) -> dict[str, Any]:
    if trace_context is None:
        return {}
    payload = trace_context.model_dump() if hasattr(trace_context, "model_dump") else dict(trace_context)
    return {
        str(key): value
        for key, value in payload.items()
        if key in allowed_fields and value is not None and not (isinstance(value, list) and not value)
    }


def batch_evaluation_unit_key(invocation: RuntimeTaskInvocation) -> str:
    episode_kind = str(getattr(invocation, "episode_kind", "") or "").strip()
    if episode_kind == "transfer_episode":
        explicit = str(getattr(invocation, "evaluation_unit_id", "") or "").strip()
        if explicit:
            return explicit
        return evaluation_unit_id_for_invocation(
            invocation.task,
            invocation.seed,
            episode_kind=episode_kind,
        )
    request_id = str(getattr(invocation, "request_id", "") or "").strip()
    if request_id:
        return request_id
    explicit = str(getattr(invocation, "evaluation_unit_id", "") or "").strip()
    if explicit:
        return explicit
    return evaluation_unit_id_for_invocation(
        invocation.task,
        invocation.seed,
        episode_kind=episode_kind,
    )


def build_trace_context(
    *,
    provider_role: str,
    request_id: str,
    runtime_hash: str | None = None,
    runtime_dir: str | None = None,
    task_id: str | None = None,
    seed: int | None = None,
    evaluation_unit_id: str | None = None,
    request_mode: str | None = None,
    episode_kind: str | None = None,
    episode_step_index: int | None = None,
    objective: str | None = None,
    session_id: str | None = None,
    build_id: str | None = None,
    factory_chat_id: str | None = None,
    factory_message_id: str | None = None,
    factory_message_index: int | None = None,
    runtime_session_id: str | None = None,
    runtime_message_id: str | None = None,
    runtime_message_index: int | None = None,
) -> OpenAITraceContext:
    return OpenAITraceContext(
        session_id=session_id,
        provider_role=provider_role,
        build_id=build_id,
        runtime_hash=runtime_hash,
        runtime_dir=runtime_dir,
        task_id=task_id,
        seed=seed,
        request_id=request_id,
        evaluation_unit_id=evaluation_unit_id,
        request_mode=request_mode,
        episode_kind=episode_kind,
        episode_step_index=episode_step_index,
        factory_chat_id=factory_chat_id,
        factory_message_id=factory_message_id,
        factory_message_index=factory_message_index,
        runtime_session_id=runtime_session_id,
        runtime_message_id=runtime_message_id,
        runtime_message_index=runtime_message_index,
        objective=objective,
    )


def derive_trace_context(parent: OpenAITraceContext | None, **updates: Any) -> OpenAITraceContext:
    payload = (parent).model_dump() if parent is not None else {}
    for key, value in updates.items():
        if value is not None:
            payload[key] = value
    return OpenAITraceContext(**payload)


def runtime_trace_context(
    parent: OpenAITraceContext | None = None,
    *,
    request_id: str,
    runtime_hash: str | None = None,
    runtime_dir: str | None = None,
    task_id: str | None = None,
    seed: int | None = None,
    evaluation_unit_id: str | None = None,
    request_mode: str | None = None,
    episode_kind: str | None = None,
    episode_step_index: int | None = None,
    objective: str | None = None,
    factory_chat_id: str | None = None,
    factory_message_id: str | None = None,
    factory_message_index: int | None = None,
    runtime_session_id: str | None = None,
    runtime_message_id: str | None = None,
    runtime_message_index: int | None = None,
) -> OpenAITraceContext:
    context = derive_trace_context(
        parent,
        session_id=resolve_trace_session_id(trace_context_field(parent, "session_id")),
        provider_role="runtime",
        request_id=request_id,
        runtime_hash=runtime_hash,
        runtime_dir=runtime_dir,
        task_id=task_id,
        seed=seed,
        evaluation_unit_id=evaluation_unit_id,
        request_mode=request_mode,
        episode_kind=episode_kind,
        episode_step_index=episode_step_index,
        objective=objective,
        factory_chat_id=factory_chat_id,
        factory_message_id=factory_message_id,
        factory_message_index=factory_message_index,
        runtime_session_id=runtime_session_id,
        runtime_message_id=runtime_message_id,
        runtime_message_index=runtime_message_index,
    )
    if str(episode_kind or "").strip() != "transfer_episode":
        context = context.model_copy(update={"episode_kind": None, "episode_step_index": None})
    return context
