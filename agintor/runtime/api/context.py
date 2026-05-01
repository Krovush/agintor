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

from .tracing import (
    _PROVIDER_IDEMPOTENCY_TRACE_FIELDS,
    _trace_context_subset,
    derive_trace_context,
)

@dataclass(frozen=True)
class PromptCompilation:
    task: BenchmarkTask
    adapter_kind: str


@dataclass
class AgentFrame:
    frame_id: str
    agent: AgentTemplate
    request_id: str
    plan_id: str
    objective: str
    operation_ids: list[str]
    depth: int
    checkpoint: Checkpoint | None = None
    parent_id: str | None = None
    worker_id: str | None = None
    role: str = "root"
    tool_scope: list[str] = field(default_factory=list)
    model_class: str = "small"
    branch_group_id: str | None = None
    trace_context: OpenAITraceContext | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RuntimeBudget:
    cost: float = 0.0
    latency: float = 0.0
    calls: int = 0
    checks: int = 0
    tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    C_max: float = 100.0
    L_max: float = 120.0
    M_max: int = 64
    Q_max: int = 16
    context_window_tokens: int = 768

    def normalized(self) -> dict[str, float]:
        return {
            "cost": self.cost / max(1.0, self.C_max),
            "latency": self.latency / max(1.0, self.L_max),
            "calls": self.calls / max(1, self.M_max),
            "checks": self.checks / max(1, self.Q_max),
        }

    def exhausted(self) -> bool:
        n = self.normalized()
        return any(value >= 1.0 for value in n.values())

    def remaining_model_calls(self) -> int:
        return max(0, int(self.M_max - self.calls))

    def remaining_checks(self) -> int:
        return max(0, int(self.Q_max - self.checks))

    def remaining_latency(self) -> float:
        return max(0.0, float(self.L_max - self.latency))

    def consume_model_response(self, response: ModelResponse) -> None:
        self.calls += 1
        self.cost += float(response.dollar_cost)
        self.latency += float(response.latency_s)
        self.input_tokens += int(response.input_tokens)
        self.output_tokens += int(response.output_tokens)
        if response.token_estimate > 0:
            self.tokens += int(response.token_estimate)
        else:
            self.tokens += int(response.input_tokens) + int(response.output_tokens)

    def consume_check(self, count: int = 1, latency_s: float = 0.0) -> None:
        self.checks += int(count)
        self.latency += float(latency_s)

    def consume_tool_latency(self, latency_s: float) -> None:
        self.latency += float(latency_s)


@dataclass
class RuntimeState:
    request_id: str = ""
    plan_id: str = ""
    execution_state: str = "idle"
    active_branch_count: int = 0
    checkpoint_sequence_no: int = 0
    event_sequence_no: int = 0
    event_sequence_start: int = 0
    queue: list[AgentFrame] = field(default_factory=list)
    visible_tool_names: list[str] = field(default_factory=list)
    unresolved_goals: list[str] = field(default_factory=list)
    confidence: float = 0.0
    mode: str | None = None
    created_tools: int = 0
    promoted_nodes: int = 0
    checks_used: int = 0
    interface_usage: dict[str, float] = field(default_factory=lambda: {"top": 0.0, "mem": 0.0, "tool": 0.0, "ctl": 0.0})
    artifacts: dict[str, Any] = field(default_factory=dict)
    checkpoints: dict[str, Checkpoint] = field(default_factory=dict)
    worker_plans: dict[str, dict[str, Any]] = field(default_factory=dict)
    open_handle_ids: list[str] = field(default_factory=list)
    plan_node_status: dict[str, str] = field(default_factory=dict)
    branch_states: dict[str, dict[str, Any]] = field(default_factory=dict)
    branch_publications: list[dict[str, Any]] = field(default_factory=list)
    branch_resume_snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)
    side_effect_receipts: list[dict[str, Any]] = field(default_factory=list)
    latest_checkpoint_ref: str | None = None
    subgoal_negative_steps: dict[str, int] = field(default_factory=dict)
    subgoal_last_model: dict[str, str] = field(default_factory=dict)
    last_unresolved_goal: str | None = None


@dataclass
class PolicyContext:
    runtime_dir: Path
    shell: Any
    task: BenchmarkTask
    request_id: str
    plan: ExecutionPlan
    trace_context: OpenAITraceContext
    provider: ModelProvider
    seed: int
    state: RuntimeState
    budget: RuntimeBudget
    trace: list[dict[str, Any]]
    objective: str
    profile: RuntimeProfile | None = None
    runtime_backend: str = "local"
    side_effect_callback: Any | None = None
    checkpoint_callback: Any | None = None
    active_frame: Any | None = None
    cancellation_event: Any | None = None

    def __post_init__(self) -> None:
        if self.profile is None:
            self.profile = default_runtime_profile()

    def record(self, event: str, **payload: Any) -> None:
        event_trace_context = (
            payload.pop("trace_context", None)
            or getattr(self.active_frame, "trace_context", None)
            or self.trace_context
        )
        frame_id = payload.pop("frame_id", None) or getattr(self.active_frame, "frame_id", None)
        branch_id = (
            payload.pop("branch_id", None)
            or getattr(self.active_frame, "worker_id", None)
            or getattr(event_trace_context, "worker_id", None)
        )
        node_id = payload.pop("node_id", None)
        execution_state = str(payload.pop("execution_state", self.state.execution_state) or self.state.execution_state)
        runtime_event = RuntimeEvent(
            event=event,
            event_id=f"runtime-event.{stable_hash(self.request_id, self.plan.plan_id, event, frame_id, branch_id, node_id, now_ts())[:12]}",
            created_at=now_ts(),
            execution_state=execution_state,
            request_id=self.request_id,
            plan_id=self.plan.plan_id,
            trace_context=event_trace_context,
            frame_id=str(frame_id).strip() or None,
            branch_id=str(branch_id).strip() or None,
            node_id=str(node_id).strip() or None,
            payload={"runtime_backend": self.runtime_backend, **payload},
        )
        if hasattr(self.shell, "append_runtime_event"):
            runtime_event = self.shell.append_runtime_event(runtime_event)
        else:
            runtime_event = (runtime_event).model_copy(update={"sequence_no": int(self.state.event_sequence_no or 0) + 1}, deep=True)
        self.state.event_sequence_no = max(int(self.state.event_sequence_no or 0), int(runtime_event.sequence_no or 0))
        self.trace.append(runtime_event.trace_row())

    def consume_model_response(self, response: ModelResponse, purpose: str) -> None:
        self.budget.consume_model_response(response)
        trace_call_id = str(response.trace_call_id or response.raw.get("trace_call_id") or "").strip()
        event_payload: dict[str, Any] = {
            "purpose": purpose,
            "model_class": response.model_name,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "total_tokens": response.token_estimate,
            "dollar_cost": response.dollar_cost,
            "latency_s": response.latency_s,
        }
        if trace_call_id:
            event_payload["trace_call_id"] = trace_call_id
        self.record(
            "model_response",
            **event_payload,
        )

    def derive_trace_context(self, **updates: Any) -> OpenAITraceContext:
        return derive_trace_context(self.trace_context, **updates)

    def build_model_request(
        self,
        *,
        instructions: str,
        prompt: str,
        model_class: str,
        purpose: str,
        payload: Optional[dict[str, Any]] = None,
        trace_context: OpenAITraceContext | None = None,
    ) -> ModelRequest:
        effective_trace_context = trace_context or self.trace_context
        return ModelRequest(
            instructions=instructions,
            prompt=prompt,
            model_class=model_class,
            seed=self.seed,
            metadata={
                "mode": purpose,
                "payload": dict(payload or {}),
                "trace_context": (effective_trace_context).model_dump(),
            },
        )

    def record_side_effect(self, receipt: SideEffectReceipt) -> None:
        self.state.side_effect_receipts.append((receipt).model_dump())
        self.record(
            "side_effect_recorded",
            side_effect_id=receipt.side_effect_id,
            action_kind=receipt.action_kind,
            status=receipt.status,
            branch_id=receipt.branch_id,
        )
        if callable(self.side_effect_callback):
            self.side_effect_callback(receipt)

    def publish_checkpoint_boundary(self, boundary: str) -> None:
        if callable(self.checkpoint_callback):
            self.checkpoint_callback(boundary)

    def raise_if_cancelled(self) -> None:
        if self.cancellation_event is not None and getattr(self.cancellation_event, "is_set", lambda: False)():
            raise BranchCancelled("branch cancelled by parent policy")

    def run_model_request(
        self,
        *,
        instructions: str,
        prompt: str,
        model_class: str,
        purpose: str,
        payload: Optional[dict[str, Any]] = None,
        trace_context: OpenAITraceContext | None = None,
    ) -> ModelResponse:
        self.raise_if_cancelled()
        if self.budget.remaining_model_calls() <= 0:
            raise HardInvalidation(f"model-call budget exhausted before provider request for {purpose}")
        effective_trace_context = trace_context or self.trace_context
        idempotency_trace_context = _trace_context_subset(
            effective_trace_context,
            _PROVIDER_IDEMPOTENCY_TRACE_FIELDS,
        )
        request_digest = stable_hash(instructions, prompt, model_class, payload or {}, idempotency_trace_context)
        unresolved_launch = False
        terminal_receipt: SideEffectReceipt | None = None
        for receipt_payload in self.state.side_effect_receipts:
            receipt = (SideEffectReceipt).model_validate(receipt_payload)
            if receipt.idempotency_key != request_digest:
                continue
            if is_terminal_receipt(receipt):
                terminal_receipt = receipt
                continue
            if receipt.action_kind == "provider_request" and receipt.status == "launched":
                unresolved_launch = True
        if terminal_receipt is not None:
            result_ref = dict(terminal_receipt.result_ref or {})
            if terminal_receipt.status in {"completed", "reconciled"}:
                self.record(
                    "side_effect_reconciled",
                    side_effect_id=terminal_receipt.side_effect_id,
                    action_kind=terminal_receipt.action_kind,
                    reconciliation_status=terminal_receipt.status,
                )
                return ModelResponse(
                    text=str(result_ref.get("text", "")),
                    raw={"replayed_from_receipt": terminal_receipt.side_effect_id},
                    model_name=result_ref.get("model_name"),
                    trace_call_id=str(result_ref.get("trace_call_id") or "").strip() or None,
                    input_tokens=int(result_ref.get("input_tokens", 0) or 0),
                    output_tokens=int(result_ref.get("output_tokens", 0) or 0),
                    token_estimate=int(result_ref.get("input_tokens", 0) or 0) + int(result_ref.get("output_tokens", 0) or 0),
                    latency_s=0.0,
                    dollar_cost=0.0,
                )
            raise HardInvalidation(
                f"provider request {request_digest[:12]} already has terminal receipt status {terminal_receipt.status!r}"
            )
        if unresolved_launch:
            raise HardInvalidation("provider request was already launched and must be reconciled before reissue")
        launch_receipt = SideEffectReceipt(
            side_effect_id=f"provider-request.{request_digest[:12]}",
            action_fingerprint=request_digest,
            idempotency_key=request_digest,
            action_kind="provider_request",
            request_id=self.request_id,
            plan_id=self.plan.plan_id,
            frame_id=getattr(self.active_frame, "frame_id", ""),
            node_id=effective_trace_context.op_id or "",
            branch_id=effective_trace_context.worker_id,
            trace_context=effective_trace_context,
            request_digest=request_digest,
            backend=self.runtime_backend,
            status="launched",
            result_ref={
                "request": {
                    "instructions": instructions,
                    "prompt": prompt,
                    "model_class": model_class,
                    "purpose": purpose,
                    "payload": dict(payload or {}),
                }
            },
            replay_policy="reconcile_before_reissue",
            reconciliation_policy="strict",
            created_at=now_ts(),
        )
        self.record_side_effect(launch_receipt)
        self.publish_checkpoint_boundary("after_provider_launch")
        self.raise_if_cancelled()
        response = self.provider.generate(
            self.build_model_request(
                instructions=instructions,
                prompt=prompt,
                model_class=model_class,
                purpose=purpose,
                payload=payload,
                trace_context=effective_trace_context,
            )
        )
        self.consume_model_response(response, purpose=purpose)
        completion_receipt = SideEffectReceipt(
            side_effect_id=f"provider-completion.{request_digest[:12]}",
            action_fingerprint=request_digest,
            idempotency_key=request_digest,
            action_kind="provider_completion",
            request_id=self.request_id,
            plan_id=self.plan.plan_id,
            frame_id=getattr(self.active_frame, "frame_id", ""),
            node_id=effective_trace_context.op_id or "",
            branch_id=effective_trace_context.worker_id,
            trace_context=effective_trace_context,
            request_digest=request_digest,
            backend=self.runtime_backend,
            status="completed",
            result_ref={
                "text": response.text,
                "model_name": response.model_name,
                "trace_call_id": str(response.trace_call_id or response.raw.get("trace_call_id") or "").strip() or None,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "token_estimate": response.token_estimate,
            },
            replay_policy="reuse_if_completed",
            reconciliation_policy="strict",
            created_at=now_ts(),
        )
        self.record_side_effect(completion_receipt)
        self.publish_checkpoint_boundary("after_provider_completion")
        self.raise_if_cancelled()
        return response
