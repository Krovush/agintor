from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

from .benchmarks import BenchmarkTask
from .protocol import RunResult, RuntimeSolveResponse


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
    if task.verifier_type == "oracle_package":
        evidence["matched"] = None
        evidence["reason"] = "oracle_package verifier requires evaluator-side sealed package dispatch"
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


def private_verifier_task(task: BenchmarkTask) -> BenchmarkTask | None:
    if getattr(task, "private_expected", None) is None:
        return None
    return task.model_copy(
        update={"expected": task.private_expected, "private_expected": None},
        deep=True,
    )


def _trace_rows_from_ref(trace_ref: str | None) -> list[dict[str, Any]]:
    if not trace_ref:
        return []
    inline_rows = RunResult.decode_trace_ref(str(trace_ref))
    if inline_rows:
        return inline_rows
    try:
        payload = json.loads(Path(str(trace_ref)).read_text(encoding="utf-8"))
    except Exception:
        return []
    return payload if isinstance(payload, list) else []


def _solve_result_is_failed(response: RuntimeSolveResponse) -> bool:
    result = response.solve_result
    status = str(result.status or "").lower()
    verification_status = str(result.verification_status or "").lower()
    lifecycle_state = str(result.run_lifecycle_state or "").lower()
    faults = dict(result.faults or {})
    if bool(faults.get("hard_invalid", False)):
        return True
    if status in {"failed", "cancelled", "controlled_failure"}:
        return True
    if verification_status == "failed":
        return True
    return lifecycle_state in {"failed", "cancelled", "pruned"}


_HOST_SEALED_CHECKERS = {"sealed_private", "sealed_private_evidence"}


def _runtime_supplied_checks(checks: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        dict(check)
        for check in checks
        if str(check.get("checker", "")) not in _HOST_SEALED_CHECKERS
    ]


def rescore_private_solve_response(response: RuntimeSolveResponse, task: BenchmarkTask) -> RuntimeSolveResponse:
    verifier_task = private_verifier_task(task)
    if verifier_task is None or _solve_result_is_failed(response):
        return response
    trace = _trace_rows_from_ref(response.solve_result.trace_ref)
    try:
        score, evidence = verify_task_with_evidence(verifier_task, response.solve_result.artifact, trace)
    except Exception as exc:
        score = 0.0
        evidence = {"reason": f"private verifier failed: {exc}", "verifier_type": verifier_task.verifier_type}
    check = {
        "checker": "sealed_private",
        "passed": score >= 1.0,
        "verifier_type": verifier_task.verifier_type,
        "authority": "host",
    }
    checks = [*_runtime_supplied_checks(response.solve_result.checks), check]
    if score >= 1.0:
        solve_result = response.solve_result.model_copy(
            update={
                "status": "verified",
                "verification_status": "verified",
                "summary": "The runtime produced a host-verified artifact.",
                "verified": True,
                "best_effort": False,
                "checks": checks,
            }
        )
    else:
        solve_result = response.solve_result.model_copy(
            update={
                "status": "unverified",
                "verification_status": "exact_verifier_failed",
                "summary": "The runtime produced an artifact, but the sealed host verifier rejected it.",
                "verified": False,
                "best_effort": False,
                "checks": [
                    *checks,
                    {
                        "checker": "sealed_private_evidence",
                        "passed": False,
                        "reason": str(evidence.get("reason", "")),
                    },
                ],
            }
        )
    return response.model_copy(update={"solve_result": solve_result})


def rescore_private_run_results(runs: Sequence[RunResult], tasks: Sequence[BenchmarkTask]) -> list[RunResult]:
    private_tasks = {
        task.task_id: task
        for task in tasks
        if private_verifier_task(task) is not None
    }
    if not private_tasks:
        return list(runs)
    rescored: list[RunResult] = []
    for run in runs:
        task = private_tasks.get(run.task_id)
        verifier_task = private_verifier_task(task) if task is not None else None
        if verifier_task is None or run.hard_invalid:
            rescored.append(run)
            continue
        try:
            score, _ = verify_task_with_evidence(verifier_task, run.artifact, run.trace_rows())
        except Exception as exc:
            rescored.append(
                run.model_copy(
                    update={
                        "verifier_score": 0.0,
                        "hard_invalid": True,
                        "invalid_reason": f"private verifier failed: {exc}",
                        "failure_kind": run.failure_kind or "private_verifier_failed",
                    }
                )
            )
            continue
        rescored.append(run.model_copy(update={"verifier_score": score}))
    return rescored


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
