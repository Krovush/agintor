from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .benchmarks import BenchmarkTask
from .providers import ModelProvider
from .runtime_profile import RuntimeProfile, default_runtime_profile
from .schemas import AgentTemplate, Checkpoint, ModelResponse, OperationSpec, RunResult, SolveRequest, SolveResult
from .utils import stable_hash


_NUMBER_LIST_RE = re.compile(r"\[([^\]]+)\]")
_MODULUS_RE = re.compile(r"\bmod(?:ulo)?\s+(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_SYMBOL_RE = re.compile(r"\b([A-Z][A-Z0-9_]{2,})\b")
_PROMPT_PATH_RE = re.compile(r"(?P<path>(?:[A-Za-z]:[\\/]|/)[^\s\"'<>|]+)")
_TRAILING_PATH_PUNCTUATION = "\"'`,;:!?)]}"


@dataclass
class AgentFrame:
    agent: AgentTemplate
    objective: str
    operation_ids: list[str]
    depth: int
    checkpoint: Checkpoint | None = None
    parent_id: str | None = None
    worker_id: str | None = None
    role: str = "root"
    tool_scope: list[str] = field(default_factory=list)
    model_class: str = "small"
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
    subgoal_negative_steps: dict[str, int] = field(default_factory=dict)
    subgoal_last_model: dict[str, str] = field(default_factory=dict)
    last_unresolved_goal: str | None = None


@dataclass
class PolicyContext:
    runtime_dir: Path
    shell: Any
    task: BenchmarkTask
    provider: ModelProvider
    seed: int
    state: RuntimeState
    budget: RuntimeBudget
    trace: list[dict[str, Any]]
    objective: str
    profile: RuntimeProfile | None = None

    def __post_init__(self) -> None:
        if self.profile is None:
            self.profile = default_runtime_profile()

    def record(self, event: str, **payload: Any) -> None:
        self.trace.append({"event": event, **payload})

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


def _extract_prompt_file_paths(prompt: str) -> list[str]:
    matches: list[str] = []
    seen: set[str] = set()
    for match in _PROMPT_PATH_RE.finditer(prompt):
        raw_path = _clean_prompt_path(match.group("path"))
        if not raw_path:
            continue
        key = _normalize_path_key(raw_path)
        if not key or key in seen:
            continue
        seen.add(key)
        matches.append(raw_path)
    return matches


def _category_allowed(allowed_categories: list[str], required_category: str | None) -> bool:
    if not required_category:
        return True
    normalized_allowed = [
        str(category or "").strip().strip("/").lower()
        for category in allowed_categories
        if str(category or "").strip()
    ]
    if not normalized_allowed:
        return True
    category_key = str(required_category).strip().strip("/").lower()
    return any(
        category_key == allowed or category_key.startswith(f"{allowed}/")
        for allowed in normalized_allowed
    )


def load_solve_request(prompt: str | None = None, prompt_file: str | Path | None = None) -> SolveRequest:
    payload: dict[str, Any] = {}
    prompt_text = _normalized_request_prompt(prompt or "")
    if prompt_file is not None:
        raw = Path(prompt_file).read_text(encoding="utf-8")
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
    request_payload = {
        "prompt": prompt_text,
        "context_items": _coerce_context_items(payload.get("context_items")),
        "file_paths": _coerce_string_list(payload.get("file_paths")),
        "output_schema": dict(payload.get("output_schema", {})) if isinstance(payload.get("output_schema", {}), dict) else {},
        "allowed_tool_categories": _coerce_string_list(payload.get("allowed_tool_categories")),
        "verification_preference": verification_preference,
        "budget_overrides": dict(payload.get("budget_overrides", {})) if isinstance(payload.get("budget_overrides", {}), dict) else {},
    }
    request_id = str(payload.get("request_id", "")).strip() or f"solve.{stable_hash(request_payload)[:12]}"
    return SolveRequest(request_id=request_id, **request_payload)


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


def solve_request_to_task(request: SolveRequest) -> BenchmarkTask:
    prompt = request.prompt
    prompt_lower = prompt.lower()
    request_meta = {
        "request_id": request.request_id,
        "solve_mode": "user_request",
        "output_schema": request.output_schema,
        "allowed_tool_categories": list(request.allowed_tool_categories),
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
    file_paths = list(request.file_paths)
    if not file_paths:
        file_paths = _extract_prompt_file_paths(prompt)
    if file_paths:
        expected_owner = _find_file_owner(request, file_paths[0])
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
                file_paths=file_paths,
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
    verification_required, allow_best_effort = _verification_policy(
        request.verification_preference,
        exact_verifier_exists=False,
    )
    return BenchmarkTask(
        task_id=f"user.{request.request_id}.best_effort",
        family="e2e",
        prompt=prompt,
        task_type="user_request",
        file_paths=list(file_paths),
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
        metadata=request_meta,
    )


def solve_result_from_run_result(request: SolveRequest, run: RunResult, runtime_hash: str) -> SolveResult:
    trace_rows = run.trace_rows()
    checks = [
        {
            "checker": row.get("checker"),
            "passed": row.get("passed"),
        }
        for row in trace_rows
        if row.get("event") == "check_result"
    ]
    verified = run.verifier_score >= 1.0 and not run.hard_invalid
    controlled_failure = isinstance(run.artifact, dict) and run.artifact.get("error") == "controlled_failure"
    best_effort = not verified and not controlled_failure and not run.hard_invalid
    if run.hard_invalid:
        status = "failed"
        summary = run.invalid_reason or "runtime execution failed"
    elif controlled_failure:
        status = "controlled_failure"
        summary = "No verified terminal artifact was available under the task contract."
    elif verified:
        status = "verified"
        summary = "The runtime produced a verified artifact."
    else:
        status = "best_effort"
        summary = "The runtime produced a best-effort artifact without exact verification."
    return SolveResult(
        request_id=request.request_id,
        runtime_hash=runtime_hash,
        artifact=run.artifact,
        status=status,
        summary=summary,
        checks=checks,
        trace_ref=run.trace_ref(),
        budget={
            "cost": run.cost,
            "latency": run.latency,
            "model_calls": run.model_calls,
            "checks_used": run.checks_used,
            "tokens_used": run.tokens_used,
            "input_tokens": run.input_tokens,
            "output_tokens": run.output_tokens,
        },
        faults={
            "count": run.faults,
            "hard_invalid": run.hard_invalid,
            "invalid_reason": run.invalid_reason,
        },
        verified=verified,
        best_effort=best_effort,
    )
