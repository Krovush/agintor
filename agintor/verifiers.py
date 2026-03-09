from __future__ import annotations

from typing import Any, Mapping

from .schemas import BenchmarkTask



def _json_equal(actual: Any, expected: Any) -> bool:
    return actual == expected



def _json_numeric_equal(actual: Any, expected: Any, tol: float = 1e-9) -> bool:
    if isinstance(expected, dict):
        if not isinstance(actual, dict) or set(actual) != set(expected):
            return False
        return all(_json_numeric_equal(actual[key], expected[key], tol=tol) for key in expected)
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            return False
        return all(_json_numeric_equal(a, b, tol=tol) for a, b in zip(actual, expected))
    if isinstance(expected, (int, float)):
        try:
            return abs(float(actual) - float(expected)) <= tol
        except Exception:
            return False
    return actual == expected



def verify_task(task: BenchmarkTask, artifact: Any, trace: list[dict[str, Any]]) -> float:
    if task.verifier_type == "json_exact":
        return 1.0 if _json_equal(artifact, task.expected) else 0.0
    if task.verifier_type == "json_numeric":
        return 1.0 if _json_numeric_equal(artifact, task.expected) else 0.0
    if task.verifier_type == "string_exact":
        return 1.0 if str(artifact) == str(task.expected) else 0.0
    if task.verifier_type == "number_exact":
        try:
            return 1.0 if float(artifact) == float(task.expected) else 0.0
        except Exception:
            return 0.0
    raise ValueError(f"unknown verifier_type {task.verifier_type}")
