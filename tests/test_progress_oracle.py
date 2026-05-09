from __future__ import annotations

import pytest
from pydantic import ValidationError

from agintor.contracts import AxisDelta, DomainEvidenceContract, PairedComparison, RunResult, SuiteEvaluation
from agintor.evaluation.progress_oracle import ProgressOracle, ProgressOracleConfig
from agintor.evaluation.scoring import ScoreCalculator, mean_improvement


def _run(runtime_hash: str, task_id: str, score: float, *, cost: float = 10.0, seed: int = 0) -> RunResult:
    return RunResult(
        runtime_hash=runtime_hash,
        task_id=task_id,
        seed=seed,
        artifact={"answer": score},
        verifier_score=score,
        cost=cost,
        latency=1.0,
        faults=0,
    )


def _evaluation(runtime_hash: str, scores: list[float], *, cost: float = 10.0) -> SuiteEvaluation:
    runs = [_run(runtime_hash, f"tool.frontier.{index}", score, cost=cost) for index, score in enumerate(scores)]
    family_map = {run.task_id: "tool" for run in runs}
    return ScoreCalculator(lambdas={"cost": 0.0, "latency": 0.0, "fault": 0.0}).suite_score(runtime_hash, family_map, runs)


def _contract(*, minimum_frontier_tasks: int = 32) -> DomainEvidenceContract:
    return DomainEvidenceContract(
        contract_id="generated_tool_workflow_v1",
        domain_kind="generated_tool_workflow",
        version="v1",
        scope={"domain": "tool", "allowed_claim_language": ["generated tool workflow only"]},
        challenge_distribution={"minimum_frontier_tasks": minimum_frontier_tasks},
        answer_mechanism={"type": "deterministic_interpreter", "authority": "A4"},
        quality_axes=[
            {
                "axis_id": "expression_generalization",
                "promotion_kind": "capability",
                "comparator_type": "hidden_challenge",
                "minimum_authority": "A4",
                "epsilon": 0.03,
                "protected_regression_tolerance": 0.01,
            }
        ],
        efficiency_axes=[{"axis_id": "token_cost", "promotion_kind": "efficiency"}],
        health_floors={"generator": "pass", "answer": "pass", "validator": "pass", "statistics": "pass", "leakage": "pass"},
        statistical_rule={"type": "fixed_confirmatory", "minimum_pairs": minimum_frontier_tasks, "alpha": 0.05},
    )


def test_saturated_exact_suite_promotes_efficiency_only() -> None:
    parent = _evaluation("parent", [1.0], cost=10.0)
    child = _evaluation("child", [1.0], cost=5.0)

    signal = ProgressOracle().compare(parent, child)

    assert signal.decision == "efficiency"
    assert signal.quality_delta_lower == 0.0
    assert "efficiency_lcb_cleared" in signal.reason_codes


def test_hidden_frontier_quality_win_promotes_capability() -> None:
    comparison = PairedComparison(
        comparison_id="cmp-tool-parent-child",
        parent_runtime_hash="parent-runtime",
        child_runtime_hash="child-runtime",
        contract_id="generated_tool_workflow_v1",
        challenge_ids=[f"challenge-{index:03d}" for index in range(64)],
        axis_deltas={
            "expression_generalization": {
                "estimate": 0.18,
                "lower": 0.06,
                "upper": 0.28,
                "evidence_count": 64,
                "promotion_kind": "capability",
                "authority_level": "A4",
                "source": "hidden_frontier",
            }
        },
        efficiency_deltas={"token_cost": {"estimate": 0.0, "lower": -0.01, "upper": 0.01}},
        health_floor_status={"generator": "pass", "answer": "pass", "validator": "pass", "statistics": "pass", "leakage": "pass"},
        leakage_status="clean",
    )
    signal = ProgressOracle(ProgressOracleConfig(capability_epsilon=0.03)).decide(
        contract=_contract(minimum_frontier_tasks=64),
        comparison=comparison,
    ).progress_signal

    assert signal.decision == "capability"
    assert signal.quality_delta_lower > 0.0
    assert "frontier_quality_win" in signal.reason_codes


def test_implicit_suite_quality_win_promotes_capability() -> None:
    runs_parent = [_run("parent", f"tool.frontier.{index}", 0.0, seed=seed) for index in range(4) for seed in range(2)]
    runs_child = [_run("child", f"tool.frontier.{index}", 1.0, seed=seed) for index in range(4) for seed in range(2)]
    family_map = {run.task_id: "tool" for run in runs_parent}
    scorer = ScoreCalculator(lambdas={"cost": 0.0, "latency": 0.0, "fault": 0.0})

    signal = ProgressOracle(ProgressOracleConfig(capability_epsilon=0.03)).compare(
        scorer.suite_score("parent", family_map, runs_parent),
        scorer.suite_score("child", family_map, runs_child),
    )

    assert signal.decision == "capability"
    assert signal.quality_delta_lower > 0.0
    assert "suite_quality_win" in signal.reason_codes


def test_default_static_suite_quality_win_is_still_promotable() -> None:
    runs_parent = [_run("parent", f"demo.{index}", 0.0, seed=seed) for index in range(4) for seed in range(2)]
    runs_child = [_run("child", f"demo.{index}", 1.0, seed=seed) for index in range(4) for seed in range(2)]
    family_map = {run.task_id: "top" for run in runs_parent}
    scorer = ScoreCalculator(lambdas={"cost": 0.0, "latency": 0.0, "fault": 0.0})

    signal = ProgressOracle().compare(
        scorer.suite_score("parent", family_map, runs_parent),
        scorer.suite_score("child", family_map, runs_child),
    )

    assert signal.decision == "capability"
    assert "suite_quality_win" in signal.reason_codes


def test_implicit_generated_frontier_win_stays_subskill_without_contract() -> None:
    runs_parent = [_run("parent", f"tool.challenge.{index}", 0.0, seed=seed) for index in range(2) for seed in range(2)]
    runs_child = [_run("child", f"tool.challenge.{index}", 1.0, seed=seed) for index in range(2) for seed in range(2)]
    family_map = {run.task_id: "tool" for run in runs_parent}
    metadata = {
        run.task_id: {"domain_kind": "generated_tool_workflow", "slice_tags": ["tool", "generated", "frontier"]}
        for run in runs_parent
    }
    scorer = ScoreCalculator(lambdas={"cost": 0.0, "latency": 0.0, "fault": 0.0})

    signal = ProgressOracle().compare(
        scorer.suite_score("parent", family_map, runs_parent, task_metadata=metadata),
        scorer.suite_score("child", family_map, runs_child, task_metadata=metadata),
    )

    assert signal.decision == "subskill"
    assert "suite_quality_win" in signal.reason_codes


def test_explicit_contract_groups_task_evidence_under_semantic_axis() -> None:
    runs_parent = [_run("parent", f"tool.challenge.{index}", 0.0, seed=seed) for index in range(2) for seed in range(2)]
    runs_child = [_run("child", f"tool.challenge.{index}", 1.0, seed=seed) for index in range(2) for seed in range(2)]
    family_map = {run.task_id: "tool" for run in runs_parent}
    metadata = {
        run.task_id: {"domain_kind": "generated_tool_workflow", "slice_tags": ["tool", "generated", "frontier"]}
        for run in runs_parent
    }
    scorer = ScoreCalculator(lambdas={"cost": 0.0, "latency": 0.0, "fault": 0.0})
    contract = _contract(minimum_frontier_tasks=2).model_copy(
        update={
            "quality_axes": [
                {
                    "axis_id": "expression_generalization",
                    "promotion_kind": "capability",
                    "epsilon": 0.03,
                    "protected_regression_tolerance": 0.01,
                    "metadata": {"domain_kind": "generated_tool_workflow", "slice_tags": ["frontier"]},
                }
            ]
        }
    )

    decision = ProgressOracle().decide_evaluations(
        scorer.suite_score("parent", family_map, runs_parent, task_metadata=metadata),
        scorer.suite_score("child", family_map, runs_child, task_metadata=metadata),
        contract=contract,
        health_floor_status={"generator": "pass", "answer": "pass", "validator": "pass", "statistics": "pass", "leakage": "pass"},
        leakage_status="clean",
    )

    comparison = decision.progress_signal.pairwise_comparisons[0]
    assert decision.decision_type == "capability"
    assert list(comparison.axis_deltas) == ["expression_generalization"]
    assert comparison.challenge_ids == ["tool.challenge.0", "tool.challenge.1"]
    assert comparison.axis_task_ids == {"expression_generalization": ["tool.challenge.0", "tool.challenge.1"]}


def test_explicit_contract_does_not_auto_attest_health_and_leakage() -> None:
    runs_parent = [_run("parent", "tool.challenge.0", 0.0, seed=0)]
    runs_child = [_run("child", "tool.challenge.0", 1.0, seed=0)]
    family_map = {"tool.challenge.0": "tool"}
    metadata = {"tool.challenge.0": {"domain_kind": "generated_tool_workflow", "slice_tags": ["frontier"]}}
    scorer = ScoreCalculator(lambdas={"cost": 0.0, "latency": 0.0, "fault": 0.0})
    contract = _contract(minimum_frontier_tasks=1).model_copy(
        update={
            "quality_axes": [
                {
                    "axis_id": "expression_generalization",
                    "promotion_kind": "capability",
                    "metadata": {"domain_kind": "generated_tool_workflow", "slice_tags": ["frontier"]},
                }
            ],
            "leakage_policy": {"status_required": True},
        }
    )

    decision = ProgressOracle().decide_evaluations(
        scorer.suite_score("parent", family_map, runs_parent, task_metadata=metadata),
        scorer.suite_score("child", family_map, runs_child, task_metadata=metadata),
        contract=contract,
    )

    assert decision.decision_type == "abstain"
    assert "missing_health_floor_evidence" in decision.reason_codes


def test_explicit_capability_lcb_ignores_unmatched_subskill_axes() -> None:
    comparison = PairedComparison(
        comparison_id="cmp-mixed",
        parent_runtime_hash="parent",
        child_runtime_hash="child",
        contract_id="generated_tool_workflow_v1",
        challenge_ids=["frontier"],
        axis_deltas={
            "expression_generalization": {
                "estimate": 0.04,
                "lower": 0.02,
                "upper": 0.06,
                "evidence_count": 4,
                "promotion_kind": "capability",
                "authority_level": "A4",
                "source": "hidden_frontier",
            },
            "demo": {
                "estimate": 1.0,
                "lower": 1.0,
                "upper": 1.0,
                "evidence_count": 40,
                "promotion_kind": "subskill",
                "authority_level": "A4",
                "source": "static_exact",
            },
        },
        efficiency_deltas={"token_cost": {"estimate": 0.0, "lower": 0.0, "upper": 0.0}},
        health_floor_status={"generator": "pass", "answer": "pass", "validator": "pass", "statistics": "pass", "leakage": "pass"},
        leakage_status="clean",
    )

    decision = ProgressOracle().decide(contract=_contract(minimum_frontier_tasks=1), comparison=comparison)

    assert decision.decision_type == "no_progress"
    assert "capability_lcb_not_cleared" in decision.reason_codes


def test_quality_axis_source_uses_task_metadata_not_task_id_substrings() -> None:
    runs_parent = [_run("parent", "tool.challenge.metadata_only", 0.0, seed=0)]
    runs_child = [_run("child", "tool.challenge.metadata_only", 1.0, seed=0)]
    family_map = {"tool.challenge.metadata_only": "tool"}
    metadata = {
        "tool.challenge.metadata_only": {
            "domain_kind": "generated_tool_workflow",
            "slice_tags": ["tool", "generated", "frontier"],
        }
    }
    scorer = ScoreCalculator(lambdas={"cost": 0.0, "latency": 0.0, "fault": 0.0})

    comparison = ProgressOracle().compare_evaluations(
        scorer.suite_score("parent", family_map, runs_parent, task_metadata=metadata),
        scorer.suite_score("child", family_map, runs_child, task_metadata=metadata),
    )

    assert comparison.axis_deltas["tool.challenge.metadata_only"].source == "frontier"


def test_cheaper_but_worse_is_rejected_not_capability() -> None:
    parent_scores = [1.0] * 52 + [0.0] * 12
    child_scores = [1.0] * 45 + [0.0] * 19

    signal = ProgressOracle().compare(
        _evaluation("parent", parent_scores, cost=10.0),
        _evaluation("child", child_scores, cost=5.0),
    )

    assert signal.decision == "reject"
    assert "protected_axis_regression" in signal.reason_codes


def test_protected_axis_regression_rejects_before_aggregate_win() -> None:
    axis_deltas = {
        f"axis-{index}": AxisDelta(
            axis_id=f"axis-{index}",
            estimate=0.2,
            lower=0.2,
            upper=0.2,
            evidence_count=1,
            promotion_kind="capability",
            source="hidden_frontier",
        )
        for index in range(10)
    }
    axis_deltas["regressed"] = AxisDelta(
        axis_id="regressed",
        estimate=-0.05,
        lower=-0.05,
        upper=-0.05,
        evidence_count=1,
        promotion_kind="capability",
        source="hidden_frontier",
    )
    comparison = PairedComparison(
        comparison_id="cmp-mixed",
        parent_runtime_hash="parent",
        child_runtime_hash="child",
        contract_id="generated_tool_workflow_v1",
        challenge_ids=list(axis_deltas),
        axis_deltas=axis_deltas,
        efficiency_deltas={"token_cost": {"estimate": 0.0, "lower": 0.0, "upper": 0.0}},
        health_floor_status={"generator": "pass", "answer": "pass", "validator": "pass", "statistics": "pass", "leakage": "pass"},
        leakage_status="clean",
    )

    decision = ProgressOracle().decide(contract=_contract(minimum_frontier_tasks=1), comparison=comparison)

    assert decision.decision_type == "reject"
    assert "regressed" in decision.progress_signal.regressed_axes
    assert "protected_axis_regression" in decision.reason_codes


def test_protected_axis_regression_uses_lower_bound_not_point_estimate() -> None:
    comparison = PairedComparison(
        comparison_id="cmp-uncertain-regression",
        parent_runtime_hash="parent",
        child_runtime_hash="child",
        contract_id="generated_tool_workflow_v1",
        challenge_ids=["expression_generalization", "other"],
        axis_deltas={
            "expression_generalization": {
                "estimate": 0.02,
                "lower": -0.05,
                "upper": 0.10,
                "evidence_count": 8,
                "promotion_kind": "capability",
                "authority_level": "A4",
                "source": "hidden_frontier",
            },
            "other": {
                "estimate": 0.50,
                "lower": 0.50,
                "upper": 0.50,
                "evidence_count": 8,
                "promotion_kind": "capability",
                "authority_level": "A4",
                "source": "hidden_frontier",
            },
        },
        protected_axis_bounds={"expression_generalization": -0.05, "other": 0.50},
        efficiency_deltas={"token_cost": {"estimate": 0.0, "lower": 0.0, "upper": 0.0}},
        health_floor_status={"generator": "pass", "answer": "pass", "validator": "pass", "statistics": "pass", "leakage": "pass"},
        leakage_status="clean",
    )

    decision = ProgressOracle().decide(contract=_contract(minimum_frontier_tasks=1), comparison=comparison)

    assert decision.decision_type == "reject"
    assert decision.progress_signal.regressed_axes == ["expression_generalization"]


def test_quality_aggregation_preserves_axis_lower_bounds() -> None:
    comparison = PairedComparison(
        comparison_id="cmp-single",
        parent_runtime_hash="parent",
        child_runtime_hash="child",
        contract_id="generated_tool_workflow_v1",
        challenge_ids=["challenge-001"],
        axis_deltas={
            "expression_generalization": {
                "estimate": 0.10,
                "lower": -0.90,
                "upper": 1.0,
                "evidence_count": 1,
                "promotion_kind": "capability",
                "authority_level": "A4",
                "source": "hidden_frontier",
            }
        },
        efficiency_deltas={"token_cost": {"estimate": 0.0, "lower": 0.0, "upper": 0.0}},
        health_floor_status={"generator": "pass", "answer": "pass", "validator": "pass", "statistics": "pass", "leakage": "pass"},
        leakage_status="clean",
    )

    decision = ProgressOracle().decide(contract=_contract(minimum_frontier_tasks=1), comparison=comparison)

    assert decision.decision_type == "reject"
    assert decision.quality_delta_lower == -0.90


def test_quality_aggregation_weights_axes_by_evidence_count() -> None:
    comparison = PairedComparison(
        comparison_id="cmp-weighted",
        parent_runtime_hash="parent",
        child_runtime_hash="child",
        contract_id="generated_tool_workflow_v1",
        challenge_ids=["small", "large"],
        axis_deltas={
            "small": {
                "estimate": 0.0,
                "lower": 0.0,
                "upper": 0.0,
                "evidence_count": 1,
                "promotion_kind": "capability",
                "authority_level": "A4",
                "source": "hidden_frontier",
            },
            "large": {
                "estimate": 0.20,
                "lower": 0.20,
                "upper": 0.20,
                "evidence_count": 9,
                "promotion_kind": "capability",
                "authority_level": "A4",
                "source": "hidden_frontier",
            },
        },
        efficiency_deltas={"token_cost": {"estimate": 0.0, "lower": 0.0, "upper": 0.0}},
        health_floor_status={"generator": "pass", "answer": "pass", "validator": "pass", "statistics": "pass", "leakage": "pass"},
        leakage_status="clean",
    )

    decision = ProgressOracle().decide(contract=_contract(minimum_frontier_tasks=1), comparison=comparison)

    assert decision.quality_delta_lower == pytest.approx(0.18)


def test_missing_contract_abstains_from_apparent_quality_win() -> None:
    comparison = PairedComparison(
        comparison_id="cmp-tool-parent-child",
        parent_runtime_hash="parent-runtime",
        child_runtime_hash="child-runtime",
        contract_id="generated_tool_workflow_v1",
        challenge_ids=[f"challenge-{index:03d}" for index in range(64)],
        axis_deltas={
            "expression_generalization": {
                "estimate": 0.18,
                "lower": 0.06,
                "upper": 0.28,
                "evidence_count": 64,
                "promotion_kind": "capability",
                "authority_level": "A4",
                "source": "hidden_frontier",
            }
        },
        efficiency_deltas={"token_cost": {"estimate": 0.0, "lower": -0.01, "upper": 0.01}},
        health_floor_status={"generator": "pass", "answer": "pass", "validator": "pass", "statistics": "pass", "leakage": "pass"},
        leakage_status="clean",
    )

    decision = ProgressOracle().decide(contract=None, comparison=comparison)

    assert decision.decision_type == "abstain"
    assert "missing_domain_evidence_contract" in decision.reason_codes
    assert "capability_archive" in decision.forbidden_optimizer_updates


def test_missing_required_health_or_leakage_evidence_abstains() -> None:
    comparison = PairedComparison(
        comparison_id="cmp-missing-health",
        parent_runtime_hash="parent-runtime",
        child_runtime_hash="child-runtime",
        contract_id="generated_tool_workflow_v1",
        challenge_ids=["challenge-001", "challenge-002"],
        axis_deltas={
            "expression_generalization": {
                "estimate": 0.30,
                "lower": 0.10,
                "upper": 0.45,
                "evidence_count": 2,
                "promotion_kind": "capability",
                "authority_level": "A4",
                "source": "hidden_frontier",
            }
        },
        efficiency_deltas={"token_cost": {"estimate": 0.0, "lower": 0.0, "upper": 0.0}},
        leakage_status="unknown",
    )
    contract = _contract(minimum_frontier_tasks=2).model_copy(update={"leakage_policy": {"status_required": True}})

    decision = ProgressOracle().decide(contract=contract, comparison=comparison)

    assert decision.decision_type == "abstain"
    assert "missing_health_floor_evidence" in decision.reason_codes


def test_missing_required_leakage_evidence_abstains_after_health_passes() -> None:
    comparison = PairedComparison(
        comparison_id="cmp-missing-leakage",
        parent_runtime_hash="parent-runtime",
        child_runtime_hash="child-runtime",
        contract_id="generated_tool_workflow_v1",
        challenge_ids=["challenge-001", "challenge-002"],
        axis_deltas={
            "expression_generalization": {
                "estimate": 0.30,
                "lower": 0.10,
                "upper": 0.45,
                "evidence_count": 2,
                "promotion_kind": "capability",
                "authority_level": "A4",
                "source": "hidden_frontier",
            }
        },
        efficiency_deltas={"token_cost": {"estimate": 0.0, "lower": 0.0, "upper": 0.0}},
        health_floor_status={"generator": "pass", "answer": "pass", "validator": "pass", "statistics": "pass", "leakage": "pass"},
        leakage_status="unknown",
    )
    contract = _contract(minimum_frontier_tasks=2).model_copy(update={"leakage_policy": {"status_required": True}})

    decision = ProgressOracle().decide(contract=contract, comparison=comparison)

    assert decision.decision_type == "abstain"
    assert "missing_leakage_evidence" in decision.reason_codes


def test_detected_leakage_quarantines_after_health_passes() -> None:
    comparison = PairedComparison(
        comparison_id="cmp-leaked",
        parent_runtime_hash="parent-runtime",
        child_runtime_hash="child-runtime",
        contract_id="generated_tool_workflow_v1",
        challenge_ids=["challenge-001", "challenge-002"],
        axis_deltas={
            "expression_generalization": {
                "estimate": 0.30,
                "lower": 0.10,
                "upper": 0.45,
                "evidence_count": 2,
                "promotion_kind": "capability",
                "authority_level": "A4",
                "source": "hidden_frontier",
            }
        },
        efficiency_deltas={"token_cost": {"estimate": 0.0, "lower": 0.0, "upper": 0.0}},
        health_floor_status={"generator": "pass", "answer": "pass", "validator": "pass", "statistics": "pass", "leakage": "pass"},
        leakage_status="leaked",
    )

    decision = ProgressOracle().decide(contract=_contract(minimum_frontier_tasks=2), comparison=comparison)

    assert decision.decision_type == "quarantine"
    assert "leakage_detected" in decision.reason_codes


def test_insufficient_evidence_abstains_from_capability() -> None:
    comparison = PairedComparison(
        comparison_id="cmp-tool-parent-child",
        parent_runtime_hash="parent-runtime",
        child_runtime_hash="child-runtime",
        contract_id="generated_tool_workflow_v1",
        challenge_ids=[f"challenge-{index:03d}" for index in range(8)],
        axis_deltas={
            "expression_generalization": {
                "estimate": 0.30,
                "lower": 0.10,
                "upper": 0.45,
                "evidence_count": 8,
                "promotion_kind": "capability",
                "authority_level": "A4",
                "source": "hidden_frontier",
            }
        },
        efficiency_deltas={"token_cost": {"estimate": 0.0, "lower": -0.01, "upper": 0.01}},
        health_floor_status={"generator": "pass", "answer": "pass", "validator": "pass", "statistics": "pass", "leakage": "pass"},
        leakage_status="clean",
    )

    decision = ProgressOracle().decide(contract=_contract(minimum_frontier_tasks=32), comparison=comparison)

    assert decision.decision_type == "abstain"
    assert "insufficient_evidence" in decision.reason_codes


def test_minimum_frontier_tasks_counts_challenges_not_seed_replays() -> None:
    comparison = PairedComparison(
        comparison_id="cmp-replays",
        parent_runtime_hash="parent-runtime",
        child_runtime_hash="child-runtime",
        contract_id="generated_tool_workflow_v1",
        challenge_ids=["one-challenge"],
        axis_deltas={
            "expression_generalization": {
                "estimate": 0.30,
                "lower": 0.10,
                "upper": 0.45,
                "evidence_count": 64,
                "promotion_kind": "capability",
                "authority_level": "A4",
                "source": "hidden_frontier",
            }
        },
        efficiency_deltas={"token_cost": {"estimate": 0.0, "lower": -0.01, "upper": 0.01}},
        health_floor_status={"generator": "pass", "answer": "pass", "validator": "pass", "statistics": "pass", "leakage": "pass"},
        leakage_status="clean",
    )

    decision = ProgressOracle().decide(contract=_contract(minimum_frontier_tasks=32), comparison=comparison)

    assert decision.decision_type == "abstain"
    assert "insufficient_evidence" in decision.reason_codes


def test_empty_efficiency_deltas_do_not_crash_quality_decisions() -> None:
    comparison = PairedComparison(
        comparison_id="cmp-no-efficiency",
        parent_runtime_hash="parent-runtime",
        child_runtime_hash="child-runtime",
        contract_id="generated_tool_workflow_v1",
        challenge_ids=["challenge-001", "challenge-002"],
        axis_deltas={
            "expression_generalization": {
                "estimate": 0.30,
                "lower": 0.10,
                "upper": 0.45,
                "evidence_count": 2,
                "promotion_kind": "capability",
                "authority_level": "A4",
                "source": "hidden_frontier",
            }
        },
        efficiency_deltas={},
        health_floor_status={"generator": "pass", "answer": "pass", "validator": "pass", "statistics": "pass", "leakage": "pass"},
        leakage_status="clean",
    )

    decision = ProgressOracle().decide(contract=_contract(minimum_frontier_tasks=2), comparison=comparison)

    assert decision.decision_type == "capability"


def test_explicit_contract_without_efficiency_axes_cannot_promote_cost_only_win() -> None:
    parent = _evaluation("parent", [1.0, 1.0], cost=10.0)
    child = _evaluation("child", [1.0, 1.0], cost=1.0)
    metadata = {
        task_id: {"domain_kind": "generated_tool_workflow", "slice_tags": ["frontier"]}
        for task_id in parent.task_scores
    }
    parent = parent.model_copy(update={"task_metadata": metadata})
    child = child.model_copy(update={"task_metadata": metadata})
    contract = _contract(minimum_frontier_tasks=1).model_copy(update={"efficiency_axes": []})

    decision = ProgressOracle().decide_evaluations(
        parent,
        child,
        contract=contract,
        health_floor_status={"generator": "pass", "answer": "pass", "validator": "pass", "statistics": "pass", "leakage": "pass"},
        leakage_status="clean",
    )
    comparison = decision.progress_signal.pairwise_comparisons[0]

    assert comparison.efficiency_deltas == {}
    assert decision.decision_type != "efficiency"


def test_unsupported_quality_comparator_abstains_instead_of_promoting() -> None:
    comparison = PairedComparison(
        comparison_id="cmp-unsupported-comparator",
        parent_runtime_hash="parent",
        child_runtime_hash="child",
        contract_id="generated_tool_workflow_v1",
        challenge_ids=["challenge-001", "challenge-002"],
        axis_deltas={
            "expression_generalization": {
                "estimate": 0.30,
                "lower": 0.20,
                "upper": 0.40,
                "evidence_count": 2,
                "promotion_kind": "capability",
                "authority_level": "A4",
                "source": "hidden_frontier",
            }
        },
        efficiency_deltas={},
        health_floor_status={"generator": "pass", "answer": "pass", "validator": "pass", "statistics": "pass", "leakage": "pass"},
        leakage_status="clean",
    )
    contract = _contract(minimum_frontier_tasks=1).model_copy(
        update={
            "quality_axes": [
                {
                    "axis_id": "expression_generalization",
                    "promotion_kind": "capability",
                    "comparator_type": "pairwise_preference",
                }
            ]
        }
    )

    decision = ProgressOracle().decide(contract=contract, comparison=comparison)

    assert decision.decision_type == "abstain"
    assert "unsupported_quality_comparator" in decision.reason_codes
    assert "unsupported_comparator:pairwise_preference" in decision.reason_codes


def test_efficiency_uses_contract_declared_metric_axis() -> None:
    parent_run = _run("parent", "tool.challenge.0", 1.0, cost=10.0).model_copy(update={"latency": 10.0})
    child_run = _run("child", "tool.challenge.0", 1.0, cost=10.0).model_copy(update={"latency": 5.0})
    family_map = {"tool.challenge.0": "tool"}
    metadata = {"tool.challenge.0": {"domain_kind": "generated_tool_workflow", "slice_tags": ["frontier"]}}
    scorer = ScoreCalculator(lambdas={"cost": 0.0, "latency": 0.0, "fault": 0.0})
    contract = _contract(minimum_frontier_tasks=1).model_copy(
        update={
            "efficiency_axes": [
                {
                    "axis_id": "latency_efficiency",
                    "promotion_kind": "efficiency",
                    "metric": "latency",
                    "epsilon": 0.01,
                }
            ]
        }
    )

    decision = ProgressOracle().decide_evaluations(
        scorer.suite_score("parent", family_map, [parent_run], task_metadata=metadata),
        scorer.suite_score("child", family_map, [child_run], task_metadata=metadata),
        contract=contract,
        health_floor_status={"generator": "pass", "answer": "pass", "validator": "pass", "statistics": "pass", "leakage": "pass"},
        leakage_status="clean",
    )
    comparison = decision.progress_signal.pairwise_comparisons[0]

    assert list(comparison.efficiency_deltas) == ["latency_efficiency"]
    assert comparison.efficiency_deltas["latency_efficiency"].estimate == pytest.approx(0.5)
    assert decision.decision_type == "efficiency"
    assert decision.progress_signal.efficiency_signal is not None
    assert decision.progress_signal.efficiency_signal.axis_ids == ["latency_efficiency"]


def test_unsupported_efficiency_metric_does_not_fall_back_to_generic_axis() -> None:
    parent = _evaluation("parent", [1.0], cost=10.0)
    child = _evaluation("child", [1.0], cost=10.0)
    metadata = {
        task_id: {"domain_kind": "generated_tool_workflow", "slice_tags": ["frontier"]}
        for task_id in parent.task_scores
    }
    parent = parent.model_copy(update={"task_metadata": metadata})
    child = child.model_copy(update={"task_metadata": metadata})
    contract = _contract(minimum_frontier_tasks=1).model_copy(
        update={
            "efficiency_axes": [
                {
                    "axis_id": "tool_call_efficiency",
                    "promotion_kind": "efficiency",
                    "metric": "tool_calls",
                }
            ]
        }
    )

    decision = ProgressOracle().decide_evaluations(
        parent,
        child,
        contract=contract,
        health_floor_status={"generator": "pass", "answer": "pass", "validator": "pass", "statistics": "pass", "leakage": "pass"},
        leakage_status="clean",
    )
    comparison = decision.progress_signal.pairwise_comparisons[0]

    assert comparison.efficiency_deltas == {}
    assert "runtime_efficiency" not in comparison.efficiency_deltas
    assert decision.decision_type != "efficiency"


def test_non_promotable_quality_axis_can_protect_but_not_promote() -> None:
    comparison = PairedComparison(
        comparison_id="cmp-diagnostic-axis",
        parent_runtime_hash="parent",
        child_runtime_hash="child",
        contract_id="generated_tool_workflow_v1",
        challenge_ids=["challenge-001"],
        axis_deltas={
            "expression_generalization": {
                "estimate": 0.20,
                "lower": 0.20,
                "upper": 0.20,
                "evidence_count": 1,
                "promotion_kind": "capability",
                "authority_level": "A4",
                "source": "hidden_frontier",
            }
        },
        efficiency_deltas={},
        health_floor_status={"generator": "pass", "answer": "pass", "validator": "pass", "statistics": "pass", "leakage": "pass"},
        leakage_status="clean",
    )
    contract = _contract(minimum_frontier_tasks=1).model_copy(
        update={
            "quality_axes": [
                {
                    "axis_id": "expression_generalization",
                    "promotion_kind": "capability",
                    "promotion_eligible": False,
                    "protected_regression_tolerance": 0.01,
                }
            ],
            "efficiency_axes": [],
        }
    )

    decision = ProgressOracle().decide(contract=contract, comparison=comparison)

    assert decision.decision_type == "no_progress"
    assert decision.progress_signal.improved_axes == []


def test_release_quality_axis_kind_is_rejected_at_contract_boundary() -> None:
    with pytest.raises(ValidationError):
        DomainEvidenceContract(
            contract_id="contract",
            domain_kind="generated_tool_workflow",
            version="v1",
            scope={"domain": "tool"},
            challenge_distribution={},
            answer_mechanism={"type": "deterministic"},
            quality_axes=[{"axis_id": "release", "promotion_kind": "release_quality"}],
        )


def test_singleton_mean_improvement_preserves_neutral_non_regression() -> None:
    delta, se, lcb = mean_improvement([1.0], [1.0])

    assert delta == 0.0
    assert se == 0.0
    assert lcb == 0.0


def test_static_suite_saturation_requests_frontier_expansion_without_efficiency() -> None:
    comparison = PairedComparison(
        comparison_id="cmp-static-parent-child",
        parent_runtime_hash="parent-runtime",
        child_runtime_hash="child-runtime",
        contract_id="generated_tool_workflow_v1",
        challenge_ids=["static-tool-task"],
        axis_deltas={
            "answer_exact": {
                "estimate": 0.0,
                "lower": 0.0,
                "upper": 0.0,
                "evidence_count": 1,
                "promotion_kind": "capability",
                "authority_level": "A4",
                "source": "static_exact",
                "saturated": True,
            }
        },
        efficiency_deltas={"token_cost": {"estimate": 0.0, "lower": 0.0, "upper": 0.0}},
        health_floor_status={"generator": "pass", "answer": "pass", "validator": "pass", "statistics": "pass", "leakage": "pass"},
        leakage_status="clean",
    )

    decision = ProgressOracle().decide(
        contract=_contract(minimum_frontier_tasks=1),
        comparison=comparison,
        static_suite_saturated=True,
        frontier_evidence_available=False,
    )

    assert decision.decision_type == "abstain"
    assert {"static_suite_saturated", "expand_frontier_challenges", "no_capability_signal"}.issubset(set(decision.reason_codes))
    assert "capability_archive" in decision.forbidden_optimizer_updates
