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
    _TRAILING_PATH_PUNCTUATION,
    _normalize_path_key,
)

_NUMBER_LIST_RE = re.compile(r"\[([^\]]+)\]")


_MODULUS_RE = re.compile(r"\bmod(?:ulo)?\s+(-?\d+(?:\.\d+)?)", re.IGNORECASE)


_SYMBOL_RE = re.compile(r"\b([A-Z][A-Z0-9_]{2,})\b")


_URL_RE = re.compile(r"(?P<url>[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>]+)", re.IGNORECASE)


_HTTP_METHOD_RE = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\b", re.IGNORECASE)


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
