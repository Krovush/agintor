from __future__ import annotations

from agintor.contracts import GoalSpec
from agintor.integrations.tradingagents.compiler import tradingagents_spec_from_goal


def test_tradingagents_spec_from_goal():
    goal = GoalSpec(goal_id="g1", raw_prompt="Build a trading agent", normalized_goal="Build a trading agent")
    spec = tradingagents_spec_from_goal(goal)
    assert spec.runtime_kind == "tradingagents_langgraph_v1"
    assert "market" in spec.selected_analysts
