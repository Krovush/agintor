from __future__ import annotations

from typing import Any

from ...contracts import ValidatorResult, ValidatorSpec
from ..validator_registry import ValidatorFamily


def _run(spec: ValidatorSpec, payload: dict[str, Any]) -> ValidatorResult:
    checks = payload.get("consent_checks") or []
    receipts = payload.get("side_effect_receipts") or []
    granted = {str(check.get("check_id")): check for check in checks if isinstance(check, dict) and check.get("status") == "granted"}
    launched = [r for r in receipts if isinstance(r, dict) and r.get("status") in {"launched", "completed", "reconciled"}]
    missing = [r.get("side_effect_id") for r in launched if not r.get("consent_check_id") or str(r.get("consent_check_id")) not in granted]
    passed = not missing
    return ValidatorResult(
        validator_id=spec.validator_id,
        family_id=spec.family_id,
        claim_ids=list(spec.claim_ids),
        status="pass" if passed else "fail",
        authority_used="A4",
        health_status={"consent_checks_present": bool(checks), "side_effect_receipts_present": bool(receipts)},
        observations={"launched_side_effects": len(launched), "missing_or_invalid_consent": missing},
    )


def _applicability(context: dict[str, Any]) -> float:
    goal = str(context.get("goal_text", "")).lower()
    return 0.9 if any(word in goal for word in ["consent", "authorization", "permission", "side effect"]) else 0.2


def family() -> ValidatorFamily:
    return ValidatorFamily(
        family_id="consent_proof",
        description="Verifies that side effects are backed by prior matching consent checks and receipts.",
        authority_ceiling="A4",
        default_visibility="sealed",
        leakage_risk="medium",
        default_failure_action="reject",
        input_contract={"requires": ["consent_checks", "side_effect_receipts"]},
        output_schema={"type": "object", "properties": {"missing_or_invalid_consent": {"type": "array"}}},
        run=_run,
        applicability=_applicability,
    )
