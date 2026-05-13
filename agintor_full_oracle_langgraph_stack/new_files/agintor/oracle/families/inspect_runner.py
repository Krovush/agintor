from __future__ import annotations

from typing import Any

from ...contracts import ValidatorResult, ValidatorSpec
from ..validator_registry import ValidatorFamily


def _run(spec: ValidatorSpec, payload: dict[str, Any]) -> ValidatorResult:
    result = payload.get("inspect_result") or {}
    score = float(result.get("score", 0.0) or 0.0) if isinstance(result, dict) else 0.0
    return ValidatorResult(
        validator_id=spec.validator_id,
        family_id=spec.family_id,
        claim_ids=list(spec.claim_ids),
        status="pass" if score >= float(spec.inputs.get("pass_threshold", 1.0)) else "fail",
        authority_used="A4" if bool(result.get("sandboxed", False)) else "A3",
        health_status={"runner": "inspect", "sandboxed": bool(result.get("sandboxed", False))},
        observations={"score": score, "raw": result},
    )


def _applicability(context: dict[str, Any]) -> float:
    return 0.6 if context.get("inspect_task_available") else 0.05


def family() -> ValidatorFamily:
    return ValidatorFamily(
        family_id="inspect_runner",
        description="Adapter for Inspect tasks, scorers, solvers, and sandboxes. Agintor still owns promotion authority.",
        authority_ceiling="A4",
        default_visibility="sealed",
        leakage_risk="medium",
        default_failure_action="abstain",
        input_contract={"requires": ["inspect_task_ref"]},
        output_schema={"type": "object", "properties": {"score": {"type": "number"}}},
        run=_run,
        applicability=_applicability,
    )
