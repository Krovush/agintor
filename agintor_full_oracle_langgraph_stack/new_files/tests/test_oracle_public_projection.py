from __future__ import annotations

import json

from agintor.contracts import baseline_langgraph_runtime_spec, oracle_public_projection
from agintor.factory.goals import build_goal_spec
from agintor.oracle.compiler import OracleCompiler


def test_oracle_public_projection_strips_private_expected_and_private_metadata():
    goal = build_goal_spec("Create a stateful service agent with consent proof.")
    package = OracleCompiler().compile(goal, baseline_langgraph_runtime_spec(runtime_id="oracle.public"))
    public = oracle_public_projection(package)
    rendered = json.dumps(public, sort_keys=True)
    assert "private_expected" not in rendered
    assert "expected_digest" not in rendered
    assert "promotion_threshold" not in rendered
