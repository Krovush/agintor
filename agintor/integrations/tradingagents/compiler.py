from __future__ import annotations

from ...contracts import AgentSpec, GraphEdgeSpec, GraphNodeSpec, GraphSpec, GoalSpec, ModelPolicy, PromptSpec
from ...utils import stable_hash
from .spec import TradingAgentsRuntimeSpec


def tradingagents_spec_from_goal(goal_spec: GoalSpec) -> TradingAgentsRuntimeSpec:
    runtime_id = f"tradingagents.{stable_hash(goal_spec.goal_id, goal_spec.normalized_goal)[:12]}"
    agents = [
        AgentSpec(
            agent_id="agent.market",
            role="analyst",
            description="Market analyst",
            prompt=PromptSpec(output_instructions="Summarize observable market context."),
            model_policy_id="quick",
            scope=["top"],
        ),
        AgentSpec(
            agent_id="agent.news",
            role="analyst",
            description="News analyst",
            prompt=PromptSpec(output_instructions="Summarize public news context."),
            model_policy_id="quick",
            scope=["top"],
        ),
        AgentSpec(
            agent_id="agent.research",
            role="researcher",
            description="Research debate synthesizer",
            prompt=PromptSpec(output_instructions="Combine market and news observations into a trade thesis."),
            model_policy_id="deep",
            scope=["top"],
        ),
        AgentSpec(
            agent_id="agent.trader",
            role="trader",
            description="Trading decision maker",
            prompt=PromptSpec(output_instructions="Convert the thesis into a cautious order intent."),
            model_policy_id="deep",
            scope=["top"],
        ),
        AgentSpec(
            agent_id="agent.risk",
            role="risk",
            description="Risk manager",
            prompt=PromptSpec(output_instructions="Approve, adjust, or reject the order intent based on risk."),
            model_policy_id="deep",
            scope=["ctl"],
        ),
    ]
    nodes = [
        GraphNodeSpec(node_id="node.market", node_type="agent", agent_id="agent.market", output_key="market_view"),
        GraphNodeSpec(node_id="node.news", node_type="agent", agent_id="agent.news", output_key="news_view"),
        GraphNodeSpec(
            node_id="node.research",
            node_type="agent",
            agent_id="agent.research",
            input_keys=["market_view", "news_view"],
            output_key="research_debate",
        ),
        GraphNodeSpec(
            node_id="node.trader",
            node_type="agent",
            agent_id="agent.trader",
            input_keys=["research_debate"],
            output_key="order_intent",
        ),
        GraphNodeSpec(
            node_id="node.risk",
            node_type="agent",
            agent_id="agent.risk",
            input_keys=["order_intent"],
            output_key="risk_checked_order",
        ),
        GraphNodeSpec(node_id="node.terminal", node_type="verify", input_keys=["risk_checked_order"]),
    ]
    edges = [
        GraphEdgeSpec(source="node.market", target="node.news", priority=0),
        GraphEdgeSpec(source="node.news", target="node.research", priority=0),
        GraphEdgeSpec(source="node.research", target="node.trader", priority=0),
        GraphEdgeSpec(source="node.trader", target="node.risk", priority=0),
        GraphEdgeSpec(source="node.risk", target="node.terminal", priority=0),
    ]
    return TradingAgentsRuntimeSpec(
        runtime_id=runtime_id,
        name=f"TradingAgents runtime for {goal_spec.goal_id}",
        description=goal_spec.normalized_goal,
        agents=agents,
        graph=GraphSpec(
            graph_id="tradingagents",
            entry_node="node.market",
            terminal_nodes=["node.terminal"],
            nodes=nodes,
            edges=edges,
        ),
        models=[
            ModelPolicy(model_policy_id="quick", provider_name="runtime_default", model_class="small"),
            ModelPolicy(model_policy_id="deep", provider_name="runtime_default", model_class="large"),
        ],
    )


__all__ = ["tradingagents_spec_from_goal"]
