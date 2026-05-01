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
    _slug,
    trace_session_dir_name,
)

_WRITE_COUNTER = count(1)

def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _trace_root() -> Path:
    configured = str(os.environ.get("AGINTOR_OPENAI_TRACE_DIR", "")).strip()
    if configured:
        return ensure_directory(Path(configured))
    return ensure_directory(_repo_root() / "openai_api_traces")


def _session_dir(session_id: str | None = None) -> Path:
    return ensure_directory(_trace_root() / "sessions" / trace_session_dir_name(session_id))


def _calls_dir(session_id: str | None = None) -> Path:
    return ensure_directory(_session_dir(session_id) / "calls")


def _factory_message_view_dir(
    session_dir: Path,
    trace_context: OpenAITraceContext,
) -> Path | None:
    chat = str(trace_context.factory_chat_id or "").strip()
    message = str(trace_context.factory_message_id or "").strip()
    if not chat or not message:
        return None
    if trace_context.factory_message_index is not None:
        message_slug = f"m{int(trace_context.factory_message_index)}_{_slug(message, fallback='message')}"
    else:
        message_slug = _slug(message, fallback="message")
    return (
        session_dir
        / "factory_projects"
        / _slug(chat, fallback="chat")
        / message_slug
    )


def _runtime_session_message_view_dir(
    session_dir: Path,
    trace_context: OpenAITraceContext,
) -> Path | None:
    runtime = str(trace_context.runtime_hash or "").strip()
    session = str(trace_context.runtime_session_id or "").strip()
    message = str(trace_context.runtime_message_id or "").strip()
    if not runtime or not session or not message:
        return None
    if trace_context.runtime_message_index is not None:
        message_slug = f"m{int(trace_context.runtime_message_index)}_{_slug(message, fallback='message')}"
    else:
        message_slug = _slug(message, fallback="message")
    return (
        session_dir
        / "runtime_sessions"
        / _slug(runtime, fallback="runtime")
        / _slug(session, fallback="session")
        / message_slug
    )


def _benchmark_task_view_dir(
    session_dir: Path,
    trace_context: OpenAITraceContext,
) -> Path | None:
    request_or_unit = str(trace_context.evaluation_unit_id or trace_context.request_id or "").strip()
    task = str(trace_context.task_id or "").strip()
    runtime = str(trace_context.runtime_hash or "").strip()
    if not request_or_unit or not task or trace_context.seed is None or not runtime:
        return None
    request_slug = _slug(request_or_unit, fallback="request")
    if (
        str(trace_context.episode_kind or "").strip() == "transfer_episode"
        and trace_context.episode_step_index is not None
    ):
        leaf = f"{request_slug}__step_{int(trace_context.episode_step_index)}"
    else:
        leaf = request_slug
    return (
        session_dir
        / "benchmark_tasks"
        / _slug(task, fallback="task")
        / f"seed_{int(trace_context.seed)}"
        / _slug(runtime, fallback="runtime")
        / leaf
    )


def _trace_group_view_dir(
    session_dir: Path,
    group_kind: str,
    trace_context: OpenAITraceContext,
) -> Path | None:
    if group_kind == TRACE_GROUP_FACTORY_MESSAGE:
        return _factory_message_view_dir(session_dir, trace_context)
    if group_kind == TRACE_GROUP_RUNTIME_SESSION_MESSAGE:
        return _runtime_session_message_view_dir(session_dir, trace_context)
    if group_kind == TRACE_GROUP_BENCHMARK_TASK:
        return _benchmark_task_view_dir(session_dir, trace_context)
    return None


def _write_text(path: Path, text: str) -> None:
    ensure_directory(path.parent)
    last_error: PermissionError | None = None
    for attempt in range(6):
        temp_path = path.with_name(f".tmp-{os.getpid()}-{next(_WRITE_COUNTER)}-{stable_hash(path.name)[:8]}")
        try:
            temp_path.write_text(text, encoding="utf-8")
            os.replace(temp_path, path)
            return
        except PermissionError as exc:
            last_error = exc
            if attempt == 5:
                raise
            time.sleep(0.025 * (attempt + 1))
        finally:
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except OSError:
                pass
    if last_error is not None:
        raise last_error
