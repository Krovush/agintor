from __future__ import annotations

from ...contracts import AgentSpec, GraphEdgeSpec, GraphNodeSpec, GraphSpec, GoalSpec, ModelPolicy
from ...utils import stable_hash
from .spec import TradingAgentsRuntimeSpec


def tradingagents_spec_from_goal(goal_spec: GoalSpec) -> TradingAgentsRuntimeSpec:
    runtime_id = f"tradingagents.{stable_hash(goal_spec.goal_id, goal_spec.normalized_goal)[:12]}"
    agents = [
        AgentSpec(agent_id="agent.market", name="Market Analyst", role="analyst", model_policy_id="quick"),
        AgentSpec(agent_id="agent.news", name="News Analyst", role="analyst", model_policy_id="quick"),
        AgentSpec(agent_id="agent.research", name="Research Debate", role="researcher", model_policy_id="deep"),
        AgentSpec(agent_id="agent.trader", name="Trader", role="trader", model_policy_id="deep"),
        AgentSpec(agent_id="agent.risk", name="Risk Manager", role="risk", model_policy_id="deep"),
    ]
    nodes = [
        GraphNodeSpec(node_id="node.market", agent_id="agent.market", outputs=["market_view"]),
        GraphNodeSpec(node_id="node.news", agent_id="agent.news", outputs=["news_view"]),
        GraphNodeSpec(node_id="node.research", agent_id="agent.research", outputs=["research_debate"]),
        GraphNodeSpec(node_id="node.trader", agent_id="agent.trader", outputs=["order_intent"]),
        GraphNodeSpec(node_id="node.risk", agent_id="agent.risk", outputs=["risk_checked_order"]),
        GraphNodeSpec(node_id="node.terminal", node_kind="terminal"),
    ]
    edges = [
        GraphEdgeSpec(edge_id="edge.market.research", source="node.market", target="node.research"),
        GraphEdgeSpec(edge_id="edge.news.research", source="node.news", target="node.research"),
        GraphEdgeSpec(edge_id="edge.research.trader", source="node.research", target="node.trader"),
        GraphEdgeSpec(edge_id="edge.trader.risk", source="node.trader", target="node.risk"),
        GraphEdgeSpec(edge_id="edge.risk.terminal", source="node.risk", target="node.terminal"),
    ]
    return TradingAgentsRuntimeSpec(
        runtime_id=runtime_id,
        name=f"TradingAgents runtime for {goal_spec.goal_id}",
        description=goal_spec.normalized_goal,
        agents=agents,
        graph=GraphSpec(entry_node_id="node.market", terminal_node_ids=["node.terminal"], nodes=nodes, edges=edges),
        models=[ModelPolicy(model_policy_id="quick", model_class="small"), ModelPolicy(model_policy_id="deep", model_class="large")],
    )


__all__ = ["tradingagents_spec_from_goal"]
