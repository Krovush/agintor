from __future__ import annotations

from typing import Any

from ...contracts import ValidatorResult, ValidatorSpec
from ..validator_registry import ValidatorFamily


def _run(spec: ValidatorSpec, payload: dict[str, Any]) -> ValidatorResult:
    artifact = payload.get("artifact") or {}
    citations = artifact.get("citations", []) if isinstance(artifact, dict) else []
    contradictions = payload.get("contradictions", [])
    freshness_ok = payload.get("freshness_ok", True)
    passed = bool(citations) and not contradictions and bool(freshness_ok)
    return ValidatorResult(
        validator_id=spec.validator_id,
        family_id=spec.family_id,
        claim_ids=list(spec.claim_ids),
        status="pass" if passed else "abstain" if not citations else "fail",
        authority_used="A3" if citations else "A1",
        health_status={"retrieval_present": bool(citations), "contradiction_scan": True},
        observations={"citation_count": len(citations), "contradictions": list(contradictions), "freshness_ok": freshness_ok},
    )


def _applicability(context: dict[str, Any]) -> float:
    goal = str(context.get("goal_text", "")).lower()
    return 0.85 if any(word in goal for word in ["research", "facts", "cite", "current", "news", "source"]) else 0.15


def family() -> ValidatorFamily:
    return ValidatorFamily(
        family_id="factual_grounded",
        description="Grounding validator for source support, source freshness, and contradiction checks.",
        authority_ceiling="A3",
        default_visibility="sealed",
        leakage_risk="medium",
        default_failure_action="abstain",
        input_contract={"requires": ["artifact", "sources"]},
        output_schema={"type": "object", "properties": {"citation_count": {"type": "number"}}},
        run=_run,
        applicability=_applicability,
    )
