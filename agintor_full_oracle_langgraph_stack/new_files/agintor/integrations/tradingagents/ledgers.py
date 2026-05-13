from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ...utils import stable_hash, now_ts
from .action_mapper import OrderIntent


class TradingDecisionLedger(BaseModel):
    ledger_id: str
    runtime_hash: str = ""
    runtime_spec_digest: str = ""
    symbol: str = ""
    decision_cutoff_ts: float = 0.0
    data_snapshot_digest: str = ""
    recommendations: list[dict[str, Any]] = Field(default_factory=list)
    order_intents: list[OrderIntent] = Field(default_factory=list)
    fills: list[dict[str, Any]] = Field(default_factory=list)
    portfolio_state: dict[str, Any] = Field(default_factory=dict)
    costs: dict[str, float] = Field(default_factory=dict)
    risk_policy_ok: bool = False
    created_at: float = Field(default_factory=now_ts)

    @property
    def digest(self) -> str:
        return stable_hash(self.model_dump(mode="json"))


def reconcile_trading_ledger(ledger: TradingDecisionLedger) -> dict[str, Any]:
    orders_valid = all(intent.symbol and intent.quantity >= 0.0 for intent in ledger.order_intents)
    fills_reconciled = len(ledger.fills) <= len([intent for intent in ledger.order_intents if intent.side != "hold"])
    portfolio_reconciled = bool(ledger.portfolio_state) or not ledger.fills
    data_cutoff_ok = bool(ledger.decision_cutoff_ts and ledger.data_snapshot_digest)
    return {
        "ledger_digest": ledger.digest,
        "data_cutoff_ok": data_cutoff_ok,
        "orders_valid": orders_valid,
        "fills_reconciled": fills_reconciled,
        "portfolio_reconciled": portfolio_reconciled,
        "risk_policy_ok": bool(ledger.risk_policy_ok),
    }


__all__ = ["TradingDecisionLedger", "reconcile_trading_ledger"]
