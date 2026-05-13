from __future__ import annotations

from typing import Any

from ...contracts import ValidatorResult, ValidatorSpec
from ..validator_registry import ValidatorFamily


def _run(spec: ValidatorSpec, payload: dict[str, Any]) -> ValidatorResult:
    preference = str(payload.get("preference", "")).lower()
    calibrated = bool(payload.get("calibrated", False))
    won = preference in {"child", "candidate", "new"}
    status = "pass" if won else "fail" if preference in {"parent", "baseline"} else "abstain"
    return ValidatorResult(
        validator_id=spec.validator_id,
        family_id=spec.family_id,
        claim_ids=list(spec.claim_ids),
        status=status,
        authority_used="A2" if calibrated else "A1",
        health_status={"calibrated": calibrated, "preference_present": bool(preference)},
        observations={"preference": preference, "calibrated": calibrated},
    )


def _applicability(context: dict[str, Any]) -> float:
    goal = str(context.get("goal_text", "")).lower()
    return 0.7 if any(word in goal for word in ["preference", "style", "quality", "user liked", "human review"]) else 0.05


def family() -> ValidatorFamily:
    return ValidatorFamily(
        family_id="pairwise_preference",
        description="Human/model preference validator with low authority cap unless calibrated and combined with stronger evidence.",
        authority_ceiling="A2",
        default_visibility="private",
        leakage_risk="medium",
        default_failure_action="diagnostic",
        input_contract={"requires": ["parent_artifact", "child_artifact"]},
        output_schema={"type": "object", "properties": {"preference": {"type": "string"}}},
        run=_run,
        applicability=_applicability,
    )
