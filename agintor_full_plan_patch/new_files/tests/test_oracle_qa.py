from __future__ import annotations

from agintor.contracts import GoalSpec, default_langgraph_runtime_spec
from agintor.oracle.compiler import OracleCompiler
from agintor.oracle.qa import run_oracle_qa


def test_oracle_qa_passes_compiled_package():
    goal = GoalSpec(goal_id="g1", raw_prompt="Validate schema artifact", normalized_goal="Validate schema artifact")
    spec = default_langgraph_runtime_spec(runtime_id="r1", name="Runtime")
    package = OracleCompiler().compile(goal, spec)
    assert run_oracle_qa(package).passed
