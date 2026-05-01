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

from .request_loading import benchmark_task_to_solve_request
from .tracing import (
    _RESUME_TRACE_CONTEXT_OVERRIDE_FIELDS,
    _trace_context_subset,
)

def resume_task_and_plan_from_checkpoint(
    envelope: CheckpointEnvelope,
) -> tuple[BenchmarkTask, ExecutionPlan]:
    task = (BenchmarkTask).model_validate(envelope.task_payload)
    plan = (ExecutionPlan).model_validate(envelope.plan_snapshot)
    return task, plan


def _trace_context_override_payload(trace_context: OpenAITraceContext | Mapping[str, Any] | None) -> dict[str, Any]:
    return _trace_context_subset(trace_context, _RESUME_TRACE_CONTEXT_OVERRIDE_FIELDS)


def _rebound_trace_context_payload(
    payload: Mapping[str, Any] | None,
    active_request_id: str,
    trace_context: OpenAITraceContext | Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping) and trace_context is None:
        return None
    rebound = dict(payload or {})
    rebound.update(_trace_context_override_payload(trace_context))
    rebound["request_id"] = active_request_id
    return rebound


def _refresh_plan_digest(plan_snapshot: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(plan_snapshot)
    payload["plan_digest"] = ""
    return (ExecutionPlan).model_validate(payload).model_dump()


def _rebind_request_id_mirrors(payload: Any, active_request_id: str) -> Any:
    if isinstance(payload, Mapping):
        rebound: dict[str, Any] = {}
        for key, value in payload.items():
            if str(key) == "request_id":
                rebound[str(key)] = active_request_id
            else:
                rebound[str(key)] = _rebind_request_id_mirrors(value, active_request_id)
        return rebound
    if isinstance(payload, list):
        return [_rebind_request_id_mirrors(item, active_request_id) for item in payload]
    return payload


def _rebind_frame_snapshot_request_id(
    frame_payload: Mapping[str, Any] | None,
    active_request_id: str,
    trace_context: OpenAITraceContext | Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not isinstance(frame_payload, Mapping):
        return None
    rebound = dict(frame_payload)
    rebound["request_id"] = active_request_id
    rebound["trace_context"] = _rebound_trace_context_payload(
        rebound.get("trace_context"),
        active_request_id,
        trace_context,
    )
    return rebound


def _rebind_branch_publication_request_id(
    publication_payload: Mapping[str, Any] | None,
    active_request_id: str,
    trace_context: OpenAITraceContext | Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not isinstance(publication_payload, Mapping):
        return None
    rebound = dict(publication_payload)
    rebound["trace_context"] = _rebound_trace_context_payload(
        rebound.get("trace_context"),
        active_request_id,
        trace_context,
    )
    return rebound


def _rebind_branch_state_request_id(
    branch_state_payload: Mapping[str, Any] | None,
    active_request_id: str,
    trace_context: OpenAITraceContext | Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not isinstance(branch_state_payload, Mapping):
        return None
    rebound = dict(branch_state_payload)
    rebound["publications"] = [
        _rebind_branch_publication_request_id(payload, active_request_id, trace_context)
        for payload in rebound.get("publications", [])
        if payload is not None
    ]
    return rebound


def _rebind_branch_resume_snapshot_request_id(
    snapshot_payload: Mapping[str, Any] | None,
    active_request_id: str,
    trace_context: OpenAITraceContext | Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not isinstance(snapshot_payload, Mapping):
        return None
    rebound = dict(snapshot_payload)
    branch_plan = dict(rebound.get("branch_plan") or {})
    branch_plan["request_id"] = active_request_id
    branch_plan["trace_context"] = _rebound_trace_context_payload(
        branch_plan.get("trace_context"),
        active_request_id,
        trace_context,
    )
    rebound["branch_plan"] = branch_plan
    rebound["active_frame"] = _rebind_frame_snapshot_request_id(
        rebound.get("active_frame"),
        active_request_id,
        trace_context,
    )
    rebound["queued_frames"] = [
        _rebind_frame_snapshot_request_id(frame_payload, active_request_id, trace_context)
        for frame_payload in rebound.get("queued_frames", [])
        if frame_payload is not None
    ]
    rebound["branch_publications"] = [
        _rebind_branch_publication_request_id(publication_payload, active_request_id, trace_context)
        for publication_payload in rebound.get("branch_publications", [])
        if publication_payload is not None
    ]
    rebound["side_effect_receipts"] = [
        _rebind_side_effect_receipt_request_id(receipt_payload, active_request_id, trace_context)
        for receipt_payload in rebound.get("side_effect_receipts", [])
        if receipt_payload is not None
    ]
    return rebound


def _rebind_side_effect_receipt_request_id(
    receipt_payload: Mapping[str, Any] | None,
    active_request_id: str,
    trace_context: OpenAITraceContext | Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not isinstance(receipt_payload, Mapping):
        return None
    rebound = dict(receipt_payload)
    rebound["request_id"] = active_request_id
    rebound["trace_context"] = _rebound_trace_context_payload(
        rebound.get("trace_context"),
        active_request_id,
        trace_context,
    )
    return rebound


def rebind_checkpoint_envelope_for_resume(
    envelope: CheckpointEnvelope,
    *,
    active_request_id: str,
    source_checkpoint_ref: str | None = None,
    trace_context: OpenAITraceContext | Mapping[str, Any] | None = None,
) -> CheckpointEnvelope:
    payload = ((envelope).model_copy(deep=True)).model_dump()
    original_request_id = (
        str(payload.get("origin_request_id") or payload.get("request_id") or "").strip()
        or str(envelope.request_id)
    )
    payload["request_id"] = active_request_id
    payload["origin_request_id"] = original_request_id
    selected_source_checkpoint_ref = (
        str(source_checkpoint_ref or payload.get("source_checkpoint_ref") or "").strip() or None
    )
    payload["selected_checkpoint_ref"] = selected_source_checkpoint_ref
    payload["source_checkpoint_ref"] = selected_source_checkpoint_ref
    plan_snapshot = dict(payload.get("plan_snapshot") or {})
    plan_snapshot["request_id"] = active_request_id
    plan_snapshot["trace_context"] = _rebound_trace_context_payload(
        plan_snapshot.get("trace_context"),
        active_request_id,
        trace_context,
    )
    plan_snapshot = _refresh_plan_digest(plan_snapshot)
    payload["plan_snapshot"] = plan_snapshot
    runtime_state_snapshot = dict(payload.get("runtime_state_snapshot") or {})
    runtime_state_snapshot["request_id"] = active_request_id
    if selected_source_checkpoint_ref is not None:
        runtime_state_snapshot["latest_checkpoint_ref"] = selected_source_checkpoint_ref
    runtime_state_snapshot["active_frame"] = _rebind_frame_snapshot_request_id(
        runtime_state_snapshot.get("active_frame"),
        active_request_id,
        trace_context,
    )
    runtime_state_snapshot["queued_frames"] = [
        _rebind_frame_snapshot_request_id(frame_payload, active_request_id, trace_context)
        for frame_payload in runtime_state_snapshot.get("queued_frames", [])
        if frame_payload is not None
    ]
    runtime_state_snapshot["branch_states"] = {
        str(key): _rebind_branch_state_request_id(value, active_request_id, trace_context)
        for key, value in dict(runtime_state_snapshot.get("branch_states", {})).items()
        if value is not None
    }
    runtime_state_snapshot["branch_publications"] = [
        _rebind_branch_publication_request_id(publication_payload, active_request_id, trace_context)
        for publication_payload in runtime_state_snapshot.get("branch_publications", [])
        if publication_payload is not None
    ]
    runtime_state_snapshot["branch_resume_snapshots"] = {
        str(key): _rebind_branch_resume_snapshot_request_id(value, active_request_id, trace_context)
        for key, value in dict(runtime_state_snapshot.get("branch_resume_snapshots", {})).items()
        if value is not None
    }
    payload["runtime_state_snapshot"] = runtime_state_snapshot
    side_effect_ledger = dict(payload.get("side_effect_ledger") or {})
    side_effect_ledger["receipts"] = [
        _rebind_side_effect_receipt_request_id(receipt_payload, active_request_id, trace_context)
        for receipt_payload in side_effect_ledger.get("receipts", [])
        if receipt_payload is not None
    ]
    payload["side_effect_ledger"] = side_effect_ledger
    payload.pop("working_state_summary", None)
    payload["working_state"] = _rebind_request_id_mirrors(
        payload.get("working_state", {}),
        active_request_id,
    )
    trace_cursor = _rebind_request_id_mirrors(
        payload.get("trace_cursor", {}),
        active_request_id,
    )
    if isinstance(trace_cursor, Mapping):
        trace_cursor = dict(trace_cursor)
        if str(trace_cursor.get("last_solve_request_id") or "").strip() in {"", original_request_id}:
            trace_cursor["last_solve_request_id"] = active_request_id
    payload["trace_cursor"] = trace_cursor
    return (CheckpointEnvelope).model_validate(payload)


def solve_request_from_resume_checkpoint(
    envelope: CheckpointEnvelope,
    *,
    request_id_override: str | None = None,
    request_bundle: Mapping[str, Any] | None = None,
    source_checkpoint_ref: str | None = None,
    trace_context: OpenAITraceContext | Mapping[str, Any] | None = None,
) -> tuple[SolveRequest, CheckpointEnvelope, str]:
    effective_request_id = str(request_id_override or envelope.request_id).strip() or envelope.request_id
    rebound_envelope = rebind_checkpoint_envelope_for_resume(
        envelope,
        active_request_id=effective_request_id,
        source_checkpoint_ref=source_checkpoint_ref,
        trace_context=trace_context,
    )
    task, plan = resume_task_and_plan_from_checkpoint(rebound_envelope)
    bundle = dict(request_bundle or {})
    request_kind = str(bundle.get("request_kind", "") or "").strip()
    if request_kind and request_kind not in {"runtime_solve_request", "runtime_task_invocation", "runtime_task_invocation_group"}:
        raise ValueError(f"resume encountered unknown durable request envelope kind {request_kind!r}")
    if plan.origin.origin_kind == "benchmark":
        return benchmark_task_to_solve_request(task, request_id=effective_request_id), rebound_envelope, effective_request_id

    payload = bundle.get("payload")
    if bundle.get("request_kind") == "runtime_solve_request" and isinstance(payload, Mapping):
        original_request = (RuntimeSolveRequest).model_validate(dict(payload))
        if original_request.mode == "user_request" and original_request.solve_request is not None:
            solve_request = (SolveRequest).model_validate((original_request.solve_request).model_dump())
            return (solve_request).model_copy(update={"request_id": effective_request_id}), rebound_envelope, effective_request_id

    raise ValueError(
        "resume for user_request checkpoints requires the stored runtime_solve_request envelope with solve_request payload"
    )
