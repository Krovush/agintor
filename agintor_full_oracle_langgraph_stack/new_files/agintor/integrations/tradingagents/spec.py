from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from ...contracts import RuntimeSpec


class TradingAgentsRuntimeSpec(RuntimeSpec):
    runtime_kind: Literal["tradingagents_langgraph_v1"] = "tradingagents_langgraph_v1"
    selected_analysts: list[str] = Field(default_factory=lambda: ["market", "news", "fundamentals"])
    deep_think_model: str = "runtime_provider/deep"
    quick_think_model: str = "runtime_provider/quick"
    debate_rounds: int = 1
    risk_discussion_rounds: int = 1
    data_vendor_policy: dict[str, Any] = Field(default_factory=dict)
    action_mapping_policy_id: str = "bounded_order_intent.v1"
    risk_policy_id: str = "default_risk.v1"


__all__ = ["TradingAgentsRuntimeSpec"]
