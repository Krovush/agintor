from __future__ import annotations

from typing import Any

from ...contracts import ValidatorResult, ValidatorSpec
from ..validator_registry import ValidatorFamily


def _run(spec: ValidatorSpec, payload: dict[str, Any]) -> ValidatorResult:
    raw_trace = payload.get("trace")
    trace = raw_trace if isinstance(raw_trace, list) else []
    required_events = set(spec.inputs.get("required_events", []))
    forbidden_events = set(spec.inputs.get("forbidden_events", []))
    if not required_events and not forbidden_events:
        return ValidatorResult(
            validator_id=spec.validator_id,
            family_id=spec.family_id,
            claim_ids=list(spec.claim_ids),
            status="abstain",
            authority_used="A0",
            health_status={"trajectory_checks": False},
            observations={"reason": "missing_trace_obligations", "event_count": len(trace)},
        )
    observed_events = {str(row.get("event")) for row in trace if isinstance(row, dict) and row.get("event") is not None}
    missing = sorted(required_events - observed_events)
    forbidden_seen = sorted(forbidden_events & observed_events)
    trace_present = bool(observed_events)
    passed = trace_present and not missing and not forbidden_seen
    return ValidatorResult(
        validator_id=spec.validator_id,
        family_id=spec.family_id,
        claim_ids=list(spec.claim_ids),
        status="pass" if passed else "fail",
        authority_used="A3" if trace_present else "A0",
        health_status={"trace_present": trace_present, "trajectory_checks": True},
        observations={
            "missing_events": missing,
            "forbidden_events_seen": forbidden_seen,
            "event_count": len(observed_events),
            "reason": "" if trace_present else "missing_trace",
        },
    )


def _applicability(context: dict[str, Any]) -> float:
    return 0.7 if context.get("trace_available", True) else 0.0


def family() -> ValidatorFamily:
    return ValidatorFamily(
        family_id="trace_state",
        description="Trajectory/graph-state validator for required and forbidden nodes, tools, budget, and side-effect receipts.",
        authority_ceiling="A3",
        default_visibility="sealed",
        leakage_risk="low",
        default_failure_action="abstain",
        input_contract={"requires": ["trace"], "requires_any": ["required_events", "forbidden_events"]},
        output_schema={"type": "object", "properties": {"missing_events": {"type": "array"}}},
        run=_run,
        applicability=_applicability,
    )
