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

_HOST_SESSION_ID = (
    "session."
    + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    + f".pid{os.getpid()}."
    + stable_hash(socket.gethostname())[:8]
)


def _slug(value: str, *, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip()).strip("._")
    return (cleaned or fallback)[:96]


def _derived_host_session_id() -> str:
    return str(os.environ.get("AGINTOR_OPENAI_TRACE_SESSION_ID", "")).strip() or _HOST_SESSION_ID


def resolve_trace_session_id(session_id: str | None = None) -> str:
    explicit = str(session_id or "").strip()
    return explicit or _derived_host_session_id()


def trace_session_dir_name(session_id: str | None = None) -> str:
    return _slug(resolve_trace_session_id(session_id), fallback="session")


def resolve_trace_context(request_metadata: Mapping[str, Any] | None) -> OpenAITraceContext:
    metadata = request_metadata or {}
    raw_context = metadata.get("trace_context") if isinstance(metadata, Mapping) else None
    context: OpenAITraceContext | None = None
    if isinstance(raw_context, OpenAITraceContext):
        context = raw_context
    elif isinstance(raw_context, Mapping):
        try:
            context = (OpenAITraceContext).model_validate(raw_context)
        except Exception:
            context = None
    context = context or OpenAITraceContext()
    session_id = resolve_trace_session_id(context.session_id)
    return (OpenAITraceContext).model_validate({**(context).model_dump(), "session_id": session_id})


TRACE_GROUP_FACTORY_MESSAGE = "factory_message"


TRACE_GROUP_RUNTIME_SESSION_MESSAGE = "runtime_session_message"


TRACE_GROUP_BENCHMARK_TASK = "benchmark_task"


def factory_message_trace_key(
    *,
    factory_chat_id: str | None,
    factory_message_id: str | None,
    factory_message_index: int | None = None,
) -> str:
    chat = str(factory_chat_id or "").strip()
    message = str(factory_message_id or "").strip()
    if not chat or not message:
        return None
    parts = ["factory", chat]
    if factory_message_index is not None:
        parts.append(f"m{int(factory_message_index)}")
    parts.append(message)
    return "|".join(parts)


def runtime_message_trace_key(
    *,
    runtime_hash: str | None,
    runtime_session_id: str | None,
    runtime_message_id: str | None,
    runtime_message_index: int | None = None,
) -> str | None:
    runtime = str(runtime_hash or "").strip()
    session = str(runtime_session_id or "").strip()
    message = str(runtime_message_id or "").strip()
    if not runtime or not session or not message:
        return None
    parts = ["runtime", runtime, "session", session]
    if runtime_message_index is not None:
        parts.append(f"m{int(runtime_message_index)}")
    parts.append(message)
    return "|".join(parts)


def benchmark_task_trace_key(
    *,
    request_id: str | None,
    task_id: str | None,
    seed: int | None,
    runtime_hash: str | None,
    evaluation_unit_id: str | None = None,
    episode_kind: str | None = None,
    episode_step_index: int | None = None,
) -> str | None:
    request_or_unit = str(evaluation_unit_id or request_id or "").strip()
    task = str(task_id or "").strip()
    runtime = str(runtime_hash or "").strip()
    if not request_or_unit or not task or seed is None or not runtime:
        return None
    parts = [task, f"seed_{int(seed)}"]
    parts.extend(_benchmark_episode_key_parts(episode_kind=episode_kind, episode_step_index=episode_step_index))
    parts.extend([runtime, request_or_unit])
    return "|".join(parts)


def _benchmark_episode_key_parts(
    *,
    episode_kind: str | None,
    episode_step_index: int | None,
) -> list[str]:
    if str(episode_kind or "").strip() != "transfer_episode":
        return []
    if episode_step_index is None:
        return []
    return ["episode_transfer_episode", f"step_{int(episode_step_index)}"]


def trace_grouping_key(trace_context: OpenAITraceContext) -> tuple[str, str] | None:
    """Dispatch a trace context to its group kind and stable key.

    Records belong to exactly one of: factory message, runtime session message,
    or benchmark task. A record that supplies none of those identity sets cannot
    be grouped beyond the flat session view.
    """
    if trace_context.request_mode == "benchmark":
        benchmark_key = benchmark_task_trace_key(
            request_id=trace_context.request_id,
            task_id=trace_context.task_id,
            seed=trace_context.seed,
            runtime_hash=trace_context.runtime_hash,
            evaluation_unit_id=trace_context.evaluation_unit_id,
            episode_kind=trace_context.episode_kind,
            episode_step_index=trace_context.episode_step_index,
        )
        if benchmark_key:
            return TRACE_GROUP_BENCHMARK_TASK, benchmark_key
        return None
    factory_key = factory_message_trace_key(
        factory_chat_id=trace_context.factory_chat_id,
        factory_message_id=trace_context.factory_message_id,
        factory_message_index=trace_context.factory_message_index,
    )
    if factory_key:
        return TRACE_GROUP_FACTORY_MESSAGE, factory_key
    runtime_key = runtime_message_trace_key(
        runtime_hash=trace_context.runtime_hash,
        runtime_session_id=trace_context.runtime_session_id,
        runtime_message_id=trace_context.runtime_message_id,
        runtime_message_index=trace_context.runtime_message_index,
    )
    if runtime_key:
        return TRACE_GROUP_RUNTIME_SESSION_MESSAGE, runtime_key
    return None


def runtime_task_trace_key(
    *,
    request_id: str | None,
    task_id: str | None = None,
    seed: int | None = None,
    runtime_hash: str | None = None,
    evaluation_unit_id: str | None = None,
    episode_kind: str | None = None,
    episode_step_index: int | None = None,
) -> str | None:
    """Stable key for a record whose identity comes from benchmark task fields.

    Retained as the trace-grouping key surfaced by checkpoint trace cursors and
    the state store. Factory/runtime-session traces use their own key helpers.
    """
    return benchmark_task_trace_key(
        request_id=request_id,
        task_id=task_id,
        seed=seed,
        runtime_hash=runtime_hash,
        evaluation_unit_id=evaluation_unit_id,
        episode_kind=episode_kind,
        episode_step_index=episode_step_index,
    )
