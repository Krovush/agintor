from __future__ import annotations

from agintor.contracts import baseline_langgraph_runtime_spec
from agintor.factory.goals import build_goal_spec
from agintor.oracle.compiler import OracleCompiler
from agintor.oracle.qa import qa_oracle_package


def test_oracle_compiler_produces_frozen_package_with_hashes():
    goal = build_goal_spec("Build a runtime that answers JSON research tasks with citations.")
    spec = baseline_langgraph_runtime_spec(runtime_id="oracle.test")
    package = OracleCompiler().compile(goal, spec)
    assert package.frozen is True
    assert package.package_hash
    assert package.public_view_hash
    assert package.sealed_view_hash
    assert qa_oracle_package(package).passed is True
