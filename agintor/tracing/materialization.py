from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import socket
import threading
import textwrap
import time
from datetime import datetime, timezone
from itertools import count
from pathlib import Path
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, Field

from ..providers.base import stringify_response_input
from ..contracts import OpenAITraceContext
from ..utils import ensure_directory, stable_hash

from .identity import (
    TRACE_GROUP_BENCHMARK_TASK,
    TRACE_GROUP_FACTORY_MESSAGE,
    TRACE_GROUP_RUNTIME_SESSION_MESSAGE,
    trace_grouping_key,
)
from .layout import (
    _session_dir,
    _slug,
    _trace_group_view_dir,
    _write_text,
)
from .rendering import (
    _build_api_flow_call,
    _load_trace_records,
    _mapping,
)

_LOCK = threading.RLock()

TRACE_MATERIALIZATION_SCHEMA_VERSION = "agintor.trace-materialization.v1"


class TraceMaterializationState(BaseModel):
    session_id: str
    session_dir: str
    schema_version: str = TRACE_MATERIALIZATION_SCHEMA_VERSION
    last_finalized_call_id: str | None = None
    known_call_ids: list[str] = Field(default_factory=list)
    materialized_build_ids: list[str] = Field(default_factory=list)
    materialized_solve_request_ids: list[str] = Field(default_factory=list)
    materialized_factory_message_keys: list[str] = Field(default_factory=list)
    materialized_runtime_session_message_keys: list[str] = Field(default_factory=list)
    materialized_benchmark_task_keys: list[str] = Field(default_factory=list)
    pending_build_ids: list[str] = Field(default_factory=list)
    pending_solve_request_ids: list[str] = Field(default_factory=list)
    pending_factory_message_keys: list[str] = Field(default_factory=list)
    pending_runtime_session_message_keys: list[str] = Field(default_factory=list)
    pending_benchmark_task_keys: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    updated_at: float = 0.0
    call_count: int = 0
    grouped_call_count: int = 0
    factory_message_keys: list[str] = Field(default_factory=list)
    runtime_session_message_keys: list[str] = Field(default_factory=list)
    benchmark_task_keys: list[str] = Field(default_factory=list)
    rebuilt_at: str = ""


def _write_index(root: Path) -> None:
    calls_dir = root / "calls"
    rows: list[dict[str, Any]] = []
    for json_path in sorted(calls_dir.glob("*.json")):
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        rows.append(
            {
                "call_id": payload.get("call_id", json_path.stem),
                "timestamp_utc": payload.get("timestamp_utc", ""),
                "purpose": payload.get("purpose", ""),
                "method_name": payload.get("method_name", ""),
                "model_name": payload.get("model_name", ""),
                "status": payload.get("status", ""),
                "total_tokens": payload.get("total_tokens", 0),
                "latency_s": payload.get("latency_s", 0.0),
                "markdown_file": f"{json_path.stem}.md",
                "json_file": json_path.name,
            }
        )
    lines = [
        "# OpenAI Trace Index",
        "",
        "| Timestamp UTC | Purpose | Method | Model | Status | Tokens | Latency | Chat Trace | Raw JSON |",
        "| --- | --- | --- | --- | --- | ---: | ---: | --- | --- |",
    ]
    for row in rows:
        lines.append(
            "| "
            + f"{row['timestamp_utc']} | {row['purpose']} | {row['method_name']} | {row['model_name']} | "
            + f"{row['status']} | {row['total_tokens']} | {row['latency_s']:.3f} | "
            + f"[{row['call_id']}](calls/{row['markdown_file']}) | [json](calls/{row['json_file']}) |"
        )
    _write_text(root / "INDEX.md", "\n".join(lines) + "\n")


def _trace_record_sort_key(record: Mapping[str, Any]) -> tuple[int, str, str]:
    return (
        int(record.get("call_ordinal", 0) or 0),
        str(record.get("timestamp_utc", "")),
        str(record.get("call_id", "")),
    )


def _record_trace_context(record: Mapping[str, Any], errors: list[str]) -> OpenAITraceContext | None:
    trace_context = _mapping(record.get("trace_context"))
    if not trace_context:
        return None
    try:
        return (OpenAITraceContext).model_validate(trace_context)
    except Exception:
        call_id = str(record.get("call_id") or "").strip() or "unknown_call"
        errors.append(f"failed_to_parse_trace_context:{call_id}")
        return None


def _write_trace_view(output_dir: Path, title: str, records: Sequence[Mapping[str, Any]]) -> None:
    destination = ensure_directory(output_dir)
    ordered = sorted(records, key=_trace_record_sort_key)
    transcript_lines = [f"# {title}", "", f"Calls: {len(ordered)}", ""]
    index_lines = [
        f"# {title} Index",
        "",
        "| # | Call ID | Purpose | Model | Status | Raw JSON |",
        "| ---: | --- | --- | --- | --- | --- |",
    ]
    for ordinal, record in enumerate(ordered, start=1):
        transcript_lines.append(_build_api_flow_call(record, header_level="##", ordinal=ordinal).rstrip())
        transcript_lines.append("")
        json_path = Path(str(record.get("_json_path") or ""))
        json_ref = os.path.relpath(json_path, start=destination).replace("\\", "/") if str(json_path) else ""
        index_lines.append(
            "| "
            + f"{ordinal} | {record.get('call_id', '')} | {record.get('purpose', '')} | "
            + f"{record.get('model_name', '')} | {record.get('status', '')} | "
            + (f"[json]({json_ref})" if json_ref else "")
            + " |"
        )
    _write_text(destination / "TRANSCRIPT.md", "\n".join(transcript_lines).rstrip() + "\n")
    _write_text(destination / "INDEX.md", "\n".join(index_lines).rstrip() + "\n")


def _reset_derived_group_dirs(session_dir: Path) -> None:
    for name in (
        "groups",
        "builds",
        "solves",
        "runtime_tasks",
        "factory_projects",
        "runtime_sessions",
        "benchmark_tasks",
    ):
        path = session_dir / name
        if path.exists():
            shutil.rmtree(path)


def _write_grouped_views(session_dir: Path, records: Sequence[Mapping[str, Any]]) -> TraceMaterializationState:
    _reset_derived_group_dirs(session_dir)
    ordered_records = sorted(records, key=_trace_record_sort_key)
    _write_trace_view(session_dir, f"OpenAI Trace Session {session_dir.name}", ordered_records)

    build_groups: dict[str, list[Mapping[str, Any]]] = {}
    solve_groups: dict[str, list[Mapping[str, Any]]] = {}
    factory_message_groups: dict[str, tuple[OpenAITraceContext, list[Mapping[str, Any]]]] = {}
    runtime_session_message_groups: dict[str, tuple[OpenAITraceContext, list[Mapping[str, Any]]]] = {}
    benchmark_task_groups: dict[str, tuple[OpenAITraceContext, list[Mapping[str, Any]]]] = {}
    errors: list[str] = []
    for record in ordered_records:
        trace_context = _record_trace_context(record, errors)
        if trace_context is None:
            continue
        build_id = str(trace_context.build_id or "").strip()
        request_id = str(trace_context.request_id or "").strip()
        if build_id:
            build_groups.setdefault(build_id, []).append(record)
        if request_id:
            solve_groups.setdefault(request_id, []).append(record)
        grouping = trace_grouping_key(trace_context)
        if grouping is None:
            continue
        group_kind, group_key = grouping
        if group_kind == TRACE_GROUP_FACTORY_MESSAGE:
            factory_message_groups.setdefault(group_key, (trace_context, []))[1].append(record)
        elif group_kind == TRACE_GROUP_RUNTIME_SESSION_MESSAGE:
            runtime_session_message_groups.setdefault(group_key, (trace_context, []))[1].append(record)
        elif group_kind == TRACE_GROUP_BENCHMARK_TASK:
            benchmark_task_groups.setdefault(group_key, (trace_context, []))[1].append(record)

    for build_id, rows in sorted(build_groups.items()):
        _write_trace_view(session_dir / "builds" / _slug(build_id, fallback="build"), f"Build {build_id}", rows)
    for request_id, rows in sorted(solve_groups.items()):
        _write_trace_view(session_dir / "solves" / _slug(request_id, fallback="request"), f"Solve {request_id}", rows)

    factory_message_keys = _emit_grouped_view(
        session_dir,
        factory_message_groups,
        TRACE_GROUP_FACTORY_MESSAGE,
        "Factory Message",
    )
    runtime_session_message_keys = _emit_grouped_view(
        session_dir,
        runtime_session_message_groups,
        TRACE_GROUP_RUNTIME_SESSION_MESSAGE,
        "Runtime Session Message",
    )
    benchmark_task_keys = _emit_grouped_view(
        session_dir,
        benchmark_task_groups,
        TRACE_GROUP_BENCHMARK_TASK,
        "Benchmark Task",
    )
    grouped_call_count = (
        sum(len(rows) for _, rows in factory_message_groups.values())
        + sum(len(rows) for _, rows in runtime_session_message_groups.values())
        + sum(len(rows) for _, rows in benchmark_task_groups.values())
    )

    known_call_ids = [
        str(record.get("call_id") or "").strip()
        for record in ordered_records
        if str(record.get("call_id") or "").strip()
    ]
    updated_at = datetime.now(timezone.utc).timestamp()
    state = TraceMaterializationState(
        session_id=session_dir.name,
        session_dir=str(session_dir),
        schema_version=TRACE_MATERIALIZATION_SCHEMA_VERSION,
        last_finalized_call_id=known_call_ids[-1] if known_call_ids else None,
        known_call_ids=known_call_ids,
        materialized_build_ids=sorted(build_groups),
        materialized_solve_request_ids=sorted(solve_groups),
        materialized_factory_message_keys=factory_message_keys,
        materialized_runtime_session_message_keys=runtime_session_message_keys,
        materialized_benchmark_task_keys=benchmark_task_keys,
        pending_build_ids=[],
        pending_solve_request_ids=[],
        pending_factory_message_keys=[],
        pending_runtime_session_message_keys=[],
        pending_benchmark_task_keys=[],
        errors=errors,
        updated_at=updated_at,
        call_count=len(ordered_records),
        grouped_call_count=grouped_call_count,
        factory_message_keys=factory_message_keys,
        runtime_session_message_keys=runtime_session_message_keys,
        benchmark_task_keys=benchmark_task_keys,
        rebuilt_at=datetime.fromtimestamp(updated_at, timezone.utc).isoformat(),
    )
    _write_text(session_dir / "materialization_state.json", json.dumps((state).model_dump(), indent=2, sort_keys=True))
    return state


def _emit_grouped_view(
    session_dir: Path,
    groups: Mapping[str, tuple[OpenAITraceContext, Sequence[Mapping[str, Any]]]],
    group_kind: str,
    title_prefix: str,
) -> list[str]:
    keys: list[str] = []
    for key, (trace_context, rows) in sorted(groups.items()):
        view_dir = _trace_group_view_dir(session_dir, group_kind, trace_context)
        if view_dir is None:
            continue
        keys.append(key)
        _write_trace_view(view_dir, f"{title_prefix} {key}", rows)
    return keys


def rebuild_trace_materialization(session_dir: Path | str | None = None) -> TraceMaterializationState:
    session_path = Path(session_dir) if session_dir is not None else _session_dir()
    with _LOCK:
        records = _load_trace_records(session_path)
        _write_index(session_path)
        return _write_grouped_views(session_path, records)


def load_materialization_state(session_dir: Path | str) -> TraceMaterializationState | None:
    path = Path(session_dir) / "materialization_state.json"
    if not path.exists():
        return None
    try:
        return (TraceMaterializationState).model_validate(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return None
