from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class OrderIntent(BaseModel):
    symbol: str
    side: Literal["buy", "sell", "hold"] = "hold"
    quantity: float = 0.0
    order_type: Literal["market", "limit"] = "market"
    limit_price: float | None = None
    rationale_ref: str = ""
    risk_limit_ref: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


def recommendation_to_order_intent(recommendation: dict[str, Any], *, default_symbol: str = "") -> OrderIntent:
    action = str(recommendation.get("action", recommendation.get("recommendation", "hold"))).lower()
    side = "hold"
    if action in {"buy", "long", "accumulate"}:
        side = "buy"
    elif action in {"sell", "short", "reduce"}:
        side = "sell"
    return OrderIntent(
        symbol=str(recommendation.get("symbol") or default_symbol),
        side=side,
        quantity=max(0.0, float(recommendation.get("quantity", 0.0) or 0.0)),
        order_type=str(recommendation.get("order_type", "market")) if str(recommendation.get("order_type", "market")) in {"market", "limit"} else "market",
        limit_price=recommendation.get("limit_price"),
        rationale_ref=str(recommendation.get("rationale_ref", "")),
        risk_limit_ref=str(recommendation.get("risk_limit_ref", "")),
        metadata={"source": "tradingagents_adapter"},
    )


__all__ = ["OrderIntent", "recommendation_to_order_intent"]
