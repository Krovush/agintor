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
    resolve_trace_context,
    resolve_trace_session_id,
    trace_grouping_key,
)
from .layout import (
    _calls_dir,
    _session_dir,
    _slug,
    _write_text,
)
from .materialization import (
    _LOCK,
    _write_grouped_views,
    _write_index,
)
from .rendering import (
    _build_markdown,
    _jsonable,
    _load_trace_records,
    render_trace_subset,
)

_CALL_COUNTER = count(1)


_WRITE_COUNTER = count(1)


def persist_openai_trace(
    *,
    provider: str,
    method_name: str,
    model_class: str,
    model_name: str,
    reasoning_effort: str | None,
    instructions: str,
    input_value: Any,
    request_payload: Mapping[str, Any],
    request_metadata: Mapping[str, Any] | None,
    response: Any = None,
    response_text: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    total_tokens: int = 0,
    latency_s: float = 0.0,
    error: str | None = None,
) -> str | None:
    try:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        purpose = str((request_metadata or {}).get("mode", "unspecified")).strip() or "unspecified"
        trace_context = resolve_trace_context(request_metadata)
        session_id = resolve_trace_session_id(trace_context.session_id)
        grouping = trace_grouping_key(trace_context)
        request_metadata_payload = _jsonable(request_metadata or {})
        if isinstance(request_metadata_payload, dict):
            request_metadata_payload["trace_context"] = (trace_context).model_dump()
        raw_response = _jsonable(response)
        with _LOCK:
            ordinal = next(_CALL_COUNTER)
            stem = "__".join(
                [
                    timestamp.split("_", 1)[0] + "Z",
                    f"c{ordinal:04d}",
                    _slug(purpose, fallback="purpose")[:16],
                    stable_hash(os.getpid(), timestamp, method_name, model_name)[:12],
                ]
            )
            record = {
                "call_id": stem,
                "call_ordinal": ordinal,
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "provider": provider,
                "provider_role": trace_context.provider_role or "",
                "method_name": method_name,
                "purpose": purpose,
                "session_id": session_id,
                "trace_context": (trace_context).model_dump(),
                "model_class": model_class,
                "model_name": model_name,
                "reasoning_effort": reasoning_effort,
                "instructions": instructions,
                "input": stringify_response_input(input_value),
                "request_payload": _jsonable(request_payload),
                "request_metadata": request_metadata_payload,
                "response_id": getattr(response, "id", None),
                "status": getattr(response, "status", None),
                "input_tokens": int(input_tokens or 0),
                "output_tokens": int(output_tokens or 0),
                "total_tokens": int(total_tokens or 0),
                "latency_s": float(latency_s or 0.0),
                "response_text": response_text,
                "response_raw": raw_response,
                "error": str(error or ""),
            }
            if grouping is not None:
                group_kind, group_key = grouping
                record["trace_group_kind"] = group_kind
                record["trace_group_key"] = group_key
            calls_dir = _calls_dir(session_id)
            json_path = calls_dir / f"{stem}.json"
            md_path = calls_dir / f"{stem}.md"
            _write_text(json_path, json.dumps(record, indent=2, sort_keys=True))
            _write_text(md_path, _build_markdown(record))
            session_dir = _session_dir(session_id)
            _write_index(session_dir)
            _write_grouped_views(session_dir, _load_trace_records(session_dir))
            return stem
    except Exception as exc:
        raise RuntimeError("failed to persist OpenAI trace") from exc


def _main() -> int:
    parser = argparse.ArgumentParser(description="Render human-readable OpenAI trace subsets.")
    parser.add_argument("--session-dir", required=True, help="Trace session directory containing a calls/ folder.")
    parser.add_argument("--output-dir", required=True, help="Destination directory for the reformatted subset.")
    parser.add_argument("--start", type=int, default=0, help="Zero-based starting call index.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of calls to render.")
    parser.add_argument(
        "--first-half",
        action="store_true",
        help="Render the first half of the available calls. Overrides --start/--limit when used alone.",
    )
    args = parser.parse_args()

    records = _load_trace_records(Path(args.session_dir))
    start = args.start
    limit = args.limit
    if args.first_half and args.limit is None and args.start == 0:
        limit = (len(records) + 1) // 2
    result = render_trace_subset(
        args.session_dir,
        output_dir=args.output_dir,
        start=start,
        limit=limit,
    )
    print(json.dumps(result, indent=2))
    return 0
