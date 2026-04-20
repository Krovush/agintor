from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .exceptions import BranchCancelled, HardInvalidation, PromptAdaptationError
from .pydantic_compat import model_copy, model_dump, model_validate
from .providers import ModelProvider
from .runtime_profile import RuntimeProfile, default_runtime_profile
from .schemas import (
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
from .utils import now_ts, stable_hash


_NUMBER_LIST_RE = re.compile(r"\[([^\]]+)\]")
_MODULUS_RE = re.compile(r"\bmod(?:ulo)?\s+(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_SYMBOL_RE = re.compile(r"\b([A-Z][A-Z0-9_]{2,})\b")
_PROMPT_ABSOLUTE_PATH_RE = re.compile(r"(?P<path>(?:[A-Za-z]:[\\/]|/)[^\n\r\t\"'<>|]+)")
_URL_RE = re.compile(r"(?P<url>[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>]+)", re.IGNORECASE)
_HTTP_METHOD_RE = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\b", re.IGNORECASE)
_TRAILING_PATH_PUNCTUATION = "\"'`,;:!?)]}"
_REPO_PATCH_TOKENS = (
    "edit",
    "modify",
    "update",
    "change",
    "patch",
    "rewrite",
    "refactor",
    "fix",
    "rename",
    "replace",
)
_FILE_INSPECTION_TOKENS = (
    "read",
    "inspect",
    "summarize",
    "explain",
    "analyze",
    "review",
    "show",
    "what",
    "which",
    "who",
)
_SERVICE_ACTION_TOKENS = (
    "call",
    "fetch",
    "request",
    "invoke",
    "send",
    "post",
    "get",
    "put",
    "delete",
    "patch",
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
            runtime_event = model_copy(
                runtime_event,
                update={"sequence_no": int(self.state.event_sequence_no or 0) + 1},
                deep=True,
            )
        self.state.event_sequence_no = max(int(self.state.event_sequence_no or 0), int(runtime_event.sequence_no or 0))
        self.trace.append(runtime_event.trace_row())

    def consume_model_response(self, response: ModelResponse, purpose: str) -> None:
        self.budget.consume_model_response(response)
        self.record(
            "model_response",
            purpose=purpose,
            model_class=response.model_name,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            total_tokens=response.token_estimate,
            dollar_cost=response.dollar_cost,
            latency_s=response.latency_s,
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
                "trace_context": model_dump(effective_trace_context),
            },
        )

    def record_side_effect(self, receipt: SideEffectReceipt) -> None:
        self.state.side_effect_receipts.append(model_dump(receipt))
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
        idempotency_trace_context = model_dump(effective_trace_context)
        idempotency_trace_context.pop("run_node_id", None)
        request_digest = stable_hash(instructions, prompt, model_class, payload or {}, idempotency_trace_context)
        unresolved_launch = False
        terminal_receipt: SideEffectReceipt | None = None
        for receipt_payload in self.state.side_effect_receipts:
            receipt = model_validate(SideEffectReceipt, receipt_payload)
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


def _normalized_request_prompt(text: str) -> str:
    return " ".join(str(text or "").split()).strip()


def _coerce_context_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _coerce_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _clean_prompt_path(value: str) -> str:
    cleaned = str(value or "").strip()
    while cleaned and cleaned[-1] in _TRAILING_PATH_PUNCTUATION:
        cleaned = cleaned[:-1]
    return cleaned


def _normalize_path_key(value: str) -> str:
    cleaned = _clean_prompt_path(value)
    if not cleaned:
        return ""
    normalized = re.sub(r"/+", "/", cleaned.replace("\\", "/"))
    if re.match(r"^[A-Za-z]:/", normalized):
        return normalized.casefold()
    return normalized


def _trim_prompt_path_to_absolute_candidate(raw_path: str) -> str | None:
    candidate = _clean_prompt_path(raw_path)
    path = Path(candidate).expanduser()
    if path.is_absolute() and path.exists():
        return str(path.resolve())
    extension_match = re.match(
        r"^(?P<path>.+\.[A-Za-z0-9]{1,8})(?=(?:\s+(?:to|and|using|then|that|which|please|for)\b|$))",
        candidate,
        flags=re.IGNORECASE,
    )
    if extension_match:
        trimmed = _clean_prompt_path(extension_match.group("path"))
        if Path(trimmed).expanduser().is_absolute():
            return str(Path(trimmed).expanduser().resolve(strict=False))
    clause_patterns = (
        r"\s+(?:to|and|using|then|that|which|please|for)\b.*$",
        r"[,:;].*$",
    )
    for pattern in clause_patterns:
        trimmed = re.sub(pattern, "", candidate, flags=re.IGNORECASE).rstrip(" " + _TRAILING_PATH_PUNCTUATION)
        if trimmed != candidate and Path(trimmed).expanduser().is_absolute():
            return str(Path(trimmed).expanduser().resolve(strict=False))
    if path.is_absolute():
        return str(path.resolve(strict=False))
    return None


def _extract_prompt_file_paths(prompt: str) -> list[str]:
    matches: list[str] = []
    seen: set[str] = set()
    for match in _PROMPT_ABSOLUTE_PATH_RE.finditer(prompt):
        resolved = _trim_prompt_path_to_absolute_candidate(match.group("path"))
        if not resolved:
            continue
        key = _normalize_path_key(resolved)
        if not key or key in seen:
            continue
        seen.add(key)
        matches.append(resolved)
    return matches


def _category_allowed(allowed_categories: list[str], required_category: str | None) -> bool:
    return capability_scope_allows(allowed_categories, required_category)


def _tool_category_hint(tool_hint: str | None) -> str:
    parts = [part for part in str(tool_hint or "").split("/") if part]
    if len(parts) >= 2:
        return "/".join(parts[:2])
    return parts[0] if parts else ""


def _dedupe_tool_categories(categories: Sequence[str]) -> list[str]:
    return normalize_capability_scopes(categories)


def _capability_intent(
    *,
    required_tool_categories: Sequence[str] = (),
    requires_default_provider: bool = False,
    requires_network_access: bool = False,
    network_transports: Sequence[str] = (),
    requires_filesystem_write: bool = False,
) -> dict[str, Any]:
    return {
        "required_tool_categories": expand_capability_scopes(required_tool_categories),
        "requires_default_provider": bool(requires_default_provider),
        "requires_network_access": bool(requires_network_access),
        "network_transports": normalize_service_transports(network_transports),
        "requires_filesystem_write": bool(requires_filesystem_write),
    }


def load_solve_request(prompt: str | None = None, prompt_file: str | Path | None = None) -> SolveRequest:
    payload: dict[str, Any] = {}
    prompt_text = _normalized_request_prompt(prompt or "")
    if prompt_file is not None:
        raw = Path(prompt_file).read_text(encoding="utf-8").lstrip("\ufeff")
        try:
            loaded = json.loads(raw)
        except Exception:
            if not prompt_text:
                prompt_text = _normalized_request_prompt(raw)
        else:
            if isinstance(loaded, dict):
                payload = dict(loaded)
            elif not prompt_text:
                prompt_text = _normalized_request_prompt(raw)
    if not prompt_text:
        prompt_text = _normalized_request_prompt(payload.get("prompt", ""))
    if not prompt_text:
        raise ValueError("solve requires either a task id or a prompt / prompt file")
    verification_preference = str(payload.get("verification_preference", "verified_if_available")).strip() or "verified_if_available"
    if verification_preference not in {"verified_if_available", "best_effort", "required"}:
        verification_preference = "verified_if_available"
    initial_file_paths = _coerce_string_list(payload.get("file_paths"))
    request_file_refs = [
        model_validate(RequestFileRef, row)
        for row in payload.get("request_file_refs", [])
        if isinstance(row, Mapping)
    ] or [
        _compile_request_file_ref(path)
        for path in (initial_file_paths or _extract_prompt_file_paths(prompt_text))
    ]
    request_payload = {
        "prompt": prompt_text,
        "context_items": _coerce_context_items(payload.get("context_items")),
        "file_paths": [file_ref.source_path for file_ref in request_file_refs],
        "request_file_refs": request_file_refs,
        "output_schema": dict(payload.get("output_schema", {})) if isinstance(payload.get("output_schema", {}), dict) else {},
        "allowed_tool_categories": _coerce_string_list(payload.get("allowed_tool_categories")),
        "verification_preference": verification_preference,
        "budget_overrides": dict(payload.get("budget_overrides", {})) if isinstance(payload.get("budget_overrides", {}), dict) else {},
    }
    request_id_payload = dict(request_payload)
    request_id_payload["request_file_refs"] = [model_dump(file_ref) for file_ref in request_file_refs]
    request_id = str(payload.get("request_id", "")).strip() or f"solve.{stable_hash(request_id_payload)[:12]}"
    return SolveRequest(request_id=request_id, **request_payload)


def benchmark_task_to_solve_request(task: BenchmarkTask, *, request_id: str | None = None) -> SolveRequest:
    verification_preference = "verified_if_available"
    if task.allow_best_effort:
        verification_preference = "best_effort"
    elif task.verification_required:
        verification_preference = "required"
    request_file_refs = [_compile_request_file_ref(path) for path in task.file_paths]
    return SolveRequest(
        request_id=request_id or f"benchmark.{task.task_id}",
        prompt=task.prompt,
        context_items=[dict(item) for item in task.context_items],
        file_paths=[file_ref.source_path for file_ref in request_file_refs],
        request_file_refs=request_file_refs,
        output_schema={},
        allowed_tool_categories=list(task.allowed_tool_categories),
        verification_preference=verification_preference,
        budget_overrides={},
    )


def _parse_number_list(prompt: str) -> list[float]:
    match = _NUMBER_LIST_RE.search(prompt)
    if not match:
        return []
    values: list[float] = []
    for raw_value in match.group(1).split(","):
        text = raw_value.strip()
        if not text:
            continue
        try:
            values.append(float(text))
        except ValueError:
            return []
    return values


def _number_value(value: float) -> int | float:
    integer = int(value)
    return integer if float(integer) == float(value) else value


def _median(values: list[float]) -> int | float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return _number_value(ordered[mid])
    return _number_value((ordered[mid - 1] + ordered[mid]) / 2.0)


def _verification_policy(preference: str, *, exact_verifier_exists: bool) -> tuple[bool, bool]:
    if preference == "required":
        return True, False
    if preference == "best_effort":
        return False, True
    return exact_verifier_exists, not exact_verifier_exists


def _find_symbol_value(request: SolveRequest, symbol: str) -> str | None:
    for item in request.context_items:
        if str(item.get("symbol", "")).strip() == symbol and "value" in item:
            return str(item["value"])
    return None


def _find_file_owner(request: SolveRequest, file_path: str) -> str | None:
    expected_path = _normalize_path_key(file_path)
    for item in request.context_items:
        if _normalize_path_key(str(item.get("file_path", ""))) == expected_path and "owner" in item:
            return str(item["owner"])
    return None


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


def batch_evaluation_unit_key(invocation: RuntimeTaskInvocation) -> str:
    episode_kind = str(getattr(invocation, "episode_kind", "") or "single_task").strip()
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
    objective: str | None = None,
    session_id: str | None = None,
    build_id: str | None = None,
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
        objective=objective,
    )


def derive_trace_context(parent: OpenAITraceContext | None, **updates: Any) -> OpenAITraceContext:
    payload = model_dump(parent) if parent is not None else {}
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
    objective: str | None = None,
) -> OpenAITraceContext:
    return derive_trace_context(
        parent,
        provider_role="runtime",
        request_id=request_id,
        runtime_hash=runtime_hash,
        runtime_dir=runtime_dir,
        task_id=task_id,
        seed=seed,
        objective=objective,
    )


def execution_plan_requires_default_provider(plan: ExecutionPlan) -> bool:
    return any(plan_node_requires_default_provider(node) for node in plan.nodes)


def execution_plan_is_prompt_local_only(plan: ExecutionPlan) -> bool:
    return all(plan_node_allowed_in_prompt_mode_local_only(node) for node in plan.nodes)


def _dedupe_network_transports(transports: Sequence[str]) -> list[str]:
    return normalize_service_transports(transports)


def _tool_category_network_transport(category: str) -> str | None:
    transports = capability_scope_service_transports([category])
    if len(transports) != 1:
        return None
    return transports[0]


def _tool_category_requires_network_access(category: str) -> bool:
    return capability_scope_requires_network_access(category)


def _tool_category_requires_filesystem_write(category: str) -> bool:
    return capability_scope_requires_filesystem_write(category)


def _plan_node_capability_intent(node: PlanNode) -> dict[str, Any]:
    payload = node.metadata.get("capability_intent")
    if isinstance(payload, Mapping):
        return dict(payload)
    return {}


def execution_plan_requirements(plan: ExecutionPlan) -> ExecutionPlanRequirements:
    request_mode = "user_request" if plan.origin.origin_kind == "user_request" else "benchmark"
    required_tool_categories: list[str] = []
    default_provider_nodes: list[str] = []
    network_nodes: list[str] = []
    network_transport_nodes: dict[str, list[str]] = {}
    filesystem_write_nodes: list[str] = []

    for node in plan.nodes:
        capability_intent = _plan_node_capability_intent(node)
        node_tool_categories = expand_capability_scopes(capability_intent.get("required_tool_categories", []))
        if not node_tool_categories:
            fallback_categories: list[str] = []
            tool_category_hint = str(node.metadata.get("tool_category_hint", "") or "").strip()
            if tool_category_hint:
                fallback_categories.append(tool_category_hint)
            elif str(node.node_kind) in {"builtin_op", "tool_call", "tool_synthesis", "repo_patch", "service_action"}:
                fallback_categories.extend(node.allowed_tool_categories)
            node_tool_categories = expand_capability_scopes(fallback_categories)
        required_tool_categories.extend(node_tool_categories)

        requires_default_provider = (
            bool(capability_intent.get("requires_default_provider"))
            if "requires_default_provider" in capability_intent
            else plan_node_requires_default_provider(node)
        )
        if requires_default_provider:
            default_provider_nodes.append(node.node_id)

        requires_network_access = (
            bool(capability_intent.get("requires_network_access"))
            if "requires_network_access" in capability_intent
            else str(node.node_kind) == "service_action"
            or any(_tool_category_requires_network_access(category) for category in node_tool_categories)
        )
        node_network_transports = normalize_service_transports(capability_intent.get("network_transports", []))
        if not node_network_transports:
            fallback_network_transports: list[str] = []
            service_transport = str(node.metadata.get("service_transport", "") or "").strip().lower()
            if service_transport:
                fallback_network_transports.append(service_transport)
            fallback_network_transports.extend(capability_scope_service_transports(node_tool_categories))
            node_network_transports = normalize_service_transports(fallback_network_transports)
        if requires_network_access and not node_network_transports:
            node_network_transports = ["generic"]
        if requires_network_access:
            network_nodes.append(node.node_id)
            for transport in node_network_transports:
                network_transport_nodes.setdefault(transport, []).append(node.node_id)

        requires_filesystem_write = (
            bool(capability_intent.get("requires_filesystem_write"))
            if "requires_filesystem_write" in capability_intent
            else str(node.node_kind) == "repo_patch"
            or any(_tool_category_requires_filesystem_write(category) for category in node_tool_categories)
        )
        if requires_filesystem_write:
            filesystem_write_nodes.append(node.node_id)

    return ExecutionPlanRequirements(
        request_mode=request_mode,
        requires_default_provider=bool(default_provider_nodes),
        default_provider_nodes=default_provider_nodes,
        requires_network_access=bool(network_nodes),
        network_nodes=network_nodes,
        required_network_transports=sorted(network_transport_nodes),
        network_transport_nodes={key: list(value) for key, value in sorted(network_transport_nodes.items())},
        requires_filesystem_write=bool(filesystem_write_nodes),
        filesystem_write_nodes=filesystem_write_nodes,
        required_tool_categories=expand_capability_scopes(required_tool_categories),
    )


def _dedupe_prompt_paths(paths: Sequence[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for path in paths:
        key = _normalize_path_key(path)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(_clean_prompt_path(path))
    return deduped


def _compile_request_file_ref(path_text: str) -> RequestFileRef:
    cleaned = _clean_prompt_path(path_text)
    candidate = Path(cleaned).expanduser()
    if candidate.is_absolute():
        host_path = str(candidate.resolve(strict=False))
        return RequestFileRef(
            file_ref_id=f"file.{stable_hash(host_path)[:12]}",
            source_path=cleaned,
            runtime_path=host_path,
            path_root="host_absolute",
            host_path=host_path,
        )
    normalized_relative = re.sub(r"[\\/]+", "/", cleaned)
    return RequestFileRef(
        file_ref_id=f"file.{stable_hash(normalized_relative)[:12]}",
        source_path=cleaned,
        runtime_path=normalized_relative,
        path_root="runtime_workspace_relative",
        workspace_relative_path=normalized_relative,
    )


def _compiled_request_file_refs(request: SolveRequest) -> list[RequestFileRef]:
    if request.request_file_refs:
        return [model_validate(RequestFileRef, model_dump(ref)) for ref in request.request_file_refs]
    explicit = _dedupe_prompt_paths(list(request.file_paths))
    raw_paths = explicit or _dedupe_prompt_paths(_extract_prompt_file_paths(request.prompt))
    return [_compile_request_file_ref(path) for path in raw_paths]


def _request_file_source_paths(request: SolveRequest) -> list[str]:
    return [file_ref.source_path for file_ref in _compiled_request_file_refs(request)]


def _request_file_paths(request: SolveRequest) -> list[str]:
    return [file_ref.runtime_path for file_ref in _compiled_request_file_refs(request)]


def _prompt_template_allowed_categories(
    request: SolveRequest,
    required_categories: Sequence[str],
) -> list[str]:
    if request.allowed_tool_categories:
        return normalize_capability_scopes(request.allowed_tool_categories)
    return normalize_capability_scopes(required_categories)


def _prompt_request_metadata(
    request: SolveRequest,
    *,
    template_kind: str,
    adaptation_assumptions: Sequence[str] | None = None,
    file_paths: Sequence[str] | None = None,
) -> dict[str, Any]:
    return {
        "request_id": request.request_id,
        "solve_mode": "user_request",
        "output_schema": request.output_schema,
        "allowed_tool_categories": list(request.allowed_tool_categories),
        "template_kind": template_kind,
        "adaptation_assumptions": [str(item) for item in adaptation_assumptions or [] if str(item).strip()],
        "request_file_paths": [str(path) for path in file_paths or [] if str(path).strip()],
    }


def _prompt_requests_repo_patch(prompt_lower: str, file_paths: Sequence[str]) -> bool:
    return bool(file_paths) and any(token in prompt_lower for token in _REPO_PATCH_TOKENS)


def _prompt_requests_file_inspection(prompt_lower: str, file_paths: Sequence[str]) -> bool:
    return bool(file_paths) and any(token in prompt_lower for token in _FILE_INSPECTION_TOKENS)


def _extract_prompt_url(prompt: str) -> str | None:
    match = _URL_RE.search(prompt)
    if not match:
        return None
    return str(match.group("url")).strip().rstrip(_TRAILING_PATH_PUNCTUATION)


def _service_context_item(request: SolveRequest) -> Mapping[str, Any] | None:
    for item in request.context_items:
        if isinstance(item.get("service_action"), Mapping):
            return dict(item["service_action"])
        if any(key in item for key in ("service_url", "url")):
            return dict(item)
    return None


def _service_allowed_categories(request: SolveRequest) -> list[str]:
    return capability_scope_service_categories(request.allowed_tool_categories)


def _service_request_spec(request: SolveRequest, prompt: str, prompt_lower: str) -> dict[str, Any] | None:
    item = _service_context_item(request)
    url = ""
    if item is not None:
        url = str(item.get("service_url") or item.get("url") or "").strip()
    if not url:
        url = str(_extract_prompt_url(prompt) or "").strip()
    service_intent = item is not None or (bool(url) and any(token in prompt_lower for token in _SERVICE_ACTION_TOKENS))
    if not service_intent:
        return None
    if not url:
        raise PromptAdaptationError(
            "template_mismatch",
            "bounded_service_action adaptation requires an explicit service URL",
        )
    allowed_service_categories = _service_allowed_categories(request)
    if request.allowed_tool_categories and not allowed_service_categories:
        raise PromptAdaptationError(
            "missing_capability",
            "bounded_service_action requires a service/* allowed_tool_categories capability such as service/http",
        )
    service_transport = ""
    category_hint = ""
    if item is not None:
        service_transport = str(item.get("service_transport") or item.get("transport") or "").strip()
        category_hint = str(item.get("service_category_hint") or item.get("category_hint") or "").strip()
    try:
        transport_compatibility = service_action_transport_compatibility(
            url=url,
            service_transport=service_transport,
            category_hint=category_hint,
            allowed_tool_categories=allowed_service_categories or [],
        )
    except ValueError as exc:
        raise PromptAdaptationError("template_mismatch", str(exc)) from exc
    method = "GET"
    if item is not None:
        method = str(item.get("method") or item.get("http_method") or method).strip().upper() or method
    else:
        method_match = _HTTP_METHOD_RE.search(prompt)
        if method_match:
            method = str(method_match.group(1)).strip().upper() or method
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise PromptAdaptationError(
            "template_mismatch",
            f"bounded_service_action does not support HTTP method {method!r}",
        )
    body = None
    headers: dict[str, Any] = {}
    timeout_s = 10.0
    if item is not None:
        candidate_body = item.get("body", item.get("json", item.get("payload")))
        if candidate_body is not None:
            body = candidate_body
        if isinstance(item.get("headers"), Mapping):
            headers = dict(item["headers"])
        try:
            timeout_s = float(item.get("timeout_s", timeout_s) or timeout_s)
        except Exception:
            timeout_s = 10.0
    return {
        "url": url,
        "method": method,
        "body": body,
        "headers": headers,
        "timeout_s": timeout_s,
        "service_transport": transport_compatibility.transport,
    }


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
        "request_file_refs": [model_dump(file_ref) for file_ref in file_ref_specs],
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
        model_copy(node, update={"branch_group_id": branch_assignments.get(node.node_id)})
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
            adjusted_nodes.append(model_copy(node, update={"dependencies": merged_dependencies}))
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
            input_bindings.append(model_validate(InputBinding, binding_payload))
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
    plan_trace_context = runtime_trace_context(
        trace_context,
        request_id=request_id,
        runtime_hash=runtime_hash,
        runtime_dir=runtime_dir,
        task_id=task.task_id,
        seed=seed,
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
            model_validate(RequestFileRef, row)
            for row in payload
            if isinstance(row, Mapping)
        ]
    return [_compile_request_file_ref(path) for path in task.file_paths]


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


def solve_result_from_run_result(request: SolveRequest, run: RunResult, runtime_hash: str) -> SolveResult:
    return solve_result_from_run_result_with_context(
        request,
        run,
        runtime_hash,
        mode="user_request",
        provider_usage={},
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


def resume_task_and_plan_from_checkpoint(
    envelope: CheckpointEnvelope,
) -> tuple[BenchmarkTask, ExecutionPlan]:
    task = model_validate(BenchmarkTask, envelope.task_payload)
    plan = model_validate(ExecutionPlan, envelope.plan_snapshot)
    return task, plan


def _rebound_trace_context_payload(
    payload: Mapping[str, Any] | None,
    active_request_id: str,
) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    rebound = dict(payload)
    rebound["request_id"] = active_request_id
    return rebound


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
) -> dict[str, Any] | None:
    if not isinstance(frame_payload, Mapping):
        return None
    rebound = dict(frame_payload)
    rebound["request_id"] = active_request_id
    rebound["trace_context"] = _rebound_trace_context_payload(
        rebound.get("trace_context"),
        active_request_id,
    )
    return rebound


def _rebind_branch_publication_request_id(
    publication_payload: Mapping[str, Any] | None,
    active_request_id: str,
) -> dict[str, Any] | None:
    if not isinstance(publication_payload, Mapping):
        return None
    rebound = dict(publication_payload)
    rebound["trace_context"] = _rebound_trace_context_payload(
        rebound.get("trace_context"),
        active_request_id,
    )
    return rebound


def _rebind_branch_state_request_id(
    branch_state_payload: Mapping[str, Any] | None,
    active_request_id: str,
) -> dict[str, Any] | None:
    if not isinstance(branch_state_payload, Mapping):
        return None
    rebound = dict(branch_state_payload)
    rebound["publications"] = [
        _rebind_branch_publication_request_id(payload, active_request_id)
        for payload in rebound.get("publications", [])
        if payload is not None
    ]
    return rebound


def _rebind_branch_resume_snapshot_request_id(
    snapshot_payload: Mapping[str, Any] | None,
    active_request_id: str,
) -> dict[str, Any] | None:
    if not isinstance(snapshot_payload, Mapping):
        return None
    rebound = dict(snapshot_payload)
    branch_plan = dict(rebound.get("branch_plan") or {})
    branch_plan["request_id"] = active_request_id
    branch_plan["trace_context"] = _rebound_trace_context_payload(
        branch_plan.get("trace_context"),
        active_request_id,
    )
    rebound["branch_plan"] = branch_plan
    rebound["active_frame"] = _rebind_frame_snapshot_request_id(
        rebound.get("active_frame"),
        active_request_id,
    )
    rebound["queued_frames"] = [
        _rebind_frame_snapshot_request_id(frame_payload, active_request_id)
        for frame_payload in rebound.get("queued_frames", [])
        if frame_payload is not None
    ]
    rebound["branch_publications"] = [
        _rebind_branch_publication_request_id(publication_payload, active_request_id)
        for publication_payload in rebound.get("branch_publications", [])
        if publication_payload is not None
    ]
    rebound["side_effect_receipts"] = [
        _rebind_side_effect_receipt_request_id(receipt_payload, active_request_id)
        for receipt_payload in rebound.get("side_effect_receipts", [])
        if receipt_payload is not None
    ]
    return rebound


def _rebind_side_effect_receipt_request_id(
    receipt_payload: Mapping[str, Any] | None,
    active_request_id: str,
) -> dict[str, Any] | None:
    if not isinstance(receipt_payload, Mapping):
        return None
    rebound = dict(receipt_payload)
    rebound["request_id"] = active_request_id
    rebound["trace_context"] = _rebound_trace_context_payload(
        rebound.get("trace_context"),
        active_request_id,
    )
    return rebound


def rebind_checkpoint_envelope_for_resume(
    envelope: CheckpointEnvelope,
    *,
    active_request_id: str,
    source_checkpoint_ref: str | None = None,
) -> CheckpointEnvelope:
    payload = model_dump(model_copy(envelope, deep=True))
    original_request_id = (
        str(payload.get("origin_request_id") or payload.get("request_id") or "").strip()
        or str(envelope.request_id)
    )
    payload["request_id"] = active_request_id
    payload["origin_request_id"] = original_request_id
    payload["source_checkpoint_ref"] = (
        str(payload.get("source_checkpoint_ref") or source_checkpoint_ref or "").strip() or None
    )
    plan_snapshot = dict(payload.get("plan_snapshot") or {})
    plan_snapshot["request_id"] = active_request_id
    plan_snapshot["trace_context"] = _rebound_trace_context_payload(
        plan_snapshot.get("trace_context"),
        active_request_id,
    )
    payload["plan_snapshot"] = plan_snapshot
    runtime_state_snapshot = dict(payload.get("runtime_state_snapshot") or {})
    runtime_state_snapshot["request_id"] = active_request_id
    runtime_state_snapshot["active_frame"] = _rebind_frame_snapshot_request_id(
        runtime_state_snapshot.get("active_frame"),
        active_request_id,
    )
    runtime_state_snapshot["queued_frames"] = [
        _rebind_frame_snapshot_request_id(frame_payload, active_request_id)
        for frame_payload in runtime_state_snapshot.get("queued_frames", [])
        if frame_payload is not None
    ]
    runtime_state_snapshot["branch_states"] = {
        str(key): _rebind_branch_state_request_id(value, active_request_id)
        for key, value in dict(runtime_state_snapshot.get("branch_states", {})).items()
        if value is not None
    }
    runtime_state_snapshot["branch_publications"] = [
        _rebind_branch_publication_request_id(publication_payload, active_request_id)
        for publication_payload in runtime_state_snapshot.get("branch_publications", [])
        if publication_payload is not None
    ]
    runtime_state_snapshot["branch_resume_snapshots"] = {
        str(key): _rebind_branch_resume_snapshot_request_id(value, active_request_id)
        for key, value in dict(runtime_state_snapshot.get("branch_resume_snapshots", {})).items()
        if value is not None
    }
    payload["runtime_state_snapshot"] = runtime_state_snapshot
    side_effect_ledger = dict(payload.get("side_effect_ledger") or {})
    side_effect_ledger["receipts"] = [
        _rebind_side_effect_receipt_request_id(receipt_payload, active_request_id)
        for receipt_payload in side_effect_ledger.get("receipts", [])
        if receipt_payload is not None
    ]
    payload["side_effect_ledger"] = side_effect_ledger
    payload["working_state_summary"] = _rebind_request_id_mirrors(
        payload.get("working_state_summary", {}),
        active_request_id,
    )
    payload["trace_cursor"] = _rebind_request_id_mirrors(
        payload.get("trace_cursor", {}),
        active_request_id,
    )
    return model_validate(CheckpointEnvelope, payload)


def solve_request_from_resume_checkpoint(
    envelope: CheckpointEnvelope,
    *,
    request_id_override: str | None = None,
    request_bundle: Mapping[str, Any] | None = None,
    source_checkpoint_ref: str | None = None,
) -> tuple[SolveRequest, CheckpointEnvelope, str]:
    effective_request_id = str(request_id_override or envelope.request_id).strip() or envelope.request_id
    rebound_envelope = rebind_checkpoint_envelope_for_resume(
        envelope,
        active_request_id=effective_request_id,
        source_checkpoint_ref=source_checkpoint_ref,
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
        original_request = model_validate(RuntimeSolveRequest, dict(payload))
        if original_request.mode == "user_request" and original_request.solve_request is not None:
            solve_request = model_validate(SolveRequest, model_dump(original_request.solve_request))
            return model_copy(solve_request, update={"request_id": effective_request_id}), rebound_envelope, effective_request_id

    raise ValueError(
        "resume for user_request checkpoints requires the stored runtime_solve_request envelope with solve_request payload"
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
            recoverability="checkpoint_available" if latest_checkpoint_ref else "none",
            verified=False,
            best_effort=False,
        ),
    )


def inspect_request_for_runtime(
    *,
    request_id: str,
    requested_backend: str,
    runtime_abi: str,
    kernel_version: str,
    storage_schema_version: str,
) -> InspectRequest:
    return InspectRequest(
        request_id=request_id,
        requested_backend=requested_backend,
        expected_runtime_abi=runtime_abi,
        expected_kernel_version=kernel_version,
        expected_storage_schema_version=storage_schema_version,
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
    return RuntimeSolveRequest(
        request_id=normalized_request_id,
        evaluation_unit_id=normalized_request_id,
        runtime_backend=runtime_backend,
        mode="benchmark",
        seed=int(seed),
        task=task,
        budget_overrides=dict(budget_overrides or {}),
        trace_context=runtime_trace_context(
            trace_context,
            request_id=normalized_request_id,
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
) -> RuntimeSolveRequest:
    effective_solve_request = solve_request
    if not solve_request.request_file_refs:
        effective_solve_request = solve_request.copy(
            update={
                "request_file_refs": _compiled_request_file_refs(solve_request),
                "file_paths": _request_file_source_paths(solve_request),
            }
        )
    return RuntimeSolveRequest(
        request_id=effective_solve_request.request_id,
        evaluation_unit_id=effective_solve_request.request_id,
        runtime_backend=runtime_backend,
        mode="user_request",
        seed=int(seed),
        solve_request=effective_solve_request,
        budget_overrides=dict(effective_solve_request.budget_overrides),
        trace_context=runtime_trace_context(
            trace_context,
            request_id=effective_solve_request.request_id,
            seed=seed,
            objective=effective_solve_request.prompt,
        ),
    )


def runtime_batch_request_for_tasks(
    *,
    request_id: str,
    runtime_backend: str,
    task_runs: list[tuple[BenchmarkTask, int]],
    budget_overrides: dict[str, Any] | None = None,
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
            episode_kind = "benchmark_duplicate" if total_counts.get(duplicate_key, 0) > 1 else "single_task"
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
                    request_id=request_key,
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
            request_id=request_id,
        ),
    )


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
    recoverability = "none"
    if latest_checkpoint_ref:
        recoverability = "checkpoint_available"
    elif not run.hard_invalid and not controlled_failure:
        recoverability = "terminal"
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
        recoverability=recoverability,
        verified=verified,
        best_effort=best_effort,
    )
