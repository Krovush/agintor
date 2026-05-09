from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agintor.contracts import (
    CapabilityExchange,
    DomainEvidenceContract,
    EvidenceRecord,
    PairedComparison,
    RunResult,
    RuntimeBatchResponse,
    SuiteEvaluation,
)
from agintor.evaluation.benchmarks import build_tool_frontier_evidence_contract
from agintor.evaluation.challenge_generators import ToolWorkflowDifficulty, generate_tool_workflow_challenges
from agintor.evaluation.evaluator import RuntimeEvaluator
from agintor.evaluation.progress_oracle import ProgressOracle
from agintor.evaluation.scoring import ScoreCalculator


def _evaluator_for_helpers(tmp_path: Path) -> RuntimeEvaluator:
    evaluator = RuntimeEvaluator.__new__(RuntimeEvaluator)
    evaluator.evidence_ledger_path = tmp_path / "evidence.jsonl"
    evaluator.paired_comparison_ledger_path = tmp_path / "comparisons.jsonl"
    evaluator.promotion_ledger_path = tmp_path / "promotions.jsonl"
    return evaluator


def _run(runtime_hash: str, task_id: str, score: float, *, cost: float = 10.0, invalid: bool = False) -> RunResult:
    return RunResult(
        runtime_hash=runtime_hash,
        task_id=task_id,
        seed=0,
        artifact=score,
        verifier_score=score,
        cost=cost,
        latency=1.0,
        faults=0,
        hard_invalid=invalid,
    )


def test_stage4_invalid_child_never_passes_even_if_oracle_promotes_efficiency(tmp_path: Path) -> None:
    evaluator = _evaluator_for_helpers(tmp_path)
    scorer = ScoreCalculator(lambdas={"cost": 0.0, "latency": 0.0, "fault": 0.0})
    child = scorer.suite_score(
        "child",
        {"tool.frontier.0": "tool"},
        [_run("child", "tool.frontier.0", 0.0, cost=1.0, invalid=True)],
    )
    child = child.model_copy(update={"invalid": True})
    comparison = PairedComparison(
        comparison_id="cmp-efficiency",
        parent_runtime_hash="parent",
        child_runtime_hash="child",
        contract_id="contract",
        challenge_ids=["tool.frontier.0"],
        axis_deltas={
            "tool.frontier.0": {
                "estimate": 0.0,
                "lower": 0.0,
                "upper": 0.0,
                "evidence_count": 1,
                "promotion_kind": "capability",
                "source": "frontier",
            }
        },
        efficiency_deltas={"runtime_efficiency": {"estimate": 0.5, "lower": 0.5, "upper": 0.5}},
        health_floor_status={"generator": "pass", "answer": "pass", "validator": "pass", "statistics": "pass", "leakage": "pass"},
        leakage_status="clean",
    )
    contract = DomainEvidenceContract(
        contract_id="contract",
        domain_kind="generated_tool_workflow",
        version="v1",
        scope={"domain": "tool"},
        challenge_distribution={"minimum_frontier_tasks": 1},
        answer_mechanism={"type": "deterministic"},
        quality_axes=[{"axis_id": "tool.frontier.0", "promotion_kind": "capability"}],
        efficiency_axes=[{"axis_id": "runtime_efficiency", "promotion_kind": "efficiency"}],
    )
    decision = ProgressOracle().decide(contract=contract, comparison=comparison)

    result = evaluator._stage4_result_from_decision(decision, epsilon_full=0.0, child_eval=child)

    assert decision.decision_type == "efficiency"
    assert result.passed is False


def test_invalid_child_evaluation_rejects_before_promoting_ledger_updates(tmp_path: Path) -> None:
    evaluator = _evaluator_for_helpers(tmp_path)
    scorer = ScoreCalculator(lambdas={"cost": 0.0, "latency": 0.0, "fault": 0.0})
    parent = scorer.suite_score("parent", {"tool.frontier.0": "tool"}, [_run("parent", "tool.frontier.0", 0.0, cost=10.0)])
    child = scorer.suite_score(
        "child",
        {"tool.frontier.0": "tool"},
        [_run("child", "tool.frontier.0", 0.0, cost=1.0, invalid=True)],
    ).model_copy(update={"invalid": True})

    decision = ProgressOracle().decide_evaluations(parent, child)
    decision = evaluator._write_stage4_ledgers(parent, child, decision)
    result = evaluator._stage4_result_from_decision(decision, epsilon_full=0.0, child_eval=child)
    promotion_row = json.loads(evaluator.promotion_ledger_path.read_text(encoding="utf-8").splitlines()[0])

    assert decision.decision_type == "reject"
    assert result.passed is False
    assert "efficiency_archive" not in promotion_row["allowed_optimizer_updates"]
    assert "invalid_child_evaluation" in promotion_row["reason_codes"]


def test_stage4_ledgers_write_typed_evidence_records_and_attach_refs(tmp_path: Path) -> None:
    evaluator = _evaluator_for_helpers(tmp_path)
    scorer = ScoreCalculator(lambdas={"cost": 0.0, "latency": 0.0, "fault": 0.0})
    family_map = {"tool.frontier.0": "tool", "tool.frontier.1": "tool"}
    parent = scorer.suite_score(
        "parent",
        family_map,
        [_run("parent", "tool.frontier.0", 0.0), _run("parent", "tool.frontier.1", 0.0)],
    )
    child = scorer.suite_score(
        "child",
        family_map,
        [_run("child", "tool.frontier.0", 1.0), _run("child", "tool.frontier.1", 1.0)],
    )
    decision = ProgressOracle().decide_evaluations(parent, child)

    decision = evaluator._write_stage4_ledgers(parent, child, decision)
    evidence_rows = [
        EvidenceRecord.model_validate(json.loads(line))
        for line in evaluator.evidence_ledger_path.read_text(encoding="utf-8").splitlines()
    ]
    comparison_row = json.loads(evaluator.paired_comparison_ledger_path.read_text(encoding="utf-8").splitlines()[0])
    promotion_row = json.loads(evaluator.promotion_ledger_path.read_text(encoding="utf-8").splitlines()[0])

    assert evidence_rows
    assert decision.evidence_refs == [row.record_id for row in evidence_rows]
    assert comparison_row["evidence_refs"] == decision.evidence_refs
    assert comparison_row["decision_ref"] == decision.decision_id
    assert comparison_row["leakage_status"] == "clean"
    assert comparison_row["health_floor_status"]["leakage"] == "pass"
    assert promotion_row["evidence_refs"] == decision.evidence_refs
    assert promotion_row["decision_id"] == decision.decision_id
    assert all(score.axis_id == row.challenge_id for row in evidence_rows for score in row.axis_scores)


def test_stage4_evidence_rows_use_semantic_contract_axis_ids(tmp_path: Path) -> None:
    evaluator = _evaluator_for_helpers(tmp_path)
    scorer = ScoreCalculator(lambdas={"cost": 0.0, "latency": 0.0, "fault": 0.0})
    family_map = {"tool.challenge.0": "tool", "tool.challenge.1": "tool"}
    metadata = {
        task_id: {"domain_kind": "generated_tool_workflow", "slice_tags": ["frontier"]}
        for task_id in family_map
    }
    parent = scorer.suite_score(
        "parent",
        family_map,
        [_run("parent", "tool.challenge.0", 0.0), _run("parent", "tool.challenge.1", 0.0)],
        task_metadata=metadata,
    )
    child = scorer.suite_score(
        "child",
        family_map,
        [_run("child", "tool.challenge.0", 1.0), _run("child", "tool.challenge.1", 1.0)],
        task_metadata=metadata,
    )
    contract = DomainEvidenceContract(
        contract_id="contract",
        domain_kind="generated_tool_workflow",
        version="v1",
        scope={"domain": "tool"},
        challenge_distribution={"minimum_frontier_tasks": 2},
        answer_mechanism={"type": "deterministic"},
        quality_axes=[
            {
                "axis_id": "expression_generalization",
                "promotion_kind": "capability",
                "metadata": {"domain_kind": "generated_tool_workflow", "slice_tags": ["frontier"]},
            }
        ],
        efficiency_axes=[{"axis_id": "runtime_efficiency", "promotion_kind": "efficiency"}],
        health_floors={"leakage": "pass"},
        leakage_policy={"status_required": True},
    )
    decision = ProgressOracle().decide_evaluations(
        parent,
        child,
        contract=contract,
        health_floor_status={"leakage": "pass"},
        leakage_status="clean",
    )

    evaluator._write_stage4_ledgers(parent, child, decision)
    evidence_rows = [
        EvidenceRecord.model_validate(json.loads(line))
        for line in evaluator.evidence_ledger_path.read_text(encoding="utf-8").splitlines()
    ]

    assert {score.axis_id for row in evidence_rows for score in row.axis_scores} == {"expression_generalization"}


def test_stage4_ledgers_keep_evidence_for_regressed_guardrail_axes(tmp_path: Path) -> None:
    evaluator = _evaluator_for_helpers(tmp_path)
    scorer = ScoreCalculator(lambdas={"cost": 0.0, "latency": 0.0, "fault": 0.0})
    family_map = {"tool.challenge.0": "tool", "demo.guardrail": "top"}
    metadata = {
        "tool.challenge.0": {"domain_kind": "generated_tool_workflow", "slice_tags": ["frontier"]},
        "demo.guardrail": {"slice_tags": ["guardrail"]},
    }
    parent = scorer.suite_score(
        "parent",
        family_map,
        [_run("parent", "tool.challenge.0", 0.0), _run("parent", "demo.guardrail", 1.0)],
        task_metadata=metadata,
    )
    child = scorer.suite_score(
        "child",
        family_map,
        [_run("child", "tool.challenge.0", 1.0), _run("child", "demo.guardrail", 0.0)],
        task_metadata=metadata,
    )
    contract = DomainEvidenceContract(
        contract_id="contract",
        domain_kind="generated_tool_workflow",
        version="v1",
        scope={"domain": "tool"},
        challenge_distribution={"minimum_frontier_tasks": 1},
        answer_mechanism={"type": "deterministic"},
        quality_axes=[
            {
                "axis_id": "expression_generalization",
                "promotion_kind": "capability",
                "metadata": {"domain_kind": "generated_tool_workflow", "slice_tags": ["frontier"]},
            },
            {
                "axis_id": "static_guardrail",
                "promotion_kind": "capability",
                "promotion_eligible": False,
                "protected_regression_tolerance": 0.01,
                "metadata": {"task_ids": ["demo.guardrail"]},
            },
        ],
        efficiency_axes=[],
    )
    decision = ProgressOracle().decide_evaluations(parent, child, contract=contract)

    assert decision.decision_type == "reject"
    assert "protected_axis_regression" in decision.reason_codes

    evaluator._write_stage4_ledgers(parent, child, decision)
    evidence_rows = [
        EvidenceRecord.model_validate(json.loads(line))
        for line in evaluator.evidence_ledger_path.read_text(encoding="utf-8").splitlines()
    ]

    assert {row.challenge_id for row in evidence_rows} == {"tool.challenge.0", "demo.guardrail"}


def test_private_expected_is_stripped_from_runtime_visible_task_and_used_for_rescore(tmp_path: Path) -> None:
    evaluator = _evaluator_for_helpers(tmp_path)
    task = generate_tool_workflow_challenges(
        partition="train",
        count=1,
        seed=1,
        difficulty=ToolWorkflowDifficulty(expression_depth=2, dependency_width=1, distractor_count=0),
    )[0]

    visible_task = evaluator._runtime_visible_task(task)

    assert visible_task.expected is None
    assert visible_task.private_expected is None
    assert visible_task.verifier_type == "none"
    assert "private_answer_ref" not in visible_task.metadata

    run = RunResult(
        runtime_hash="runtime",
        task_id=task.task_id,
        seed=0,
        artifact=task.private_expected,
        verifier_score=0.0,
        cost=1.0,
        latency=1.0,
        faults=0,
    )
    rescored = evaluator._rescore_private_results([run], [task])

    assert rescored[0].verifier_score == 1.0


def test_evaluate_runtime_passes_authoritative_private_tasks_to_host_batch(tmp_path: Path) -> None:
    task = generate_tool_workflow_challenges(
        partition="train",
        count=1,
        seed=11,
        difficulty=ToolWorkflowDifficulty(expression_depth=2, dependency_width=1, distractor_count=0),
    )[0]
    captured: dict[str, object] = {}
    evaluator = RuntimeEvaluator.__new__(RuntimeEvaluator)
    evaluator.suite = SimpleNamespace(all_tasks=lambda _partition: [task])
    evaluator.cache = {}
    evaluator.provider = object()
    evaluator.budget_overrides = {}
    evaluator.trace_context = None
    evaluator.predictors = SimpleNamespace(freeze=lambda: None, unfreeze=lambda: None)
    evaluator._effective_runtime_profile = lambda _runtime_dir: SimpleNamespace()
    evaluator._load_runtime = lambda _runtime_dir, runtime_profile=None: SimpleNamespace(runtime_hash="runtime")
    evaluator._score_calculator = lambda use_reference_scales=True: ScoreCalculator(lambdas={"cost": 0.0, "latency": 0.0, "fault": 0.0})

    class FakeRuntimeHost:
        def run_batch(self, runtime_dir, task_runs, **kwargs):
            captured["task_runs"] = task_runs
            authoritative_task = task_runs[0][0]
            return RuntimeBatchResponse(
                request_id="batch",
                capability_exchange=CapabilityExchange(runtime_contract_version="test"),
                run_results=[
                    RunResult(
                        runtime_hash="runtime",
                        task_id=authoritative_task.task_id,
                        seed=task_runs[0][1],
                        artifact=task.private_expected,
                        verifier_score=0.0,
                        cost=0.0,
                        latency=0.1,
                        faults=0,
                    )
                ],
                provider_usage={},
            )

    evaluator.runtime_host = FakeRuntimeHost()

    evaluation = evaluator.evaluate_runtime(
        "dummy-runtime",
        seeds=(0,),
        use_cache=False,
        tasks_override=[task],
        use_reference_scales=False,
    )
    task_runs = captured["task_runs"]

    assert task_runs[0][0].private_expected == task.private_expected
    assert task_runs[0][0].verifier_type == "number_exact"
    assert evaluation.run_results[0].verifier_score == 1.0


def test_private_metadata_only_projection_keeps_public_verifier(tmp_path: Path) -> None:
    evaluator = _evaluator_for_helpers(tmp_path)
    task = generate_tool_workflow_challenges(
        partition="train",
        count=1,
        seed=2,
        difficulty=ToolWorkflowDifficulty(expression_depth=2, dependency_width=1, distractor_count=0),
    )[0].model_copy(
        update={
            "expected": 42,
            "private_expected": None,
            "verifier_type": "number_exact",
            "verification_required": True,
            "metadata": {"private_hint": "sealed", "public_payload": {"ok": True}},
        }
    )

    visible = evaluator._runtime_visible_task(task)

    assert visible.expected == 42
    assert visible.verifier_type == "number_exact"
    assert visible.verification_required is True
    assert visible.metadata == {"public_payload": {"ok": True}}


def test_stage4_contract_attestation_uses_evaluated_sealed_frontier_tasks(tmp_path: Path) -> None:
    tasks = generate_tool_workflow_challenges(
        partition="train",
        count=2,
        seed=3,
        difficulty=ToolWorkflowDifficulty(expression_depth=2, dependency_width=1, distractor_count=0),
    )
    evaluator = _evaluator_for_helpers(tmp_path)
    evaluator.suite = SimpleNamespace(train=tasks)
    evaluator.evidence_contract = build_tool_frontier_evidence_contract(minimum_frontier_tasks=2)
    evaluator.progress_oracle = ProgressOracle()
    scorer = ScoreCalculator(lambdas={"cost": 0.0, "latency": 0.0, "fault": 0.0})
    family_map = {task.task_id: task.family for task in tasks}
    metadata = {task.task_id: dict(task.metadata) for task in tasks}
    parent = scorer.suite_score(
        "parent",
        family_map,
        [_run("parent", task.task_id, 0.0) for task in tasks],
        task_metadata=metadata,
    )
    child = scorer.suite_score(
        "child",
        family_map,
        [_run("child", task.task_id, 1.0) for task in tasks],
        task_metadata=metadata,
    )

    decision = evaluator._stage4_decision(parent, child)
    comparison = decision.progress_signal.pairwise_comparisons[0]

    assert decision.decision_type == "capability"
    assert comparison.health_floor_status == {
        "answer": "pass",
        "generator": "pass",
        "leakage": "pass",
        "statistics": "pass",
        "validator": "pass",
    }
    assert comparison.leakage_status == "clean"


def test_stage4_contract_attestation_rejects_missing_sealed_answers(tmp_path: Path) -> None:
    tasks = [
        task.model_copy(update={"private_expected": None})
        for task in generate_tool_workflow_challenges(
            partition="train",
            count=2,
            seed=4,
            difficulty=ToolWorkflowDifficulty(expression_depth=2, dependency_width=1, distractor_count=0),
        )
    ]
    evaluator = _evaluator_for_helpers(tmp_path)
    evaluator.suite = SimpleNamespace(train=tasks)
    evaluator.evidence_contract = build_tool_frontier_evidence_contract(minimum_frontier_tasks=2)
    evaluator.progress_oracle = ProgressOracle()
    scorer = ScoreCalculator(lambdas={"cost": 0.0, "latency": 0.0, "fault": 0.0})
    family_map = {task.task_id: task.family for task in tasks}
    metadata = {task.task_id: dict(task.metadata) for task in tasks}
    parent = scorer.suite_score(
        "parent",
        family_map,
        [_run("parent", task.task_id, 0.0) for task in tasks],
        task_metadata=metadata,
    )
    child = scorer.suite_score(
        "child",
        family_map,
        [_run("child", task.task_id, 1.0) for task in tasks],
        task_metadata=metadata,
    )

    decision = evaluator._stage4_decision(parent, child)
    comparison = decision.progress_signal.pairwise_comparisons[0]

    assert decision.decision_type == "abstain"
    assert decision.reason_codes == ["missing_health_floor_evidence"]
    assert comparison.health_floor_status["answer"] == "missing"


def test_stage4_contract_attestation_checks_generator_provenance_values(tmp_path: Path) -> None:
    tasks = [
        task.model_copy(update={"metadata": {**task.metadata, "generator_id": "wrong-generator"}})
        for task in generate_tool_workflow_challenges(
            partition="train",
            count=2,
            seed=5,
            difficulty=ToolWorkflowDifficulty(expression_depth=2, dependency_width=1, distractor_count=0),
        )
    ]
    evaluator = _evaluator_for_helpers(tmp_path)
    evaluator.suite = SimpleNamespace(train=tasks)
    evaluator.evidence_contract = build_tool_frontier_evidence_contract(minimum_frontier_tasks=2)
    evaluator.progress_oracle = ProgressOracle()
    scorer = ScoreCalculator(lambdas={"cost": 0.0, "latency": 0.0, "fault": 0.0})
    family_map = {task.task_id: task.family for task in tasks}
    metadata = {task.task_id: dict(task.metadata) for task in tasks}
    parent = scorer.suite_score(
        "parent",
        family_map,
        [_run("parent", task.task_id, 0.0) for task in tasks],
        task_metadata=metadata,
    )
    child = scorer.suite_score(
        "child",
        family_map,
        [_run("child", task.task_id, 1.0) for task in tasks],
        task_metadata=metadata,
    )

    decision = evaluator._stage4_decision(parent, child)
    comparison = decision.progress_signal.pairwise_comparisons[0]

    assert decision.decision_type == "abstain"
    assert comparison.health_floor_status["generator"] == "missing"
