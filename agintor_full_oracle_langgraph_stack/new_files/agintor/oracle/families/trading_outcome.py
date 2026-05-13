from __future__ import annotations

from typing import Any

from ...contracts import ValidatorResult, ValidatorSpec
from ..validator_registry import ValidatorFamily


def _run(spec: ValidatorSpec, payload: dict[str, Any]) -> ValidatorResult:
    ledger = payload.get("trading_ledger") or {}
    data_cutoff_ok = bool(ledger.get("data_cutoff_ok", False))
    orders_valid = bool(ledger.get("orders_valid", False))
    fills_reconciled = bool(ledger.get("fills_reconciled", False))
    portfolio_reconciled = bool(ledger.get("portfolio_reconciled", False))
    risk_ok = bool(ledger.get("risk_policy_ok", False))
    outcome = ledger.get("outcome_metrics", {}) if isinstance(ledger, dict) else {}
    passed = data_cutoff_ok and orders_valid and fills_reconciled and portfolio_reconciled and risk_ok
    return ValidatorResult(
        validator_id=spec.validator_id,
        family_id=spec.family_id,
        claim_ids=list(spec.claim_ids),
        status="pass" if passed else "fail",
        authority_used="A4" if outcome else "A3",
        health_status={"price_snapshot_loaded": bool(ledger.get("price_snapshot_digest")), "ledger_reconciled": portfolio_reconciled},
        observations={
            "data_cutoff_ok": data_cutoff_ok,
            "orders_valid": orders_valid,
            "fills_reconciled": fills_reconciled,
            "portfolio_reconciled": portfolio_reconciled,
            "risk_policy_ok": risk_ok,
            "outcome_metrics": outcome,
        },
    )


def _applicability(context: dict[str, Any]) -> float:
    goal = str(context.get("goal_text", "")).lower()
    return 0.97 if any(word in goal for word in ["trading", "trade", "stock", "portfolio", "alpha", "pnl", "market"]) else 0.0


def family() -> ValidatorFamily:
    return ValidatorFamily(
        family_id="trading_outcome",
        description="TradingAgents-style validator for cutoff integrity, order/fill/portfolio reconciliation, risk policy, and post-close outcome.",
        authority_ceiling="A4",
        default_visibility="sealed",
        leakage_risk="high",
        default_failure_action="reject",
        input_contract={"requires": ["trading_ledger"], "sealed_optional": ["price_snapshots"]},
        output_schema={"type": "object", "properties": {"outcome_metrics": {"type": "object"}}},
        run=_run,
        applicability=_applicability,
    )
