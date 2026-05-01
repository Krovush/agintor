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

_PROMPT_ABSOLUTE_PATH_RE = re.compile(r"(?P<path>(?:[A-Za-z]:[\\/]|/)[^\n\r\t\"'<>|]+)")


_TRAILING_PATH_PUNCTUATION = "\"'`,;:!?)]}"


_PATH_CLAUSE_BOUNDARY_WORDS = "to|and|using|then|that|which|please|for|by|with|while"


def _normalized_request_prompt(text: str) -> str:
    return str(text or "").strip()


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
        rf"^(?P<path>.+\.[A-Za-z0-9]{{1,8}})(?=(?:\s+(?:{_PATH_CLAUSE_BOUNDARY_WORDS})\b|$))",
        candidate,
        flags=re.IGNORECASE,
    )
    if extension_match:
        trimmed = _clean_prompt_path(extension_match.group("path"))
        if Path(trimmed).expanduser().is_absolute():
            return str(Path(trimmed).expanduser().resolve(strict=False))
    clause_patterns = (
        rf"\s+(?:{_PATH_CLAUSE_BOUNDARY_WORDS})\b.*$",
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
        (RequestFileRef).model_validate(row)
        for row in payload.get("request_file_refs", [])
        if isinstance(row, Mapping)
    ] or [
        compile_request_file_ref(path)
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
    request_id_payload["request_file_refs"] = [(file_ref).model_dump() for file_ref in request_file_refs]
    request_id = str(payload.get("request_id", "")).strip() or f"solve.{stable_hash(request_id_payload)[:12]}"
    return SolveRequest(request_id=request_id, **request_payload)


def benchmark_task_to_solve_request(task: BenchmarkTask, *, request_id: str | None = None) -> SolveRequest:
    verification_preference = "verified_if_available"
    if task.allow_best_effort:
        verification_preference = "best_effort"
    elif task.verification_required:
        verification_preference = "required"
    request_file_refs = [compile_request_file_ref(path) for path in task.file_paths]
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


def compile_request_file_ref(path_text: str) -> RequestFileRef:
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
        return [(RequestFileRef).model_validate((ref).model_dump()) for ref in request.request_file_refs]
    explicit = _dedupe_prompt_paths(list(request.file_paths))
    raw_paths = explicit or _dedupe_prompt_paths(_extract_prompt_file_paths(request.prompt))
    return [compile_request_file_ref(path) for path in raw_paths]


def _request_file_source_paths(request: SolveRequest) -> list[str]:
    return [file_ref.source_path for file_ref in _compiled_request_file_refs(request)]


def _request_file_paths(request: SolveRequest) -> list[str]:
    return [file_ref.runtime_path for file_ref in _compiled_request_file_refs(request)]
