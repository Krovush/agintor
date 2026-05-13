from __future__ import annotations
from pathlib import Path
import textwrap
ROOT=Path('/mnt/data/agintor_full_plan_patch/new_files'); files={}
def add(p,c): files[p]=textwrap.dedent(c).lstrip()

add('templates/baseline_runtime_v2/runtime_spec.json', r'''
{
  "schema_version": "agintor.runtime_spec.v2",
  "runtime_id": "baseline.langgraph.v2",
  "runtime_kind": "langgraph_spec_v2",
  "name": "Baseline LangGraph Runtime V2",
  "description": "Spec-backed baseline runtime for Agintor pass-1 validation.",
  "agents": [
    {
      "agent_id": "agent.default",
      "name": "Default Agent",
      "role": "worker",
      "prompt": "Solve the request using only runtime-visible evidence.",
      "model_policy_id": "default"
    }
  ],
  "graph": {
    "graph_id": "runtime_graph",
    "entry_node_id": "node.default",
    "terminal_node_ids": ["node.terminal"],
    "nodes": [
      {"node_id": "node.default", "agent_id": "agent.default", "node_kind": "agent", "outputs": ["answer"]},
      {"node_id": "node.terminal", "node_kind": "terminal"}
    ],
    "edges": [
      {"edge_id": "edge.default.terminal", "source": "node.default", "target": "node.terminal"}
    ]
  },
  "tools": [],
  "models": [
    {"model_policy_id": "default", "provider_name": "runtime_default", "model_class": "small"}
  ],
  "memory": {"memory_policy_id": "default", "memory_kind": "short_term"},
  "execution": {"max_steps": 32, "side_effect_policy": "receipt_required"},
  "tracing": {"trace_level": "full"},
  "mutation_history": [],
  "metadata": {"template": "baseline_runtime_v2"}
}
''')

add('templates/baseline_runtime_v2/langgraph_app.py', r'''
from __future__ import annotations

import json
from pathlib import Path

from agintor.contracts import RuntimeSpec
from agintor.runtime.langgraph.compiler import LangGraphRuntimeCompiler


def load_app(runtime_dir: str | Path):
    runtime_path = Path(runtime_dir)
    spec = RuntimeSpec.model_validate(json.loads((runtime_path / "runtime_spec.json").read_text(encoding="utf-8")))
    return LangGraphRuntimeCompiler().compile(spec)
''')

add('tests/test_runtime_spec.py', r'''
from __future__ import annotations

import pytest

from agintor.contracts import default_langgraph_runtime_spec, runtime_spec_digest


def test_runtime_spec_digest_stable():
    spec = default_langgraph_runtime_spec(runtime_id="r1", name="Runtime")
    assert runtime_spec_digest(spec) == runtime_spec_digest(spec.model_dump(mode="json"))


def test_runtime_spec_rejects_private_fields():
    payload = default_langgraph_runtime_spec(runtime_id="r1", name="Runtime").model_dump(mode="json")
    payload["metadata"] = {"private_expected": "do not leak"}
    with pytest.raises(ValueError):
        type(default_langgraph_runtime_spec(runtime_id="r2", name="Runtime")).model_validate(payload)
''')

add('tests/test_spec_actions.py', r'''
from __future__ import annotations

from agintor.contracts import SpecAction, apply_spec_actions, default_langgraph_runtime_spec


def test_spec_action_prompt_mutation_changes_digest():
    spec = default_langgraph_runtime_spec(runtime_id="r1", name="Runtime")
    action = SpecAction(
        action_id="a1",
        action_type="set_prompt",
        target_ids=["agent.default"],
        scope=["top"],
        patch={"prompt": "new prompt"},
    )
    app = apply_spec_actions(spec, [action])
    assert app.changed
    assert app.parent_spec_digest != app.child_spec_digest
''')

add('tests/test_oracle_package.py', r'''
from __future__ import annotations

from agintor.contracts import GoalSpec, default_langgraph_runtime_spec
from agintor.oracle.compiler import OracleCompiler
from agintor.oracle.package_io import finalize_oracle_package


def test_oracle_package_hash_stable():
    goal = GoalSpec(
        goal_id="g1",
        raw_prompt="Build a repo patch agent",
        normalized_goal="Build a repo patch agent",
        success_criteria=["Applies correct patches"],
    )
    spec = default_langgraph_runtime_spec(runtime_id="r1", name="Runtime")
    package = OracleCompiler().compile(goal, spec)
    assert package.package_hash
    assert finalize_oracle_package(package).package_hash == package.package_hash
''')

add('tests/test_oracle_public_projection.py', r'''
from __future__ import annotations

from agintor.contracts import GoalSpec, OracleTask, OracleTaskSet, default_langgraph_runtime_spec
from agintor.oracle.compiler import OracleCompiler
from agintor.oracle.projections import public_oracle_projection


def test_public_projection_strips_sealed_task_fields():
    goal = GoalSpec(goal_id="g1", raw_prompt="Return JSON", normalized_goal="Return JSON")
    spec = default_langgraph_runtime_spec(runtime_id="r1", name="Runtime")
    task = OracleTask(
        task_id="t1",
        public_prompt="visible",
        sealed_inputs={"private_expected": 1},
        claim_ids=[],
    )
    package = OracleCompiler(config=None).compile(goal, spec, task_sets=[OracleTaskSet(task_set_id="ts1", tasks=[task])])
    public = public_oracle_projection(package)
    rendered = str(public)
    assert "sealed_inputs" not in rendered
    assert "private_expected" not in rendered
''')

add('tests/test_oracle_qa.py', r'''
from __future__ import annotations

from agintor.contracts import GoalSpec, default_langgraph_runtime_spec
from agintor.oracle.compiler import OracleCompiler
from agintor.oracle.qa import run_oracle_qa


def test_oracle_qa_passes_compiled_package():
    goal = GoalSpec(goal_id="g1", raw_prompt="Validate schema artifact", normalized_goal="Validate schema artifact")
    spec = default_langgraph_runtime_spec(runtime_id="r1", name="Runtime")
    package = OracleCompiler().compile(goal, spec)
    assert run_oracle_qa(package).passed
''')

add('tests/test_langgraph_runtime_compiler.py', r'''
from __future__ import annotations

from agintor.contracts import default_langgraph_runtime_spec
from agintor.runtime.langgraph.compiler import LangGraphRuntimeCompiler


def test_langgraph_compiler_fallback_smoke_run():
    spec = default_langgraph_runtime_spec(runtime_id="r1", name="Runtime")
    state = LangGraphRuntimeCompiler().smoke_run(spec)
    assert "node.default" in state["completed_node_ids"]
''')

add('tests/test_tradingagents_adapter.py', r'''
from __future__ import annotations

from agintor.contracts import GoalSpec
from agintor.integrations.tradingagents.compiler import tradingagents_spec_from_goal


def test_tradingagents_spec_from_goal():
    goal = GoalSpec(goal_id="g1", raw_prompt="Build a trading agent", normalized_goal="Build a trading agent")
    spec = tradingagents_spec_from_goal(goal)
    assert spec.runtime_kind == "tradingagents_langgraph_v1"
    assert "market" in spec.selected_analysts
''')

for p,c in files.items():
    t=ROOT/p; t.parent.mkdir(parents=True, exist_ok=True); t.write_text(c, encoding='utf-8')
print('wrote', len(files), 'files')
