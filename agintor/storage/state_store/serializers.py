from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from ...utils import ensure_directory, now_ts, stable_hash

from .layout import StateStoreError

def _request_envelope_payload(envelope: Mapping[str, Any]) -> dict[str, Any]:
    payload = _mapping(envelope.get("payload"))
    return payload if payload else _mapping(envelope)


def _trace_context_from_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    return _mapping(payload.get("trace_context"))


def _task_payload_from_execution_payload(
    payload: Mapping[str, Any],
    fallback_task_payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    task_payload = _mapping(payload.get("task"))
    if task_payload:
        return task_payload
    fallback = _mapping(fallback_task_payload)
    if fallback:
        return fallback
    if payload.get("task_id") is not None and payload.get("prompt") is not None:
        return _mapping(payload)
    return {}


def _request_bundle_execution_rows(
    envelope: Mapping[str, Any],
    *,
    fallback_task_payload: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    member_payloads = [_mapping(item) for item in _sequence(envelope.get("member_invocations")) if isinstance(item, Mapping)]
    payloads = member_payloads or [_request_envelope_payload(envelope)]
    rows: list[dict[str, Any]] = []
    for payload in payloads:
        trace_context = _trace_context_from_payload(payload)
        task_payload = _task_payload_from_execution_payload(payload, fallback_task_payload)
        episode_kind = _first_present(
            payload.get("episode_kind"),
            trace_context.get("episode_kind"),
            _episode_kind_from_task(task_payload, default=None),
        )
        episode_step_index = _first_present(
            payload.get("episode_step_index"),
            trace_context.get("episode_step_index"),
            _episode_step_index_from_task(task_payload) if episode_kind == "transfer_episode" else None,
        )
        if episode_kind != "transfer_episode":
            episode_kind = None
            episode_step_index = None
        rows.append(
            {
                "payload": payload,
                "task_payload": task_payload,
                "trace_context": trace_context,
                "request_id": _first_present(payload.get("request_id"), trace_context.get("request_id")),
                "evaluation_unit_id": _first_present(payload.get("evaluation_unit_id"), trace_context.get("evaluation_unit_id")),
                "seed": _first_present(payload.get("seed"), trace_context.get("seed")),
                "episode_kind": episode_kind,
                "episode_step_index": episode_step_index,
            }
        )
    return rows


def _episode_kind_from_task(task_payload: Mapping[str, Any], *, default: str | None = None) -> str | None:
    if _bool(task_payload.get("transfer_scored")) and _text(task_payload.get("episode_id")):
        return "transfer_episode"
    return default


def _episode_step_index_from_task(task_payload: Mapping[str, Any]) -> int | None:
    if _bool(task_payload.get("transfer_scored")) and _text(task_payload.get("episode_id")):
        return _int(task_payload.get("episode_order"))
    return None


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _payload(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if hasattr(value, "model_dump") or hasattr(value, "dict"):
        return _jsonable((value).model_dump())
    if hasattr(value, "__dict__"):
        return _jsonable(vars(value))
    return {}


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "model_dump") or hasattr(value, "dict"):
        return _jsonable((value).model_dump())
    return str(value)


def _run_root_from_payload(payload: Mapping[str, Any]) -> Path:
    run_root = str(payload.get("run_root") or "").strip()
    if not run_root:
        raise StateStoreError("state store indexing requires run_root in canonical payload")
    return Path(run_root).resolve()


def _canonical(scope: str, ref: str, record_id: str) -> dict[str, str]:
    return {
        "canonical_scope": scope,
        "canonical_ref": ref,
        "canonical_record_id": record_id,
    }


def _canonical_ref_from_reference(
    reference: Mapping[str, Any] | None,
    *,
    fallback: str,
    run_root: Path,
) -> str:
    ref = str((reference or {}).get("ref") or (reference or {}).get("checkpoint_ref") or "").strip()
    return _relative_ref(run_root, ref) if ref else fallback


def _relative_ref(run_root: Path, value: str | Path | None) -> str:
    if value is None:
        return ""
    path = Path(str(value))
    try:
        return str(path.resolve().relative_to(run_root.resolve())).replace("\\", "/")
    except Exception:
        return str(value).replace("\\", "/")


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


def _text(value: Any) -> str:
    return str(value or "").strip()


def _none_or_text(value: Any) -> str | None:
    text = _text(value)
    return text or None


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return _int(value)


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return _float(value)


def _bool(value: Any) -> int:
    return 1 if bool(value) else 0


def _json_dumps(value: Any) -> str:
    return json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":"))


def _write_json(path: Path, payload: Any) -> None:
    ensure_directory(path.parent)
    path.write_text(json.dumps(_jsonable(payload), indent=2, sort_keys=True), encoding="utf-8")


def _write_jsonl(path: Path, rows: Sequence[Any]) -> None:
    ensure_directory(path.parent)
    path.write_text("\n".join(_json_dumps(row) for row in rows) + ("\n" if rows else ""), encoding="utf-8")


def _load_optional_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _slug(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(value or "").strip())
    return (cleaned.strip("._") or "item")[:96]
