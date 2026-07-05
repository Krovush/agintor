from __future__ import annotations

from typing import Any

from ...contracts import AgentSpec, GraphEdgeSpec, GraphNodeSpec, GraphSpec, ModelPolicy, PromptSpec, RuntimeSpec
from ...contracts.runtime_spec import ToolSpec
from .spec import TradingAgentsRuntimeSpec


def tradingagents_seed_spec(*, runtime_id: str = "runtime.tradingagents.seed", symbols: list[str] | None = None) -> TradingAgentsRuntimeSpec:
    symbols = symbols or ["SPY"]
    agents = [
        AgentSpec(agent_id="market_analyst", role="market_analyst", description="Analyzes market data before cutoff.", prompt=PromptSpec(system="Analyze market data using only allowed pre-cutoff inputs."), model_policy_id="quick", scope=["top"]),
        AgentSpec(agent_id="researcher", role="researcher", description="Synthesizes bullish/bearish evidence.", prompt=PromptSpec(system="Debate evidence and produce bounded recommendation."), model_policy_id="deep", scope=["top", "ctl"]),
        AgentSpec(agent_id="risk_manager", role="risk_manager", description="Checks risk policy and order constraints.", prompt=PromptSpec(system="Reject recommendations that violate risk policy."), model_policy_id="quick", scope=["ctl"]),
        AgentSpec(agent_id="portfolio_manager", role="portfolio_manager", description="Maps decisions to bounded order intents.", prompt=PromptSpec(system="Map final recommendation to valid order intent."), model_policy_id="quick", scope=["tool", "ctl"]),
    ]
    nodes = [
        GraphNodeSpec(node_id="market", node_type="agent", agent_id="market_analyst", output_key="market_report"),
        GraphNodeSpec(node_id="research", node_type="agent", agent_id="researcher", input_keys=["market_report"], output_key="research_report"),
        GraphNodeSpec(node_id="risk", node_type="agent", agent_id="risk_manager", input_keys=["research_report"], output_key="risk_report"),
        GraphNodeSpec(node_id="portfolio", node_type="agent", agent_id="portfolio_manager", input_keys=["risk_report"], output_key="order_intent"),
    ]
    return TradingAgentsRuntimeSpec(
        runtime_id=runtime_id,
        name="TradingAgents LangGraph Seed",
        description="TradingAgents-shaped LangGraph runtime seed with analysts, debate, risk, and portfolio mapping.",
        agents=agents,
        graph=GraphSpec(
            graph_id="tradingagents",
            entry_node="market",
            terminal_nodes=["portfolio"],
            nodes=nodes,
            edges=[GraphEdgeSpec(source="market", target="research"), GraphEdgeSpec(source="research", target="risk"), GraphEdgeSpec(source="risk", target="portfolio")],
        ),
        tools=[ToolSpec(tool_id="market_data", name="Market data", family="trading/data", description="Frozen market data snapshot tool.", runtime_visible=True)],
        models=[ModelPolicy(model_policy_id="quick", model_class="small"), ModelPolicy(model_policy_id="deep", model_class="medium")],
        data_vendor_policy={"allowed_symbols": symbols, "cutoff_required": True},
        metadata={"symbols": symbols, "integration": "tradingagents"},
    )


def adapt_external_tradingagents_config(config: dict[str, Any]) -> TradingAgentsRuntimeSpec:
    symbols = list(config.get("symbols") or config.get("tickers") or ["SPY"])
    spec = tradingagents_seed_spec(symbols=symbols)
    return spec.model_copy(
        update={
            "selected_analysts": list(config.get("selected_analysts", spec.selected_analysts)),
            "debate_rounds": int(config.get("debate_rounds", spec.debate_rounds)),
            "risk_discussion_rounds": int(config.get("risk_discussion_rounds", spec.risk_discussion_rounds)),
            "data_vendor_policy": {**spec.data_vendor_policy, **dict(config.get("data_vendor_policy", {}))},
        },
        deep=True,
    )


__all__ = ["adapt_external_tradingagents_config", "tradingagents_seed_spec"]
