from __future__ import annotations

from typing import Sequence

from ...contracts import ClaimSpec, ValidationIntent, ValidatorSpec
from ..validator_registry import ValidatorFamily
from ...utils import stable_hash

FAMILY_ID = 'factual_grounded'
KEYWORDS = ['factual', 'citation', 'source', 'grounded', 'research', 'freshness']
AUTHORITY = 'A3'
VISIBILITY = 'private'
LEAKAGE_RISK = 'medium'
FAILURE_ACTION = 'abstain'
DESCRIPTION = 'Grounded factuality and citation support validator family.'


def _score(intent: ValidationIntent, claims: Sequence[ClaimSpec]) -> float:
    haystack = " ".join([*intent.task_classes, *intent.required_capabilities, *(claim.text for claim in claims)]).lower()
    hits = sum(1 for keyword in KEYWORDS if keyword in haystack)
    if FAMILY_ID in intent.task_classes:
        hits += 3
    return min(1.0, hits / max(1, min(4, len(KEYWORDS))))


def _build(intent: ValidationIntent, claims: Sequence[ClaimSpec]) -> list[ValidatorSpec]:
    applicable = [claim for claim in claims if _score(intent, [claim]) >= 0.2]
    if not applicable and claims:
        applicable = [claims[0]]
    return [
        ValidatorSpec(
            validator_id=f"{FAMILY_ID}.{stable_hash(claim.claim_id, claim.text)[:10]}",
            family_id=FAMILY_ID,
            claim_ids=[claim.claim_id],
            inputs={
                "claim_text": claim.text,
                "task_classes": list(intent.task_classes),
                "required_capabilities": list(intent.required_capabilities),
            },
            outputs_schema={
                "type": "object",
                "properties": {
                    "status": {"enum": ["pass", "fail", "abstain", "error"]},
                    "observations": {"type": "object"},
                },
                "required": ["status"],
            },
            authority_ceiling=AUTHORITY,
            visibility=VISIBILITY,
            independence_group=FAMILY_ID,
            leakage_risk=LEAKAGE_RISK,
            health_tests=["positive_control", "negative_control"],
            failure_action=FAILURE_ACTION,
        )
        for claim in applicable
    ]


def make_family() -> ValidatorFamily:
    return ValidatorFamily(
        family_id=FAMILY_ID,
        description=DESCRIPTION,
        authority_ceiling=AUTHORITY,
        visibility=VISIBILITY,
        leakage_risk=LEAKAGE_RISK,
        failure_action=FAILURE_ACTION,
        can_handle=_score,
        build_specs=_build,
        health_tests=("positive_control", "negative_control"),
    )


__all__ = ["make_family"]
