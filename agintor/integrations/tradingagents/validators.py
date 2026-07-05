from __future__ import annotations

from typing import Any

from ...contracts import ValidatorResult, ValidatorSpec
from .ledgers import TradingDecisionLedger, reconcile_trading_ledger


def validate_trading_decision_ledger(spec: ValidatorSpec, ledger_payload: dict[str, Any]) -> ValidatorResult:
    ledger = TradingDecisionLedger.model_validate(ledger_payload)
    reconciliation = reconcile_trading_ledger(ledger)
    passed = all(bool(reconciliation[key]) for key in ["data_cutoff_ok", "orders_valid", "fills_reconciled", "portfolio_reconciled"]) and bool(ledger.risk_policy_ok)
    return ValidatorResult(
        validator_id=spec.validator_id,
        family_id=spec.family_id or "trading_outcome",
        claim_ids=list(spec.claim_ids),
        status="pass" if passed else "fail",
        authority_used="A4",
        health_status={"ledger_digest": ledger.digest, "reconciliation": reconciliation},
        observations=reconciliation,
    )


__all__ = ["validate_trading_decision_ledger"]
