from __future__ import annotations

from typing import Any

from ...contracts import ValidatorResult, ValidatorSpec
from ..validator_registry import ValidatorFamily


def _run(spec: ValidatorSpec, payload: dict[str, Any]) -> ValidatorResult:
    trace = payload.get("trace") or []
    required_events = set(spec.inputs.get("required_events", []))
    forbidden_events = set(spec.inputs.get("forbidden_events", []))
    observed_events = {str(row.get("event")) for row in trace if isinstance(row, dict)}
    missing = sorted(required_events - observed_events)
    forbidden_seen = sorted(forbidden_events & observed_events)
    passed = not missing and not forbidden_seen
    return ValidatorResult(
        validator_id=spec.validator_id,
        family_id=spec.family_id,
        claim_ids=list(spec.claim_ids),
        status="pass" if passed else "fail",
        authority_used="A3",
        health_status={"trace_present": bool(trace), "trajectory_checks": True},
        observations={"missing_events": missing, "forbidden_events_seen": forbidden_seen, "event_count": len(trace)},
    )


def _applicability(context: dict[str, Any]) -> float:
    return 0.7 if context.get("trace_available", True) else 0.2


def family() -> ValidatorFamily:
    return ValidatorFamily(
        family_id="trace_state",
        description="Trajectory/graph-state validator for required and forbidden nodes, tools, budget, and side-effect receipts.",
        authority_ceiling="A3",
        default_visibility="sealed",
        leakage_risk="low",
        default_failure_action="abstain",
        input_contract={"requires": ["trace"]},
        output_schema={"type": "object", "properties": {"missing_events": {"type": "array"}}},
        run=_run,
        applicability=_applicability,
    )
