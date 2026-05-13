from __future__ import annotations

from agintor.contracts import baseline_langgraph_runtime_spec
from agintor.factory.goals import build_goal_spec
from agintor.oracle.compiler import OracleCompiler
from agintor.evaluation.oracle_runner import OracleEvaluationRunner


def test_oracle_evaluation_runner_emits_validator_and_claim_results():
    package = OracleCompiler().compile(build_goal_spec("Return JSON answer."), baseline_langgraph_runtime_spec(runtime_id="oracle.runner"))
    validator_results, claim_results = OracleEvaluationRunner().evaluate_run(package, {"artifact": {"answer": "ok"}, "trace": []})
    assert validator_results
    assert claim_results
    assert {claim.claim_id for claim in package.claim_graph.claims} == {result.claim_id for result in claim_results}
