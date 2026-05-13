from __future__ import annotations

from agintor.integrations.tradingagents import recommendation_to_order_intent, tradingagents_seed_spec


def test_tradingagents_seed_spec_is_runtime_spec_profile():
    spec = tradingagents_seed_spec(symbols=["AAPL"])
    assert spec.runtime_kind == "tradingagents_langgraph_v1"
    assert "AAPL" in spec.data_vendor_policy["allowed_symbols"]
    assert spec.spec_digest


def test_recommendation_maps_to_bounded_order_intent():
    intent = recommendation_to_order_intent({"symbol": "MSFT", "action": "buy", "quantity": 5})
    assert intent.side == "buy"
    assert intent.quantity == 5
