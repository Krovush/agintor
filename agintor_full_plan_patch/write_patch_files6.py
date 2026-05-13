from __future__ import annotations
from pathlib import Path
import textwrap
ROOT=Path('/mnt/data/agintor_full_plan_patch/new_files'); files={}
def add(p,c): files[p]=textwrap.dedent(c).lstrip()

add('agintor/search/spec_mutator.py', r'''
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..contracts import RuntimeSpec, SpecAction, SpecActionBatch, apply_spec_actions, action_ledger_rows
from ..providers import ModelProvider, ModelRequest
from ..runtime.langgraph.compiler import LangGraphRuntimeCompiler
from ..utils import ensure_directory, stable_hash


@dataclass
class SpecMutationContext:
    objective: str
    touched_scope: list[str]
    runtime_spec: RuntimeSpec
    workspace: Path
    predictor_summaries: dict[str, object]
    failing_train_traces: list[dict[str, object]]
    exemplars: list[dict[str, object]]
    seed: int


@dataclass
class SpecMutationResult:
    parent_spec: RuntimeSpec
    child_spec: RuntimeSpec
    actions: list[SpecAction]
    action_batch: SpecActionBatch
    mutation_ledger_path: str
    compiled_runtime_dir: str = ""


class HeuristicSpecActionMutator:
    def mutate(self, context: SpecMutationContext) -> list[SpecAction]:
        rng = random.Random(context.seed)
        actions: list[SpecAction] = []
        if "ctl" in context.touched_scope:
            actions.append(
                SpecAction(
                    action_id=f"spec-action.{stable_hash(context.runtime_spec.spec_digest, context.seed, 'budget')[:12]}",
                    action_type="set_budget_policy",
                    target_ids=["execution"],
                    scope=[scope for scope in context.touched_scope if scope in {"top", "mem", "tool", "ctl"}],
                    rationale="Adjust runtime step budget for search exploration.",
                    expected_effect="Explore whether a slightly larger execution envelope improves objective performance.",
                    patch={"max_steps": max(4, int(context.runtime_spec.execution.max_steps) + rng.choice([-1, 1, 2]))},
                )
            )
        elif context.runtime_spec.agents:
            agent = context.runtime_spec.agents[rng.randrange(len(context.runtime_spec.agents))]
            actions.append(
                SpecAction(
                    action_id=f"spec-action.{stable_hash(context.runtime_spec.spec_digest, context.seed, agent.agent_id)[:12]}",
                    action_type="set_prompt",
                    target_ids=[agent.agent_id],
                    scope=[scope for scope in context.touched_scope if scope in {"top", "mem", "tool", "ctl"}] or ["top"],
                    rationale="Inject objective-specific bounded instruction into agent prompt.",
                    expected_effect="Improve behavior without changing private validation access.",
                    patch={"prompt": f"{agent.prompt}\n\nOptimization focus: {context.objective}. Use only runtime-visible evidence.".strip()},
                )
            )
        return actions


class ProviderSpecActionMutator:
    def __init__(self, provider: ModelProvider) -> None:
        self.provider = provider

    def mutate(self, context: SpecMutationContext) -> list[SpecAction]:
        request = ModelRequest(
            instructions=(
                "Return strict JSON with key actions. Each action must match the Agintor SpecAction schema. "
                "Do not add private, sealed, hidden, or oracle-only data to the runtime spec."
            ),
            prompt=json.dumps(
                {
                    "objective": context.objective,
                    "touched_scope": context.touched_scope,
                    "runtime_spec": context.runtime_spec.model_dump(mode="json", exclude_none=True),
                    "predictor_summaries": context.predictor_summaries,
                    "failing_train_traces": context.failing_train_traces[:4],
                    "exemplars": context.exemplars[:4],
                },
                indent=2,
                sort_keys=True,
            ),
            model_class="large",
            seed=context.seed,
            metadata={"mode": "spec_action_mutation", "max_output_tokens": 8000},
        )
        response = self.provider.generate(request)
        payload = json.loads(response.text)
        return [SpecAction.model_validate(item) for item in payload.get("actions", [])]


class SpecActionMutator:
    def __init__(self, provider: ModelProvider | None = None, *, use_provider: bool = False) -> None:
        self.inner = ProviderSpecActionMutator(provider) if use_provider and provider is not None else HeuristicSpecActionMutator()

    def mutate_and_compile(self, context: SpecMutationContext, *, compile_runtime: bool = True) -> SpecMutationResult:
        actions = self.inner.mutate(context)
        application = apply_spec_actions(context.runtime_spec, actions)
        child_payload = context.runtime_spec.model_dump(mode="json", exclude_none=True)
        # Rebuild child from application refs by reapplying actions so callers get a concrete spec.
        child = context.runtime_spec
        for action in actions:
            child = apply_spec_actions(child, [action]).model_copy if False else child
        from ..contracts.spec_actions import _apply_one  # local import keeps private helper out of public API
        child = context.runtime_spec
        for action in actions:
            child = _apply_one(child, action)
        child = child.model_copy(update={
            "parent_spec_digest": application.parent_spec_digest,
            "mutation_history": [*context.runtime_spec.mutation_history, *application.mutation_refs],
        }, deep=True)
        workspace = ensure_directory(context.workspace)
        ledger_path = workspace / "mutation_ledger.jsonl"
        with ledger_path.open("a", encoding="utf-8") as handle:
            for row in action_ledger_rows(application):
                handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
        compiled_runtime_dir = ""
        if compile_runtime:
            runtime_dir = ensure_directory(workspace / f"runtime_{child.spec_digest[:12]}")
            LangGraphRuntimeCompiler().export_generated_app(child, runtime_dir)
            compiled_runtime_dir = str(runtime_dir)
        return SpecMutationResult(
            parent_spec=context.runtime_spec,
            child_spec=child,
            actions=actions,
            action_batch=SpecActionBatch(
                batch_id=f"spec-batch.{stable_hash(context.runtime_spec.spec_digest, [a.action_id for a in actions])[:12]}",
                parent_spec_digest=context.runtime_spec.spec_digest,
                actions=actions,
            ),
            mutation_ledger_path=str(ledger_path),
            compiled_runtime_dir=compiled_runtime_dir,
        )


__all__ = [
    "HeuristicSpecActionMutator",
    "ProviderSpecActionMutator",
    "SpecActionMutator",
    "SpecMutationContext",
    "SpecMutationResult",
]
''')

add('agintor/integrations/tradingagents/__init__.py', r'''
from __future__ import annotations

from .spec import *  # noqa: F401,F403
from .adapter import *  # noqa: F401,F403
from .compiler import *  # noqa: F401,F403
from .action_mapper import *  # noqa: F401,F403
from .data_snapshots import *  # noqa: F401,F403
from .ledgers import *  # noqa: F401,F403
from .validators import *  # noqa: F401,F403
from .outcome_oracle_family import *  # noqa: F401,F403
''')

add('agintor/integrations/tradingagents/spec.py', r'''
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from ...contracts import RuntimeSpec


class TradingAgentsRuntimeSpec(RuntimeSpec):
    runtime_kind: Literal["tradingagents_langgraph_v1"] = "tradingagents_langgraph_v1"
    selected_analysts: list[str] = Field(default_factory=lambda: ["market", "news", "fundamentals"])
    deep_think_model: str = "large"
    quick_think_model: str = "small"
    debate_rounds: int = 1
    risk_discussion_rounds: int = 1
    data_vendor_policy: dict[str, Any] = Field(default_factory=dict)
    action_mapping_policy_id: str = "bounded_order_intent_v1"
    risk_policy_id: str = "default_risk_v1"


__all__ = ["TradingAgentsRuntimeSpec"]
''')

add('agintor/integrations/tradingagents/compiler.py', r'''
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
''')

add('agintor/integrations/tradingagents/adapter.py', r'''
from __future__ import annotations

from pathlib import Path
from typing import Any

from ...runtime.langgraph.compiler import LangGraphRuntimeCompiler
from .spec import TradingAgentsRuntimeSpec


class TradingAgentsAdapter:
    def compile_runtime(self, spec: TradingAgentsRuntimeSpec, output_dir: str | Path) -> Path:
        return LangGraphRuntimeCompiler().export_generated_app(spec, output_dir)

    def public_runtime_summary(self, spec: TradingAgentsRuntimeSpec) -> dict[str, Any]:
        return {
            "runtime_id": spec.runtime_id,
            "runtime_kind": spec.runtime_kind,
            "selected_analysts": list(spec.selected_analysts),
            "debate_rounds": spec.debate_rounds,
            "risk_discussion_rounds": spec.risk_discussion_rounds,
            "risk_policy_id": spec.risk_policy_id,
        }


__all__ = ["TradingAgentsAdapter"]
''')

add('agintor/integrations/tradingagents/action_mapper.py', r'''
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
''')

add('agintor/integrations/tradingagents/data_snapshots.py', r'''
from __future__ import annotations

from pydantic import BaseModel, Field

from ...utils import stable_hash


class MarketDataSnapshot(BaseModel):
    snapshot_id: str
    symbol: str
    as_of: str
    source: str
    rows: list[dict] = Field(default_factory=list)
    digest: str = ""

    def sealed_digest(self) -> str:
        return self.digest or stable_hash(self.model_dump(mode="json", exclude_none=True))


__all__ = ["MarketDataSnapshot"]
''')

add('agintor/integrations/tradingagents/ledgers.py', r'''
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
''')

add('agintor/integrations/tradingagents/validators.py', r'''
from __future__ import annotations

from typing import Any

from ...contracts import ValidatorResult
from ...utils import stable_hash


def validate_order_intent(order_intent: dict[str, Any]) -> ValidatorResult:
    required = {"symbol", "side", "quantity"}
    missing = sorted(required - set(order_intent))
    status = "fail" if missing else "pass"
    return ValidatorResult(
        validator_id="trading_outcome.order_intent",
        claim_ids=["trading.order_validity"],
        status=status,
        authority_used="A4",
        observations={"missing": missing, "order_intent": order_intent},
        evidence_digest=stable_hash(order_intent, missing),
    )


def validate_data_cutoff(decision_time: str, source_times: list[str]) -> ValidatorResult:
    violations = [source_time for source_time in source_times if str(source_time) > str(decision_time)]
    return ValidatorResult(
        validator_id="trading_outcome.data_cutoff",
        claim_ids=["trading.data_cutoff"],
        status="fail" if violations else "pass",
        authority_used="A4",
        observations={"violations": violations, "decision_time": decision_time},
    )


__all__ = ["validate_data_cutoff", "validate_order_intent"]
''')

add('agintor/integrations/tradingagents/outcome_oracle_family.py', r'''
from __future__ import annotations

from ...oracle.families.trading_outcome import make_family

__all__ = ["make_family"]
''')

for p,c in files.items():
    t=ROOT/p; t.parent.mkdir(parents=True, exist_ok=True); t.write_text(c, encoding='utf-8')
print('wrote', len(files), 'files')
