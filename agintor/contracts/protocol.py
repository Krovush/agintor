from __future__ import annotations

import base64
import json
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, NamedTuple, Optional, Sequence
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from ..utils import cheap_embedding, now_ts, stable_hash

from .benchmarks import BenchmarkTask
from .execution import RequestFileRef
from .runtime import RuntimeIsolationPolicy
from .sessions import RuntimeSessionSeed
from .state import LongTermGraphSnapshot, PredictorSnapshot
from .tracing import OpenAITraceContext

class ToolExecutionResult(BaseModel):
    tool_name: str
    output: Any
    stdout: str = ""
    stderr: str = ""
    latency_s: float = 0.0
    success: bool = True
    artifacts: List[str] = Field(default_factory=list)
    async_handle_id: Optional[str] = None
    verifier_ok: Optional[bool] = None


class SolveRequest(BaseModel):
    request_id: str
    prompt: str
    context_items: List[Dict[str, Any]] = Field(default_factory=list)
    file_paths: List[str] = Field(default_factory=list)
    request_file_refs: List[RequestFileRef] = Field(default_factory=list)
    output_schema: Dict[str, Any] = Field(default_factory=dict)
    allowed_tool_categories: List[str] = Field(default_factory=list)
    verification_preference: str = "verified_if_available"
    budget_overrides: Dict[str, Any] = Field(default_factory=dict)


class SolveResult(BaseModel):
    request_id: str
    runtime_hash: str
    run_id: str = ""
    run_root: str = ""
    attempt_id: str = ""
    latest_checkpoint_ref: Optional[str] = None
    run_lifecycle_state: Optional[Literal["running", "paused", "completed", "failed", "cancelled", "pruned"]] = None
    run_resumable: bool = False
    run_prune_eligible: bool = False
    mode: str = "benchmark"
    artifact: Any
    status: str
    verification_status: str = "best_effort"
    summary: str
    checks: List[Dict[str, Any]] = Field(default_factory=list)
    trace_ref: Optional[str] = None
    checkpoint_ref: Optional[str] = None
    budget: Dict[str, Any] = Field(default_factory=dict)
    provider_usage: Dict[str, Any] = Field(default_factory=dict)
    faults: Dict[str, Any] = Field(default_factory=dict)
    verified: bool = False
    best_effort: bool = False
    post_message_long_term_graph: Optional[LongTermGraphSnapshot] = None
    post_message_predictor_snapshot: Optional[PredictorSnapshot] = None
    post_message_short_term_export: List[Dict[str, Any]] = Field(default_factory=list)


class RuntimeSolveRequest(BaseModel):
    request_id: str
    evaluation_unit_id: str = ""
    run_id: str = ""
    run_root: str = ""
    attempt_id: str = ""
    runtime_backend: str
    mode: Literal["benchmark", "user_request"]
    seed: int = 0
    task: Optional["BenchmarkTask"] = None
    solve_request: Optional[SolveRequest] = None
    session_seed: Optional[RuntimeSessionSeed] = None
    budget_overrides: Dict[str, Any] = Field(default_factory=dict)
    trace_context: Optional[OpenAITraceContext] = None

    @model_validator(mode="after")
    def validate_mode_payload(self) -> "RuntimeSolveRequest":
        mode = self.mode
        task = self.task
        solve_request = self.solve_request
        if mode == "benchmark" and task is None:
            raise ValueError("benchmark solve requests require a benchmark task")
        if mode == "user_request" and solve_request is None:
            raise ValueError("user_request solve requests require a solve_request payload")
        if mode == "benchmark" and self.session_seed is not None:
            raise ValueError("benchmark solve requests must not carry a runtime session seed")
        if self.session_seed is not None:
            trace_context = self.trace_context
            if trace_context is None:
                raise ValueError("runtime session seeds require trace session identity")
            if trace_context.runtime_session_id != self.session_seed.session_id:
                raise ValueError("runtime session seed does not match trace runtime_session_id")
            if trace_context.runtime_message_index != self.session_seed.message_index:
                raise ValueError("runtime session seed does not match trace runtime_message_index")
        return self


class RunResult(BaseModel):
    request_id: str = ""
    plan_id: str = ""
    run_id: str = ""
    run_root: str = ""
    attempt_id: str = ""
    runtime_hash: str = ""
    runtime_backend: str = ""
    latest_checkpoint_ref: Optional[str] = None
    run_lifecycle_state: Optional[Literal["running", "paused", "completed", "failed", "cancelled", "pruned"]] = None
    run_resumable: bool = False
    run_prune_eligible: bool = False
    task_id: str
    seed: int
    artifact: Any
    verifier_score: float
    cost: float
    latency: float
    faults: int
    trace: List[Dict[str, Any]] = Field(default_factory=list)
    trace_context: Optional[OpenAITraceContext] = None
    trace_path: Optional[str] = None
    hard_invalid: bool = False
    invalid_reason: Optional[str] = None
    failure_kind: Optional[str] = None
    mode: Optional[str] = None
    lifecycle_state: Optional[str] = None
    created_tools: int = 0
    promoted_nodes: int = 0
    checks_used: int = 0
    model_calls: int = 0
    tokens_used: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    utility: Optional[float] = None
    checkpoint_ref: Optional[str] = None
    provider_usage: Dict[str, Any] = Field(default_factory=dict)

    @staticmethod
    def _inline_trace_prefix() -> str:
        return "inline-json:"

    @classmethod
    def encode_trace_ref(cls, trace: List[Dict[str, Any]]) -> str:
        payload = json.dumps(trace, sort_keys=True, separators=(",", ":")).encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).decode("ascii")
        return cls._inline_trace_prefix() + encoded

    @classmethod
    def decode_trace_ref(cls, trace_ref: str) -> List[Dict[str, Any]]:
        if not trace_ref.startswith(cls._inline_trace_prefix()):
            return []
        encoded = trace_ref[len(cls._inline_trace_prefix()) :]
        try:
            payload = base64.urlsafe_b64decode(encoded.encode("ascii"))
            trace = json.loads(payload.decode("utf-8"))
        except Exception:
            return []
        return trace if isinstance(trace, list) else []

    def trace_rows(self) -> List[Dict[str, Any]]:
        if self.trace:
            return [dict(row) for row in self.trace]
        if not self.trace_path:
            return []
        inline_trace = self.decode_trace_ref(self.trace_path)
        if inline_trace:
            return inline_trace
        try:
            payload = json.loads(Path(self.trace_path).read_text(encoding="utf-8"))
        except Exception:
            return []
        return payload if isinstance(payload, list) else []

    def trace_ref(self) -> str:
        if self.trace_path:
            return self.trace_path
        return self.encode_trace_ref(self.trace)


class InspectRequest(BaseModel):
    request_id: str
    requested_backend: str = "local"
    expected_runtime_contract_version: str


class ResumeRequest(BaseModel):
    request_id: str = ""
    run_ref: Optional[str] = None
    checkpoint_ref: Optional[str] = None
    trace_context: Optional[OpenAITraceContext] = None
    reconciliation_policy: Literal["strict", "best_effort"] = "strict"

    @model_validator(mode="after")
    def validate_resume_target(self) -> "ResumeRequest":
        run_ref = str(self.run_ref or "").strip()
        checkpoint_ref = str(self.checkpoint_ref or "").strip()
        if not run_ref and not checkpoint_ref:
            raise ValueError("resume requires run_ref or checkpoint_ref")
        return self


class RuntimeResumeRequest(BaseModel):
    request_id: str = ""
    evaluation_unit_id: str = ""
    run_ref: Optional[str] = None
    checkpoint_ref: Optional[str] = None
    run_id: str = ""
    run_root: str = ""
    attempt_id: str = ""
    runtime_backend: str = "local"
    checkpoint_store_dir: str = ""
    trace_context: Optional[OpenAITraceContext] = None
    reconciliation_policy: Literal["strict", "best_effort"] = "strict"

    @model_validator(mode="after")
    def validate_resume_target(self) -> "RuntimeResumeRequest":
        run_ref = str(self.run_ref or "").strip()
        checkpoint_ref = str(self.checkpoint_ref or "").strip()
        if not run_ref and not checkpoint_ref:
            raise ValueError("resume requires run_ref or checkpoint_ref")
        return self


class CapabilityExchange(BaseModel):
    runtime_contract_version: str
    supported_backends: List[str] = Field(default_factory=list)
    tool_runtimes: List[str] = Field(default_factory=list)
    checkpoint_support: bool = True
    runtime_asset_capabilities: Dict[str, bool] = Field(default_factory=dict)
    side_effect_receipts: bool = False
    resume_support: bool = True
    runtime_isolation_policy: Optional[RuntimeIsolationPolicy] = None
    supported_guarantees: List[str] = Field(default_factory=list)
    effective_guarantees: List[str] = Field(default_factory=list)
    required_env_names: List[str] = Field(default_factory=list)
    required_env_any_of: List[List[str]] = Field(default_factory=list)
    capability_flags: List[str] = Field(default_factory=list)


class RuntimeSolveResponse(BaseModel):
    request_id: str
    capability_exchange: CapabilityExchange
    solve_result: SolveResult


class RuntimeTaskInvocation(BaseModel):
    request_id: str
    evaluation_unit_id: str = ""
    episode_kind: Optional[Literal["transfer_episode"]] = None
    episode_step_index: Optional[int] = None
    run_id: str = ""
    run_root: str = ""
    attempt_id: str = ""
    runtime_backend: str = ""
    seed: int
    task: BenchmarkTask
    trace_context: Optional[OpenAITraceContext] = None

    @field_validator("episode_kind", mode="before")
    @classmethod
    def normalize_episode_kind(cls, value: Any) -> Any:
        text = str(value or "").strip()
        if text == "transfer_episode":
            return text
        return None

    @model_validator(mode="after")
    def validate_episode_grouping(self) -> "RuntimeTaskInvocation":
        task = self.task
        if self.episode_kind is None:
            if (
                task is not None
                and bool(getattr(task, "transfer_scored", False))
                and str(getattr(task, "episode_id", "") or "").strip()
            ):
                self.episode_kind = "transfer_episode"
        if self.episode_kind == "transfer_episode":
            if self.episode_step_index is None and task is not None:
                self.episode_step_index = int(getattr(task, "episode_order", 0) or 0)
        else:
            self.episode_step_index = None
        return self


class RuntimeBatchRequest(BaseModel):
    request_id: str
    runtime_backend: str
    budget_overrides: Dict[str, Any] = Field(default_factory=dict)
    invocations: List[RuntimeTaskInvocation] = Field(default_factory=list)
    trace_context: Optional[OpenAITraceContext] = None


class ExecutionUnitRequestEnvelope(BaseModel):
    request_kind: Literal["runtime_solve_request", "runtime_task_invocation", "runtime_task_invocation_group"]
    request_mode: Literal["benchmark", "user_request", "batch"]
    request_id: str
    evaluation_unit_id: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    member_invocations: List[RuntimeTaskInvocation] = Field(default_factory=list)


class RuntimeBatchResponse(BaseModel):
    request_id: str
    capability_exchange: CapabilityExchange
    run_results: List[RunResult] = Field(default_factory=list)
    provider_usage: Dict[str, Any] = Field(default_factory=dict)
