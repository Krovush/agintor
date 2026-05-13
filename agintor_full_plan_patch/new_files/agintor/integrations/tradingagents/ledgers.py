from __future__ import annotations

from pydantic import BaseModel, Field

from ...utils import stable_hash


class TradingDecisionLedgerRow(BaseModel):
    decision_id: str
    runtime_hash: str
    runtime_spec_digest: str
    symbol: str
    cutoff_time: str
    recommendation: str
    order_intent: dict = Field(default_factory=dict)
    risk_checks: dict = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)

    @property
    def ledger_digest(self) -> str:
        return stable_hash(self.model_dump(mode="json", exclude_none=True))


class FillReconciliationRow(BaseModel):
    fill_id: str
    order_id: str
    symbol: str
    quantity: float
    price: float
    fees: float = 0.0
    reconciled: bool = False


__all__ = ["FillReconciliationRow", "TradingDecisionLedgerRow"]
