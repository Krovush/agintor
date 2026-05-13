from __future__ import annotations

from typing import Any, Mapping

from ..contracts import OraclePackage

_PRIVATE_KEYS = {
    "sealed_inputs",
    "sealed_fixture_refs",
    "private_expected",
    "private_answer",
    "private_answer_ref",
    "hidden_tests",
    "promotion_thresholds",
    "private_rubric",
}
_PRIVATE_PREFIXES = ("private_", "sealed_", "hidden_")


def _strip_private(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _strip_private(value.model_dump(mode="json", exclude_none=True))
    if isinstance(value, Mapping):
        stripped: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            normalized = key_text.lower()
            if normalized in _PRIVATE_KEYS or any(normalized.startswith(prefix) for prefix in _PRIVATE_PREFIXES):
                continue
            stripped[key_text] = _strip_private(item)
        return stripped
    if isinstance(value, list):
        return [_strip_private(item) for item in value]
    return value


def _private_paths(value: Any, *, path: str = "") -> list[str]:
    paths: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}" if path else key_text
            normalized = key_text.lower()
            if normalized in _PRIVATE_KEYS or any(normalized.startswith(prefix) for prefix in _PRIVATE_PREFIXES):
                paths.append(child_path)
            paths.extend(_private_paths(item, path=child_path))
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            paths.extend(_private_paths(item, path=f"{path}[{idx}]"))
    return paths


def public_oracle_projection(package: OraclePackage) -> dict[str, Any]:
    payload = package.model_dump(mode="json", exclude_none=True)
    for validator in payload.get("validator_specs", []):
        if validator.get("visibility") in {"private", "sealed"}:
            validator["inputs"] = {}
            validator["outputs_schema"] = {}
            validator["health_tests"] = []
    payload["task_sets"] = [
        {
            **task_set,
            "tasks": [
                {
                    key: _strip_private(value)
                    for key, value in task.items()
                    if key not in {"sealed_inputs", "sealed_fixture_refs"}
                }
                for task in task_set.get("tasks", [])
            ],
        }
        for task_set in payload.get("task_sets", [])
    ]
    payload["fixture_bundle_refs"] = [
        ref for ref in payload.get("fixture_bundle_refs", []) if ref.get("visibility") == "public"
    ]
    return _strip_private(payload)


def sealed_oracle_projection(package: OraclePackage) -> dict[str, Any]:
    return package.model_dump(mode="json", exclude_none=True)


def assert_no_private_oracle_fields(value: Any) -> None:
    paths = _private_paths(value)
    if paths:
        raise ValueError(f"private oracle fields leaked into public view: {paths}")


def public_task_views(package: OraclePackage, partition: str = "train") -> list[dict[str, Any]]:
    views: list[dict[str, Any]] = []
    public = public_oracle_projection(package)
    for task_set in public.get("task_sets", []):
        if str(task_set.get("partition", "")) != partition:
            continue
        views.extend(list(task_set.get("tasks", [])))
    return views


__all__ = [
    "assert_no_private_oracle_fields",
    "public_oracle_projection",
    "public_task_views",
    "sealed_oracle_projection",
]
