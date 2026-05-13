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
