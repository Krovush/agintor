from __future__ import annotations

from typing import Any

from ...contracts import ValidatorResult
from ...utils import stable_hash


def validate_order_intent(order_intent: dict[str, Any]) -> ValidatorResult:
    required = {"symbol", "side", "quantity"}
    missing = sorted(required - set(order_intent))
    status = "fail" if missing else "pass"
    return ValidatorResult(
        validator_id="trading_outcome.order_intent",
        claim_ids=["trading.order_validity"],
        status=status,
        authority_used="A4",
        observations={"missing": missing, "order_intent": order_intent},
        evidence_digest=stable_hash(order_intent, missing),
    )


def validate_data_cutoff(decision_time: str, source_times: list[str]) -> ValidatorResult:
    violations = [source_time for source_time in source_times if str(source_time) > str(decision_time)]
    return ValidatorResult(
        validator_id="trading_outcome.data_cutoff",
        claim_ids=["trading.data_cutoff"],
        status="fail" if violations else "pass",
        authority_used="A4",
        observations={"violations": violations, "decision_time": decision_time},
    )


__all__ = ["validate_data_cutoff", "validate_order_intent"]
