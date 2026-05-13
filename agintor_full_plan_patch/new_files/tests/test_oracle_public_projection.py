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
