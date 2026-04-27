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
from datetime import datetime, timezone
from itertools import count
from pathlib import Path
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, Field

from .provider_common import stringify_response_input
from .schemas import OpenAITraceContext
from .utils import ensure_directory, stable_hash


_CALL_COUNTER = count(1)
_WRITE_COUNTER = count(1)
_LOCK = threading.RLock()
TRACE_MATERIALIZATION_SCHEMA_VERSION = "agintor.trace-materialization.v1"
_PATCH_MARKER_RE = re.compile(r"^(?:<{3,7}\s*(?:SEARCH|REPLACE)?|={7}|>{7}\s*REPLACE)\s*$")
_BULLET_RE = re.compile(r"^(\s*(?:[-*+]|\d+\.)\s+)(.*)$")
_HOST_SESSION_ID = (
    "session."
    + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    + f".pid{os.getpid()}."
    + stable_hash(socket.gethostname())[:8]
)


class TraceMaterializationState(BaseModel):
    session_id: str
    session_dir: str
    schema_version: str = TRACE_MATERIALIZATION_SCHEMA_VERSION
    last_finalized_call_id: str | None = None
    known_call_ids: list[str] = Field(default_factory=list)
    materialized_build_ids: list[str] = Field(default_factory=list)
    materialized_solve_request_ids: list[str] = Field(default_factory=list)
    materialized_runtime_task_keys: list[str] = Field(default_factory=list)
    pending_build_ids: list[str] = Field(default_factory=list)
    pending_solve_request_ids: list[str] = Field(default_factory=list)
    pending_runtime_task_keys: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    updated_at: float = 0.0
    call_count: int = 0
    grouped_call_count: int = 0
    runtime_task_keys: list[str] = Field(default_factory=list)
    rebuilt_at: str = ""


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _trace_root() -> Path:
    configured = str(os.environ.get("AGINTOR_OPENAI_TRACE_DIR", "")).strip()
    if configured:
        return ensure_directory(Path(configured))
    return ensure_directory(_repo_root() / "openai_api_traces")


def _derived_host_session_id() -> str:
    return str(os.environ.get("AGINTOR_OPENAI_TRACE_SESSION_ID", "")).strip() or _HOST_SESSION_ID


def resolve_trace_session_id(session_id: str | None = None) -> str:
    explicit = str(session_id or "").strip()
    return explicit or _derived_host_session_id()


def trace_session_dir_name(session_id: str | None = None) -> str:
    return _slug(resolve_trace_session_id(session_id), fallback="session")


def _session_dir(session_id: str | None = None) -> Path:
    return ensure_directory(_trace_root() / "sessions" / trace_session_dir_name(session_id))


def _calls_dir(session_id: str | None = None) -> Path:
    return ensure_directory(_session_dir(session_id) / "calls")


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
    del episode_kind, episode_step_index
    request_or_unit = str(evaluation_unit_id or request_id or "").strip()
    task = str(task_id or "").strip()
    runtime = str(runtime_hash or "").strip()
    if not request_or_unit or not task or seed is None or not runtime:
        return None
    return "|".join([task, f"seed_{int(seed)}", runtime, request_or_unit])


def _runtime_task_key(trace_context: OpenAITraceContext) -> str | None:
    return runtime_task_trace_key(
        request_id=trace_context.request_id,
        task_id=trace_context.task_id,
        seed=trace_context.seed,
        runtime_hash=trace_context.runtime_hash,
        evaluation_unit_id=trace_context.evaluation_unit_id,
        episode_kind=trace_context.episode_kind,
        episode_step_index=trace_context.episode_step_index,
    )


def _runtime_task_view_dir(session_dir: Path, trace_context: OpenAITraceContext) -> Path | None:
    request_or_unit = str(trace_context.evaluation_unit_id or trace_context.request_id or "").strip()
    task = str(trace_context.task_id or "").strip()
    runtime = str(trace_context.runtime_hash or "").strip()
    if not request_or_unit or not task or trace_context.seed is None or not runtime:
        return None
    return (
        session_dir
        / "runtime_tasks"
        / _slug(task, fallback="task")
        / f"seed_{int(trace_context.seed)}"
        / "runtimes"
        / _slug(runtime, fallback="runtime")
        / "requests"
        / _slug(request_or_unit, fallback="request")
    )


def _slug(value: str, *, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip()).strip("._")
    return (cleaned or fallback)[:96]


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "model_dump"):
        try:
            return _jsonable(value.model_dump())
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        try:
            return _jsonable(vars(value))
        except Exception:
            pass
    return str(value)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _render_block(value: Any, *, preferred: str = "text") -> tuple[str, str]:
    if isinstance(value, str):
        return preferred, value
    return "json", json.dumps(_jsonable(value), indent=2, sort_keys=True)


def _write_text(path: Path, text: str) -> None:
    ensure_directory(path.parent)
    temp_path = path.with_name(f".tmp-{os.getpid()}-{next(_WRITE_COUNTER)}-{stable_hash(path.name)[:8]}")
    try:
        temp_path.write_text(text, encoding="utf-8")
        os.replace(temp_path, path)
    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass


def _pretty_json(value: Any) -> str:
    return json.dumps(_jsonable(value), indent=2, ensure_ascii=False)


def _collapse_blank_lines(lines: Sequence[str]) -> str:
    collapsed: list[str] = []
    previous_blank = False
    for line in lines:
        is_blank = not str(line).strip()
        if is_blank:
            if previous_blank:
                continue
            collapsed.append("")
            previous_blank = True
            continue
        collapsed.append(str(line).rstrip())
        previous_blank = False
    return "\n".join(collapsed).strip()


def _try_parse_json_text(text: str) -> Any | None:
    stripped = str(text or "").strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except Exception:
        return None


def _try_parse_labeled_literal(text: str) -> tuple[str, Any] | None:
    stripped = str(text or "").strip()
    match = re.match(r"^([A-Za-z0-9_./\\ -]+):\s*(\{[\s\S]*|\[[\s\S]*|\([\s\S]*\))$", stripped)
    if not match:
        return None
    label, literal_body = match.groups()
    try:
        value = ast.literal_eval(literal_body)
    except Exception:
        return None
    return label.strip(), value


def _looks_like_patch_text(text: str) -> bool:
    return "SEARCH" in text and "REPLACE" in text and "<" in text


def _wrap_paragraph(text: str, *, initial_indent: str = "", subsequent_indent: str = "") -> str:
    return textwrap.fill(
        text,
        width=100,
        initial_indent=initial_indent,
        subsequent_indent=subsequent_indent,
        break_long_words=False,
        break_on_hyphens=False,
    )


def _format_patch_text(text: str) -> str:
    lines = str(text or "").replace("\r\n", "\n").split("\n")
    formatted: list[str] = []
    for raw_line in lines:
        line = raw_line.rstrip()
        if _PATCH_MARKER_RE.match(line.strip()):
            if formatted and formatted[-1] != "":
                formatted.append("")
            formatted.append(line.strip())
            formatted.append("")
            continue
        formatted.append(line)
    return _collapse_blank_lines(formatted)


def _format_plain_text(text: str) -> str:
    normalized = str(text or "").replace("\r\n", "\n").strip()
    if not normalized:
        return ""
    parsed_json = _try_parse_json_text(normalized)
    if parsed_json is not None:
        return _pretty_json(parsed_json)
    labeled_literal = _try_parse_labeled_literal(normalized)
    if labeled_literal is not None:
        label, literal_value = labeled_literal
        return f"{label}:\n{_pretty_json(literal_value)}"
    if _looks_like_patch_text(normalized):
        return _format_patch_text(normalized)

    lines = normalized.split("\n")
    output: list[str] = []
    paragraph_parts: list[str] = []

    def flush_paragraph() -> None:
        if not paragraph_parts:
            return
        output.append(_wrap_paragraph(" ".join(part.strip() for part in paragraph_parts if part.strip())))
        paragraph_parts.clear()

    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            if output and output[-1] != "":
                output.append("")
            continue
        bullet_match = _BULLET_RE.match(line)
        if bullet_match:
            flush_paragraph()
            indent, body = bullet_match.groups()
            output.append(
                _wrap_paragraph(
                    body.strip(),
                    initial_indent=indent,
                    subsequent_indent=" " * len(indent),
                )
            )
            continue
        if stripped.endswith(":") and len(stripped) <= 80:
            flush_paragraph()
            output.append(stripped)
            continue
        paragraph_parts.append(stripped)

    flush_paragraph()
    return _collapse_blank_lines(output)


def _fenced_block(language: str, body: str) -> str:
    content = str(body or "").rstrip()
    return f"```{language}\n{content}\n```"


def _code_language(name: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix == ".py":
        return "python"
    if suffix == ".json":
        return "json"
    if suffix in {".md", ".txt"}:
        return "text"
    return "text"


def _render_sections(sections: Sequence[tuple[str, str]]) -> str:
    rendered: list[str] = []
    for title, body in sections:
        if not str(body or "").strip():
            continue
        rendered.extend([f"**{title}**", "", body.strip(), ""])
    return "\n".join(rendered).strip()


def _render_planning_payload(payload: Mapping[str, Any], *, include_raw_goal: bool = True) -> str:
    sections: list[tuple[str, str]] = []
    raw_goal = payload.get("raw_goal") or payload.get("goal_spec", {}).get("raw_prompt")
    if include_raw_goal and raw_goal:
        sections.append(("Build Goal", _fenced_block("text", _format_plain_text(str(raw_goal)))))
    if "goal_spec" in payload:
        sections.append(("Goal Spec", _fenced_block("json", _pretty_json(payload["goal_spec"]))))
    if "success_criteria" in payload:
        sections.append(("Success Criteria", _fenced_block("json", _pretty_json(payload["success_criteria"]))))
    if "benchmark_plan" in payload:
        sections.append(("Benchmark Plan", _fenced_block("json", _pretty_json(payload["benchmark_plan"]))))
    return _render_sections(sections)


def _render_patch_payload(payload: Mapping[str, Any]) -> str:
    sections: list[tuple[str, str]] = []
    if payload.get("objective"):
        sections.append(("Mutation Objective", _fenced_block("text", _format_plain_text(str(payload["objective"])))))
    if payload.get("touched_scope") is not None:
        sections.append(("Touched Scope", _fenced_block("json", _pretty_json(payload["touched_scope"]))))
    if payload.get("contracts") is not None:
        sections.append(("Contracts", _fenced_block("json", _pretty_json(payload["contracts"]))))
    if payload.get("high_performing_exemplars") is not None:
        sections.append(("High-Performing Exemplars", _fenced_block("json", _pretty_json(payload["high_performing_exemplars"]))))
    if payload.get("immutable_manifest") is not None:
        sections.append(("Immutable Manifest", _fenced_block("json", _pretty_json(payload["immutable_manifest"]))))
    if payload.get("patch_rules") is not None:
        sections.append(("Patch Rules", _fenced_block("json", _pretty_json(payload["patch_rules"]))))
    if payload.get("predictor_summaries") is not None:
        sections.append(("Predictor Summaries", _fenced_block("json", _pretty_json(payload["predictor_summaries"]))))
    if payload.get("recent_failing_train_traces") is not None:
        sections.append(("Recent Failing Train Traces", _fenced_block("json", _pretty_json(payload["recent_failing_train_traces"]))))
    if payload.get("candidate_answer"):
        sections.append(("Candidate Answer To Repair", _fenced_block("text", _format_plain_text(str(payload["candidate_answer"])))))
    original_prompt = payload.get("original_prompt")
    if original_prompt:
        parsed_original_prompt = _try_parse_json_text(str(original_prompt))
        if isinstance(parsed_original_prompt, Mapping):
            sections.append(("Original Mutation Prompt", _render_patch_payload(parsed_original_prompt)))
        else:
            sections.append(("Original Mutation Prompt", _fenced_block("text", _format_plain_text(str(original_prompt)))))
    mutable_files = payload.get("mutable_files")
    if isinstance(mutable_files, Mapping):
        file_blocks: list[str] = []
        for file_name, source_text in mutable_files.items():
            file_blocks.extend(
                [
                    f"File: `{file_name}`",
                    "",
                    _fenced_block(_code_language(str(file_name)), str(source_text).rstrip()),
                    "",
                ]
            )
        sections.append(("Mutable Files Sent To The Model", "\n".join(file_blocks).strip()))
    return _render_sections(sections)


def _render_tool_spec_payload(payload: Mapping[str, Any], metadata: Mapping[str, Any]) -> str:
    sections: list[tuple[str, str]] = []
    if payload:
        sections.append(("Tool Request", _fenced_block("json", _pretty_json(payload))))
    metadata_payload = metadata.get("payload")
    if metadata_payload is not None:
        sections.append(("Runtime Hint Payload", _fenced_block("json", _pretty_json(metadata_payload))))
    return _render_sections(sections)


def _render_summary_payload(payload_text: str) -> str:
    return _render_sections([("Evidence Sent", _fenced_block("text", _format_plain_text(payload_text)))])


def _render_user_request_payload(payload_text: str) -> str:
    return _render_sections([("Request", _fenced_block("text", _format_plain_text(payload_text)))])


def _render_outgoing_markdown(record: Mapping[str, Any]) -> str:
    purpose = str(record.get("purpose") or "")
    payload_text = str(record.get("input") or "")
    instructions = _format_plain_text(str(record.get("instructions") or ""))
    metadata = record.get("request_metadata") or {}
    parsed_input = _try_parse_json_text(payload_text)
    sections: list[tuple[str, str]] = [
        ("Instructions", _fenced_block("text", instructions or "(none)")),
    ]
    if purpose == "planning" and isinstance(parsed_input, Mapping):
        sections.append(("Request Body", _render_planning_payload(parsed_input)))
    elif purpose in {"patch", "patch_repair"} and isinstance(parsed_input, Mapping):
        sections.append(("Request Body", _render_patch_payload(parsed_input)))
    elif purpose == "tool_spec":
        tool_payload = parsed_input if isinstance(parsed_input, Mapping) else {}
        sections.append(("Request Body", _render_tool_spec_payload(tool_payload, metadata)))
    elif purpose == "summary":
        sections.append(("Request Body", _render_summary_payload(payload_text)))
    elif purpose == "user_request":
        sections.append(("Request Body", _render_user_request_payload(payload_text)))
    elif isinstance(parsed_input, Mapping):
        sections.append(("Request Body", _fenced_block("json", _pretty_json(parsed_input))))
    else:
        sections.append(("Request Body", _fenced_block("text", _format_plain_text(payload_text))))
    return _render_sections(sections)


def _render_incoming_markdown(record: Mapping[str, Any]) -> str:
    purpose = str(record.get("purpose") or "")
    response_text = str(record.get("response_text") or "")
    parsed_response = _try_parse_json_text(response_text)
    if purpose == "planning" and isinstance(parsed_response, Mapping):
        return _render_sections([("Model Response", _render_planning_payload(parsed_response, include_raw_goal=False))])
    if purpose == "tool_spec" and parsed_response is not None:
        return _render_sections([("Model Response", _fenced_block("json", _pretty_json(parsed_response)))])
    if purpose in {"patch", "patch_repair"}:
        return _render_sections([("Model Response", _fenced_block("text", _format_plain_text(response_text)))])
    if parsed_response is not None:
        return _render_sections([("Model Response", _fenced_block("json", _pretty_json(parsed_response)))])
    return _render_sections([("Model Response", _fenced_block("text", _format_plain_text(response_text)))])


def _load_trace_records(session_dir: Path) -> list[dict[str, Any]]:
    calls_dir = session_dir / "calls"
    records: list[dict[str, Any]] = []
    for json_path in sorted(calls_dir.glob("*.json")):
        try:
            record = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        record["_json_path"] = str(json_path)
        records.append(record)
    records.sort(key=lambda item: (int(item.get("call_ordinal", 0) or 0), str(item.get("timestamp_utc", "")), str(item.get("call_id", ""))))
    return records


def _build_api_flow_call(record: Mapping[str, Any], *, header_level: str, ordinal: int | None = None) -> str:
    title_prefix = f"{ordinal:02d}. " if ordinal is not None else ""
    heading = f"{header_level} {title_prefix}{record.get('purpose', 'unspecified')} | {record.get('model_name', '')} | {record.get('status', 'unknown')}"
    json_path = str(record.get("_json_path") or "")
    lines = [
        heading,
        "",
        f"- Timestamp (UTC): {record.get('timestamp_utc', '')}",
        f"- Method: {record.get('method_name', '')}",
        f"- Tokens: {record.get('total_tokens', 0)} total ({record.get('input_tokens', 0)} in / {record.get('output_tokens', 0)} out)",
        f"- Latency: {float(record.get('latency_s') or 0.0):.3f}s",
    ]
    if json_path:
        lines.append(f"- Source JSON: {json_path}")
    lines.extend(
        [
            "",
            "### Outgoing",
            "",
            _render_outgoing_markdown(record).strip(),
            "",
            "### Incoming",
            "",
            _render_incoming_markdown(record).strip(),
        ]
    )
    error_text = _format_plain_text(str(record.get("error") or ""))
    if error_text:
        lines.extend(["", "### Error", "", _fenced_block("text", error_text)])
    return "\n".join(lines).strip() + "\n"


def render_trace_subset(
    session_dir: Path | str,
    *,
    output_dir: Path | str,
    start: int = 0,
    limit: int | None = None,
) -> dict[str, str]:
    session_path = Path(session_dir)
    destination = ensure_directory(Path(output_dir))
    records = _load_trace_records(session_path)
    if limit is None:
        selected = records[start:]
    else:
        selected = records[start : start + max(0, limit)]
    call_dir = ensure_directory(destination / "calls")

    call_rows: list[tuple[int, dict[str, Any], str]] = []
    for index, record in enumerate(selected, start=1):
        call_file_name = "__".join(
            [
                f"{index:02d}",
                _slug(str(record.get("purpose") or ""), fallback="purpose"),
                _slug(str(record.get("method_name") or ""), fallback="method"),
                _slug(str(record.get("model_name") or ""), fallback="model"),
            ]
        ) + ".md"
        call_path = call_dir / call_file_name
        _write_text(call_path, _build_api_flow_call(record, header_level="#", ordinal=index))
        call_rows.append((index, record, call_file_name))

    transcript_lines = [
        "# OpenAI API Trace Log",
        "",
        "Readable request/response render with a strict outgoing and incoming split.",
        "",
    ]
    for index, record, _ in call_rows:
        transcript_lines.append(_build_api_flow_call(record, header_level="##", ordinal=index).rstrip())
        transcript_lines.append("")
    transcript_path = destination / "TRANSCRIPT__OUTGOING_INCOMING.md"
    _write_text(transcript_path, "\n".join(transcript_lines).rstrip() + "\n")

    index_lines = [
        "# Reformatted Trace Index",
        "",
        "| # | Purpose | Model | Status | Tokens | Call File |",
        "| ---: | --- | --- | --- | ---: | --- |",
    ]
    for index, record, call_file_name in call_rows:
        index_lines.append(
            "| "
            + f"{index} | {record.get('purpose', '')} | {record.get('model_name', '')} | {record.get('status', '')} | "
            + f"{record.get('total_tokens', 0)} | [call](calls/{call_file_name}) |"
        )
    index_path = destination / "INDEX.md"
    _write_text(index_path, "\n".join(index_lines) + "\n")

    readme_lines = [
        "# Reformatted OpenAI Trace Subset",
        "",
        f"- Source session: {session_path}",
        f"- Selected calls: {len(selected)}",
        f"- Start offset: {start}",
        f"- Limit: {limit if limit is not None else 'all remaining'}",
        f"- Transcript: {transcript_path}",
        f"- Index: {index_path}",
        "",
        "Call groups in this subset:",
        "- `smoke_test`: connectivity sanity check.",
        "- `planning`: factory-side goal and benchmark refinement.",
        "- `patch`: factory-side mutation request sent to the model.",
        "- `tool_spec`: runtime-side tool synthesis request.",
        "- `summary`: runtime-side memory summarization request.",
        "- `user_request`: runtime-side direct answer request.",
        "",
        "Formatting rules used here:",
        "- `Outgoing` means everything Agintor sent to OpenAI for that call.",
        "- `Incoming` means the model's returned text for that call.",
        "- JSON is pretty-printed.",
        "- Python source embedded inside JSON is unescaped into real code blocks.",
        "- Patch outputs are spaced into readable SEARCH/REPLACE blocks.",
        "- Plain text is reflowed into narrower paragraphs and readable bullet lists.",
        "",
    ]
    readme_path = destination / "README.md"
    _write_text(readme_path, "\n".join(readme_lines))

    return {
        "session_dir": str(session_path),
        "output_dir": str(destination),
        "transcript_path": str(transcript_path),
        "index_path": str(index_path),
        "readme_path": str(readme_path),
    }


def _build_markdown(record: Mapping[str, Any]) -> str:
    instruction_lang, instructions = _render_block(record.get("instructions", ""), preferred="text")
    input_lang, input_body = _render_block(record.get("input", ""), preferred="text")
    output_lang, output_body = _render_block(record.get("response_text", ""), preferred="text")
    metadata_lang, metadata_body = _render_block(record.get("request_metadata", {}), preferred="json")
    payload_lang, payload_body = _render_block(record.get("request_payload", {}), preferred="json")
    response_lang, response_body = _render_block(record.get("response_raw", {}), preferred="json")
    lines = [
        f"# OpenAI Call {record['call_id']}",
        "",
        f"- Timestamp (UTC): {record['timestamp_utc']}",
        f"- Provider: {record['provider']}",
        f"- Method: {record['method_name']}",
        f"- Purpose: {record['purpose']}",
        f"- Model class: {record['model_class']}",
        f"- Resolved model: {record['model_name']}",
        f"- Reasoning effort: {record.get('reasoning_effort') or 'none'}",
        f"- Status: {record.get('status') or 'unknown'}",
        f"- Response id: {record.get('response_id') or ''}",
        f"- Input tokens: {record.get('input_tokens') or 0}",
        f"- Output tokens: {record.get('output_tokens') or 0}",
        f"- Total tokens: {record.get('total_tokens') or 0}",
        f"- Latency seconds: {record.get('latency_s') or 0.0}",
        "",
        "## Chat Render",
        "",
        "### System / Instructions",
        f"```{instruction_lang}",
        instructions,
        "```",
        "",
        "### User / Input",
        f"```{input_lang}",
        input_body,
        "```",
        "",
        "### Assistant / Output",
        f"```{output_lang}",
        output_body,
        "```",
        "",
        "## Request Metadata",
        f"```{metadata_lang}",
        metadata_body,
        "```",
        "",
        "## Request Payload Sent To OpenAI",
        f"```{payload_lang}",
        payload_body,
        "```",
        "",
        "## Raw Response",
        f"```{response_lang}",
        response_body,
        "```",
    ]
    error_text = str(record.get("error") or "").strip()
    if error_text:
        lines.extend(
            [
                "",
                "## Error",
                "```text",
                error_text,
                "```",
            ]
        )
    return "\n".join(lines) + "\n"


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
    for name in ("groups", "builds", "solves", "runtime_tasks"):
        path = session_dir / name
        if path.exists():
            shutil.rmtree(path)


def _write_grouped_views(session_dir: Path, records: Sequence[Mapping[str, Any]]) -> TraceMaterializationState:
    _reset_derived_group_dirs(session_dir)
    ordered_records = sorted(records, key=_trace_record_sort_key)
    _write_trace_view(session_dir, f"OpenAI Trace Session {session_dir.name}", ordered_records)

    build_groups: dict[str, list[Mapping[str, Any]]] = {}
    solve_groups: dict[str, list[Mapping[str, Any]]] = {}
    runtime_groups: dict[str, tuple[OpenAITraceContext, list[Mapping[str, Any]]]] = {}
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
        runtime_task_key = _runtime_task_key(trace_context)
        if runtime_task_key:
            runtime_groups.setdefault(runtime_task_key, (trace_context, []))[1].append(record)

    for build_id, rows in sorted(build_groups.items()):
        _write_trace_view(session_dir / "builds" / _slug(build_id, fallback="build"), f"Build {build_id}", rows)
    for request_id, rows in sorted(solve_groups.items()):
        _write_trace_view(session_dir / "solves" / _slug(request_id, fallback="request"), f"Solve {request_id}", rows)

    runtime_task_keys: list[str] = []
    grouped_call_count = 0
    for runtime_task_key, (trace_context, rows) in sorted(runtime_groups.items()):
        group_dir = _runtime_task_view_dir(session_dir, trace_context)
        if group_dir is None:
            continue
        runtime_task_keys.append(runtime_task_key)
        grouped_call_count += len(rows)
        _write_trace_view(group_dir, f"Runtime Task {runtime_task_key}", rows)

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
        materialized_runtime_task_keys=runtime_task_keys,
        pending_build_ids=[],
        pending_solve_request_ids=[],
        pending_runtime_task_keys=[],
        errors=errors,
        updated_at=updated_at,
        call_count=len(ordered_records),
        grouped_call_count=grouped_call_count,
        runtime_task_keys=runtime_task_keys,
        rebuilt_at=datetime.fromtimestamp(updated_at, timezone.utc).isoformat(),
    )
    _write_text(session_dir / "materialization_state.json", json.dumps((state).model_dump(), indent=2, sort_keys=True))
    return state


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
        runtime_task_key = _runtime_task_key(trace_context)
        request_metadata_payload = _jsonable(request_metadata or {})
        if isinstance(request_metadata_payload, dict):
            request_metadata_payload["trace_context"] = (trace_context).model_dump()
        raw_response = _jsonable(response)
        with _LOCK:
            ordinal = next(_CALL_COUNTER)
            stem = "__".join(
                [
                    timestamp,
                    f"pid{os.getpid()}",
                    f"call{ordinal:04d}",
                    _slug(purpose, fallback="purpose"),
                    _slug(method_name, fallback="method"),
                    _slug(model_name, fallback="model"),
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
            if runtime_task_key:
                record["runtime_task_key"] = runtime_task_key
            calls_dir = _calls_dir(session_id)
            json_path = calls_dir / f"{stem}.json"
            md_path = calls_dir / f"{stem}.md"
            _write_text(json_path, json.dumps(record, indent=2, sort_keys=True))
            _write_text(md_path, _build_markdown(record))
            session_dir = _session_dir(session_id)
            _write_index(session_dir)
            _write_grouped_views(session_dir, _load_trace_records(session_dir))
            return stem
    except Exception:
        return None


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


if __name__ == "__main__":
    raise SystemExit(_main())
