from __future__ import annotations

from typing import Any

from ...contracts import BenchmarkTask


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


def _trace_summary(trace: list[dict[str, Any]]) -> dict[str, Any]:
    events = [row.get("event", "") for row in trace]
    return {
        "event_count": len(trace),
        "events_tail": events[-6:],
        "has_tool_fault": any(event == "tool_fault" for event in events),
        "has_compaction": any(event == "compaction" for event in events),
        "has_stop": any(event == "stop" for event in events),
    }


def _trace_has_events(trace: list[dict[str, Any]], expected: Any) -> bool:
    wanted = expected if isinstance(expected, list) else [expected]
    observed = {row.get("event") for row in trace}
    return all(event in observed for event in wanted)


def _trace_event_count(trace: list[dict[str, Any]], event_name: str) -> int:
    return sum(1 for row in trace if row.get("event") == event_name)


def verify_task_with_evidence(task: BenchmarkTask, artifact: Any, trace: list[dict[str, Any]]) -> tuple[float, dict[str, Any]]:
    evidence = {
        "verifier_type": task.verifier_type,
        "expected": task.expected,
        "artifact": artifact,
        "trace": _trace_summary(trace),
    }
    if task.verifier_type in {"none", "best_effort"}:
        evidence["matched"] = None
        evidence["reason"] = "no exact verifier available"
        return 0.0, evidence
    if task.verifier_type == "trace_event":
        matched = _trace_has_events(trace, task.expected)
        evidence["matched"] = matched
        evidence["observed"] = [row.get("event", "") for row in trace]
        return (1.0 if matched else 0.0), evidence
    if task.verifier_type == "trace_event_count":
        event_name = str(task.expected.get("event", "")) if isinstance(task.expected, dict) else str(task.expected)
        minimum = int(task.expected.get("min", 1)) if isinstance(task.expected, dict) else 1
        observed = _trace_event_count(trace, event_name)
        matched = observed >= minimum
        evidence["matched"] = matched
        evidence["observed"] = observed
        return (1.0 if matched else 0.0), evidence
    if task.verifier_type == "json_exact":
        matched = _json_equal(artifact, task.expected)
        evidence["matched"] = matched
        return (1.0 if matched else 0.0), evidence
    if task.verifier_type == "json_numeric":
        matched = _json_numeric_equal(artifact, task.expected)
        evidence["matched"] = matched
        return (1.0 if matched else 0.0), evidence
    if task.verifier_type == "string_exact":
        matched = str(artifact) == str(task.expected)
        evidence["matched"] = matched
        return (1.0 if matched else 0.0), evidence
    if task.verifier_type == "number_exact":
        try:
            matched = float(artifact) == float(task.expected)
        except Exception:
            matched = False
        evidence["matched"] = matched
        return (1.0 if matched else 0.0), evidence
    raise ValueError(f"unknown verifier_type {task.verifier_type}")


def verify_task(task: BenchmarkTask, artifact: Any, trace: list[dict[str, Any]]) -> float:
    score, _ = verify_task_with_evidence(task, artifact, trace)
    return score


def run_checker(task: BenchmarkTask, artifact: Any, trace: list[dict[str, Any]], checker: str) -> dict[str, Any]:
    if checker == "local":
        passed = artifact not in (None, "", {}, [])
        return {
            "checker": checker,
            "passed": passed,
            "reason": "non-empty artifact" if passed else "empty artifact",
        }
    if checker == "subtree":
        child_events = _trace_event_count(trace, "child_complete")
        return {
            "checker": checker,
            "passed": child_events > 0,
            "reason": "child outputs reached parent merge" if child_events > 0 else "no child outputs recorded",
            "observed": child_events,
        }
    if checker == "repo":
        expected = task.expected
        if isinstance(expected, dict):
            passed = isinstance(artifact, dict) and set(artifact).issubset(set(expected))
        elif isinstance(expected, list):
            passed = isinstance(artifact, list)
        elif isinstance(expected, (int, float)):
            passed = isinstance(artifact, (int, float))
        else:
            passed = isinstance(artifact, type(expected)) or str(artifact) == str(expected)
        return {
            "checker": checker,
            "passed": passed,
            "reason": "artifact shape is verifier-compatible" if passed else "artifact shape mismatch",
            "trace_summary": _trace_summary(trace),
        }
    if checker == "benchmark":
        score, evidence = verify_task_with_evidence(task, artifact, trace)
        return {
            "checker": checker,
            "passed": score >= 1.0,
            "score": score,
            "evidence": evidence,
        }
    raise ValueError(f"unknown checker {checker}")
