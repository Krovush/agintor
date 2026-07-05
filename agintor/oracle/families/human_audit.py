from __future__ import annotations

from typing import Any

from ...contracts import ValidatorResult, ValidatorSpec
from ..validator_registry import ValidatorFamily


def _run(spec: ValidatorSpec, payload: dict[str, Any]) -> ValidatorResult:
    audit = payload.get("human_audit") or {}
    signed = bool(audit.get("signed", False))
    approved = bool(audit.get("approved", False))
    return ValidatorResult(
        validator_id=spec.validator_id,
        family_id=spec.family_id,
        claim_ids=list(spec.claim_ids),
        status="pass" if signed and approved else "fail" if signed else "abstain",
        authority_used="A5" if signed else "A0",
        health_status={"signed": signed},
        observations={"audit_ref": audit.get("audit_ref", ""), "approved": approved},
    )


def _applicability(context: dict[str, Any]) -> float:
    return 0.8 if context.get("requires_human_audit") else 0.05


def family() -> ValidatorFamily:
    return ValidatorFamily(
        family_id="human_audit",
        description="Stores signed human review references as bounded A5 evidence.",
        authority_ceiling="A5",
        default_visibility="private",
        leakage_risk="medium",
        default_failure_action="abstain",
        input_contract={"requires": ["human_audit"]},
        output_schema={"type": "object", "properties": {"approved": {"type": "boolean"}}},
        run=_run,
        applicability=_applicability,
    )
