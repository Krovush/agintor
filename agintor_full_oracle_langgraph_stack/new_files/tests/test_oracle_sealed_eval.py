from __future__ import annotations

from agintor.contracts import baseline_langgraph_runtime_spec, oracle_runtime_visible_tasks_by_partition, oracle_tasks_by_partition
from agintor.factory.goals import build_goal_spec
from agintor.oracle.compiler import OracleCompiler


def test_oracle_sealed_tasks_keep_private_expected_host_side_only():
    package = OracleCompiler().compile(build_goal_spec("Build a repo patch agent."), baseline_langgraph_runtime_spec(runtime_id="oracle.sealed"))
    sealed = oracle_tasks_by_partition(package, "train")
    visible = oracle_runtime_visible_tasks_by_partition(package, "train")
    assert sealed[0].private_expected is not None
    assert visible[0].private_expected is None
    assert visible[0].expected is None
