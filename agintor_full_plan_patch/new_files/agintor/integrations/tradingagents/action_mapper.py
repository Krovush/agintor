from __future__ import annotations

from ...contracts import SpecAction


def tradingagents_action_to_spec_action(action: dict) -> SpecAction:
    action_type = str(action.get("action_type", "set_routing_policy"))
    return SpecAction(
        action_id=str(action.get("action_id", "tradingagents.action")),
        action_type=action_type,  # type: ignore[arg-type]
        target_ids=[str(item) for item in action.get("target_ids", [])],
        scope=[scope for scope in action.get("scope", ["top"]) if scope in {"top", "mem", "tool", "ctl"}],
        rationale=str(action.get("rationale", "TradingAgents adapter action")),
        expected_effect=str(action.get("expected_effect", "Improve trading workflow behavior")),
        patch=dict(action.get("patch", {})),
    )


__all__ = ["tradingagents_action_to_spec_action"]
