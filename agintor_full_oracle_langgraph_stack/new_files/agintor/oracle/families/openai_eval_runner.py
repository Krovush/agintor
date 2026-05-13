from __future__ import annotations

from typing import Any

from ...contracts import ValidatorResult, ValidatorSpec
from ..validator_registry import ValidatorFamily


def _run(spec: ValidatorSpec, payload: dict[str, Any]) -> ValidatorResult:
    result = payload.get("openai_eval_result") or {}
    passed = bool(result.get("passed", False))
    calibrated = bool(result.get("calibrated", False))
    return ValidatorResult(
        validator_id=spec.validator_id,
        family_id=spec.family_id,
        claim_ids=list(spec.claim_ids),
        status="pass" if passed else "fail",
        authority_used="A3" if calibrated else "A2",
        health_status={"runner": "openai_evals", "calibrated": calibrated},
        observations={"passed": passed, "score": result.get("score"), "eval_ref": result.get("eval_ref", "")},
    )


def _applicability(context: dict[str, Any]) -> float:
    return 0.5 if context.get("openai_eval_available") else 0.05


def family() -> ValidatorFamily:
    return ValidatorFamily(
        family_id="openai_eval_runner",
        description="Adapter for OpenAI Evals-style private evals. Model graders remain authority-capped.",
        authority_ceiling="A3",
        default_visibility="sealed",
        leakage_risk="medium",
        default_failure_action="abstain",
        input_contract={"requires": ["eval_ref"]},
        output_schema={"type": "object", "properties": {"passed": {"type": "boolean"}}},
        run=_run,
        applicability=_applicability,
    )
