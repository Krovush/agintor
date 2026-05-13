from __future__ import annotations

from typing import Any

from ...contracts import ValidatorResult, ValidatorSpec
from ..validator_registry import ValidatorFamily


def _run(spec: ValidatorSpec, payload: dict[str, Any]) -> ValidatorResult:
    final_state = payload.get("final_state") or {}
    expected_state = payload.get("private_expected_state") or spec.inputs.get("expected_state") or {}
    duplicate_side_effects = int(payload.get("duplicate_side_effects", 0) or 0)
    policy_violations = payload.get("policy_violations") or []
    state_ok = not expected_state or final_state == expected_state
    passed = state_ok and duplicate_side_effects == 0 and not policy_violations
    return ValidatorResult(
        validator_id=spec.validator_id,
        family_id=spec.family_id,
        claim_ids=list(spec.claim_ids),
        status="pass" if passed else "fail",
        authority_used="A4" if expected_state else "A3",
        health_status={"state_fixture_loaded": bool(expected_state), "side_effect_scan": True},
        observations={"state_ok": state_ok, "duplicate_side_effects": duplicate_side_effects, "policy_violations": list(policy_violations)},
    )


def _applicability(context: dict[str, Any]) -> float:
    goal = str(context.get("goal_text", "")).lower()
    return 0.9 if any(word in goal for word in ["service", "api", "state", "database", "workflow", "customer"]) else 0.1


def family() -> ValidatorFamily:
    return ValidatorFamily(
        family_id="stateful_service",
        description="tau-bench-style service simulation validator with API state, final-state diffs, and duplicate side-effect checks.",
        authority_ceiling="A4",
        default_visibility="sealed",
        leakage_risk="high",
        default_failure_action="reject",
        input_contract={"requires": ["final_state"], "sealed_optional": ["expected_state"]},
        output_schema={"type": "object", "properties": {"state_ok": {"type": "boolean"}}},
        run=_run,
        applicability=_applicability,
    )
