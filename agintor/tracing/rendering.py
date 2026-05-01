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

from .layout import (
    _slug,
    _write_text,
)

_PATCH_MARKER_RE = re.compile(r"^(?:<{3,7}\s*(?:SEARCH|REPLACE)?|={7}|>{7}\s*REPLACE)\s*$")


_BULLET_RE = re.compile(r"^(\s*(?:[-*+]|\d+\.)\s+)(.*)$")


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
