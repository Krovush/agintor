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

def solve_result_from_run_result(request: SolveRequest, run: RunResult, runtime_hash: str) -> SolveResult:
    return solve_result_from_run_result_with_context(
        request,
        run,
        runtime_hash,
        mode="user_request",
        provider_usage={},
    )


def _run_result_latest_checkpoint_ref(run: RunResult) -> str | None:
    ref = str(run.latest_checkpoint_ref or run.checkpoint_ref or "").strip()
    return ref or None


def _run_result_failure_kind(run: RunResult) -> str | None:
    failure_kind = str(run.failure_kind or "").strip()
    if failure_kind:
        return failure_kind
    return None


def _run_result_is_non_failing_terminal(run: RunResult) -> bool:
    if run.hard_invalid:
        return False
    lifecycle_state = str(run.run_lifecycle_state or run.lifecycle_state or "").strip().lower()
    return lifecycle_state == "completed"


def reduce_grouped_run_results(runs: Sequence[RunResult]) -> dict[str, Any]:
    if not runs:
        raise ValueError("grouped run reduction requires at least one RunResult")

    latest_checkpoint_ref: str | None = None
    first_failure_kind: str | None = None
    all_executed_members_completed = True
    any_cancelled = False

    for run in runs:
        blocked_tail_member = (
            isinstance(run.artifact, Mapping)
            and str(run.artifact.get("error", "") or "").strip() == "blocked_by_prior_episode_failure"
        )
        if blocked_tail_member:
            all_executed_members_completed = False
            continue
        checkpoint_ref = _run_result_latest_checkpoint_ref(run)
        if checkpoint_ref:
            latest_checkpoint_ref = checkpoint_ref
        lifecycle_state = str(run.run_lifecycle_state or run.lifecycle_state or "").strip().lower()
        if lifecycle_state == "cancelled":
            any_cancelled = True
            all_executed_members_completed = False
            if first_failure_kind is None:
                first_failure_kind = _run_result_failure_kind(run) or "cancelled"
            continue
        if _run_result_is_non_failing_terminal(run):
            continue
        all_executed_members_completed = False
        if first_failure_kind is None:
            first_failure_kind = _run_result_failure_kind(run)

    if any_cancelled:
        lifecycle_state = "cancelled"
    else:
        lifecycle_state = "completed" if all_executed_members_completed else ("paused" if latest_checkpoint_ref else "failed")
    return {
        "lifecycle_state": lifecycle_state,
        "latest_checkpoint_ref": latest_checkpoint_ref,
        "failure_kind": first_failure_kind,
        "resumable": bool(latest_checkpoint_ref),
        "prune_eligible": lifecycle_state == "failed" and not latest_checkpoint_ref,
    }


def solve_result_from_run_result_with_context(
    request: SolveRequest,
    run: RunResult,
    runtime_hash: str,
    *,
    mode: str,
    provider_usage: dict[str, Any],
) -> SolveResult:
    trace_rows = run.trace_rows()
    checks = [
        {
            "checker": row.get("checker"),
            "passed": row.get("passed"),
        }
        for row in trace_rows
        if row.get("event") == "check_result"
    ]
    benchmark_checks = [check for check in checks if check.get("checker") == "benchmark"]
    verified = run.verifier_score >= 1.0 and not run.hard_invalid
    controlled_failure = isinstance(run.artifact, dict) and run.artifact.get("error") == "controlled_failure"
    exact_verifier_failed = bool(benchmark_checks) and not verified and not controlled_failure and not run.hard_invalid
    partially_checked = bool(checks) and not benchmark_checks and not verified and not controlled_failure and not run.hard_invalid
    best_effort = not partially_checked and not exact_verifier_failed and not verified and not controlled_failure and not run.hard_invalid
    lifecycle_state = str(run.run_lifecycle_state or run.lifecycle_state or "").strip().lower()
    if lifecycle_state == "cancelled":
        status = "failed"
        verification_status = "failed"
        summary = "The runtime was cancelled before reaching a valid terminal artifact."
        best_effort = False
    elif run.hard_invalid:
        status = "failed"
        verification_status = "failed"
        summary = run.invalid_reason or "runtime execution failed"
    elif controlled_failure:
        status = "controlled_failure"
        verification_status = "required_but_unverified"
        summary = "No verified terminal artifact was available under the task contract."
    elif verified:
        status = "verified"
        verification_status = "verified"
        summary = "The runtime produced a verified artifact."
    elif exact_verifier_failed:
        status = "unverified"
        verification_status = "exact_verifier_failed"
        summary = "The runtime produced an artifact, but the exact verifier rejected it."
    elif partially_checked:
        status = "partially_checked"
        verification_status = "partially_checked"
        summary = "The runtime produced an artifact with non-benchmark checks but no exact verifier."
    else:
        status = "best_effort"
        verification_status = "best_effort"
        summary = "The runtime produced a best-effort artifact without exact verification."
    latest_checkpoint_ref = run.latest_checkpoint_ref or run.checkpoint_ref
    return SolveResult(
        request_id=request.request_id,
        runtime_hash=runtime_hash,
        run_id=run.run_id,
        run_root=run.run_root,
        attempt_id=run.attempt_id,
        latest_checkpoint_ref=latest_checkpoint_ref,
        run_lifecycle_state=run.run_lifecycle_state,
        run_resumable=run.run_resumable,
        run_prune_eligible=run.run_prune_eligible,
        mode=mode,
        artifact=run.artifact,
        status=status,
        verification_status=verification_status,
        summary=summary,
        checks=checks,
        trace_ref=run.trace_ref(),
        checkpoint_ref=latest_checkpoint_ref,
        budget={
            "cost": run.cost,
            "latency": run.latency,
            "model_calls": run.model_calls,
            "checks_used": run.checks_used,
            "tokens_used": run.tokens_used,
            "input_tokens": run.input_tokens,
            "output_tokens": run.output_tokens,
        },
        provider_usage=dict(provider_usage),
        faults={
            "count": run.faults,
            "hard_invalid": run.hard_invalid,
            "invalid_reason": run.invalid_reason,
            "failure_kind": run.failure_kind,
        },
        verified=verified,
        best_effort=best_effort,
    )
