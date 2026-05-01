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

def runtime_solve_failure_response(
    request: SolveRequest,
    runtime_hash: str,
    capability_exchange: CapabilityExchange,
    *,
    mode: str,
    summary: str,
    provider_usage: dict[str, Any] | None = None,
    fault_code: str = "solve_failure",
    run_id: str = "",
    run_root: str = "",
    attempt_id: str = "",
    latest_checkpoint_ref: str | None = None,
) -> RuntimeSolveResponse:
    return RuntimeSolveResponse(
        request_id=request.request_id,
        capability_exchange=capability_exchange,
        solve_result=SolveResult(
            request_id=request.request_id,
            runtime_hash=runtime_hash,
            run_id=run_id,
            run_root=run_root,
            attempt_id=attempt_id,
            latest_checkpoint_ref=latest_checkpoint_ref,
            run_lifecycle_state="failed",
            run_resumable=bool(latest_checkpoint_ref),
            run_prune_eligible=not bool(latest_checkpoint_ref),
            mode=mode,
            artifact={"error": fault_code, "message": summary},
            status="failed",
            verification_status="failed",
            summary=summary,
            checks=[],
            budget={},
            provider_usage=dict(provider_usage or {}),
            faults={
                "count": 1,
                "hard_invalid": False,
                "invalid_reason": summary,
                "code": fault_code,
                "contract_error": True,
            },
            verified=False,
            best_effort=False,
        ),
    )
