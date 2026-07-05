from __future__ import annotations

from typing import Any

from ...contracts import ValidatorResult, ValidatorSpec
from ..validator_registry import ValidatorFamily


def _run(spec: ValidatorSpec, payload: dict[str, Any]) -> ValidatorResult:
    expected = payload.get("private_expected", payload.get("expected"))
    observed = payload.get("artifact", payload.get("observed"))
    passed = observed == expected
    return ValidatorResult(
        validator_id=spec.validator_id,
        family_id=spec.family_id,
        claim_ids=list(spec.claim_ids),
        status="pass" if passed else "fail",
        authority_used="A4",
        health_status={"positive_control": True, "negative_control": True},
        observations={"matched": passed, "expected_present": expected is not None},
    )


def _applicability(context: dict[str, Any]) -> float:
    return 0.9 if context.get("private_expected_available") else 0.1


def family() -> ValidatorFamily:
    return ValidatorFamily(
        family_id="exact_private_answer",
        description="Compares runtime output to sealed/private expected values. Use as a high-authority canary, not as the whole oracle.",
        authority_ceiling="A4",
        default_visibility="sealed",
        leakage_risk="high",
        default_failure_action="reject",
        input_contract={"requires": ["artifact", "private_expected"]},
        output_schema={"type": "object", "properties": {"matched": {"type": "boolean"}}},
        run=_run,
        applicability=_applicability,
    )
