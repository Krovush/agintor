from __future__ import annotations

from types import SimpleNamespace

from agintor.contracts import ObjectiveKind, ObjectiveSpec, PromotionDecision, RunResult, SuiteEvaluation, TaskScore
from agintor.factory.export import _export_candidate_records
from agintor.learning.observations import extract_predictor_observations_for_promotion
from agintor.search.archive import QualityDiversityArchive
from agintor.search.engine import EvolutionEngine, route_promotion_decision


def _decision(decision_type: str) -> PromotionDecision:
    allowed = {
        "capability": ["capability_archive", "capability_scheduler", "capability_predictors", "capability_priors"],
        "efficiency": ["efficiency_archive", "efficiency_predictors"],
        "subskill": ["subskill_archive", "subskill_scheduler", "subskill_predictors"],
        "abstain": ["diagnostic_log", "diagnostic_predictors"],
        "reject": ["hard_failure_stats", "diagnostic_predictors"],
    }
    forbidden = {
        "capability": ["efficiency_archive"],
        "efficiency": ["capability_archive", "capability_scheduler", "capability_predictors", "capability_priors"],
        "subskill": ["capability_archive", "capability_predictors", "capability_priors", "efficiency_archive"],
        "abstain": ["capability_archive", "capability_scheduler", "capability_predictors", "capability_priors", "efficiency_archive"],
        "reject": ["capability_archive", "capability_scheduler", "capability_predictors", "capability_priors", "efficiency_archive"],
    }
    return PromotionDecision(
        decision_id=f"decision-{decision_type}",
        decision_type=decision_type,
        contract_id="generated_tool_workflow_v1",
        scope={"domain": "tool", "axis_ids": ["expression_generalization"]},
        winning_runtime_hash="child-runtime" if decision_type not in {"abstain", "reject"} else "",
        parent_runtime_hash="parent-runtime",
        child_runtime_hash="child-runtime",
        comparison_ref="cmp-tool-parent-child",
        allowed_optimizer_updates=allowed[decision_type],
        forbidden_optimizer_updates=forbidden[decision_type],
        reason_codes=[f"{decision_type}_route"],
        evidence_refs=["evidence-ledger:cmp-tool-parent-child"],
    )


def _run_result() -> RunResult:
    return RunResult(
        request_id="request-tool",
        run_id="run-tool",
        runtime_hash="child-runtime",
        task_id="tool.frontier.challenge",
        seed=0,
        artifact={"value": 81},
        verifier_score=1.0,
        cost=4.0,
        latency=0.2,
        faults=0,
        trace=[
            {"event": "mode_selected", "mode": "benchmark"},
            {"event": "tool_operation", "tool": "generated_expression"},
            {"event": "checks_requested", "count": 1},
        ],
        tokens_used=100,
        model_calls=1,
        checks_used=1,
    )


def _evaluation() -> SuiteEvaluation:
    task_score = TaskScore(
        s=1.0,
        rho=1.0,
        cvar=1.0,
        utilities=[1.0],
        verifier_scores=[1.0],
        costs=[4.0],
        latencies=[0.2],
        faults=[0],
    )
    return SuiteEvaluation(
        runtime_hash="child-runtime",
        objective_scores={"s:tool.frontier.challenge": 1.0, "sbar:tool": 1.0, "sbar:global": 1.0},
        task_scores={"tool.frontier.challenge": task_score},
        family_scores={"tool": {"s": 1.0, "rho": 1.0}},
        run_results=[_run_result()],
        invalid=False,
    )


def test_promotion_decision_controls_archive_and_prior_routing() -> None:
    capability_route = route_promotion_decision(_decision("capability"))
    efficiency_route = route_promotion_decision(_decision("efficiency"))
    subskill_route = route_promotion_decision(_decision("subskill"))
    abstain_route = route_promotion_decision(_decision("abstain"))

    assert capability_route.archive_name == "capability"
    assert capability_route.insert_archive is True
    assert capability_route.scheduler_credit_kind == "capability"
    assert capability_route.predictor_family_prefix == "capability"
    assert capability_route.updates_capability_priors is True

    assert efficiency_route.archive_name == "efficiency"
    assert efficiency_route.insert_archive is True
    assert efficiency_route.scheduler_credit_kind == "efficiency"
    assert efficiency_route.predictor_family_prefix == "efficiency"
    assert efficiency_route.updates_capability_priors is False

    assert subskill_route.archive_name == "subskill"
    assert subskill_route.insert_archive is True
    assert subskill_route.updates_capability_priors is False

    assert abstain_route.archive_name is None
    assert abstain_route.insert_archive is False
    assert abstain_route.updates_capability_priors is False


def test_capability_predictors_ignore_efficiency_only_wins() -> None:
    task_family_map = {"tool.frontier.challenge": "tool"}
    efficiency_observations = extract_predictor_observations_for_promotion(
        _evaluation(),
        task_family_map,
        decision=_decision("efficiency"),
    )
    capability_observations = extract_predictor_observations_for_promotion(
        _evaluation(),
        task_family_map,
        decision=_decision("capability"),
    )

    assert efficiency_observations
    assert capability_observations
    assert all((obs.metadata or {}).get("promotion_type") == "efficiency" for obs in efficiency_observations)
    assert all((obs.metadata or {}).get("updates_capability_prior") is False for obs in efficiency_observations)
    assert not any(obs.family.startswith("capability:") for obs in efficiency_observations)

    assert any((obs.metadata or {}).get("promotion_type") == "capability" for obs in capability_observations)
    assert any((obs.metadata or {}).get("updates_capability_prior") is True for obs in capability_observations)
    assert any(obs.family.startswith("capability:") for obs in capability_observations)


def test_archive_replacement_uses_objective_score_not_delta_scale() -> None:
    archive = QualityDiversityArchive(delta_f=0.0)
    seed_eval = _evaluation().model_copy(
        update={
            "runtime_hash": "seed-runtime",
            "objective_scores": {"s:tool.frontier.challenge": 0.5, "sbar:tool": 0.5, "sbar:global": 0.5},
        }
    )
    child_eval = _evaluation().model_copy(
        update={
            "runtime_hash": "child-runtime",
            "objective_scores": {"s:tool.frontier.challenge": 0.6, "sbar:tool": 0.6, "sbar:global": 0.6},
        }
    )
    decision = _decision("capability").model_copy(update={"quality_delta_lower": 0.01, "quality_delta_estimate": 0.01})

    archive.insert("seed", "seed-runtime", "same-code", 10, seed_eval, scope=["tool"])
    archive.insert("child", "child-runtime", "same-code", 10, child_eval, scope=["tool"], promotion_decision=decision)

    leader = archive.island("sbar:global")[0]
    assert leader.entry.runtime_hash == "child-runtime"
    assert leader.entry.promotion_score == 0.6


def test_default_archive_views_are_capability_only() -> None:
    archive = QualityDiversityArchive()

    archive.insert(
        "child",
        "child-runtime",
        "code",
        10,
        _evaluation(),
        scope=["tool"],
        archive_kind="efficiency",
        promotion_decision=_decision("efficiency"),
    )

    island = archive.island("sbar:global")
    assert island == []
    assert archive.select_parent("sbar:global", seed=1, archive_kind="efficiency").archive_kind == "efficiency"


def test_efficiency_archive_replacement_uses_efficiency_delta_as_tie_breaker() -> None:
    archive = QualityDiversityArchive(delta_f=0.0)
    incumbent_eval = _evaluation().model_copy(update={"runtime_hash": "incumbent-runtime"})
    cheaper_eval = _evaluation().model_copy(update={"runtime_hash": "cheaper-runtime"})
    decision = _decision("efficiency").model_copy(update={"efficiency_delta_lower": 0.2, "efficiency_delta_estimate": 0.2})

    archive.insert(
        "incumbent",
        "incumbent-runtime",
        "same-code",
        10,
        incumbent_eval,
        scope=["tool"],
        archive_kind="efficiency",
    )
    archive.insert(
        "cheaper",
        "cheaper-runtime",
        "same-code",
        10,
        cheaper_eval,
        scope=["tool"],
        archive_kind="efficiency",
        promotion_decision=decision,
    )

    leader = archive.island("sbar:global", archive_kind="efficiency")[0]
    assert leader.entry.runtime_hash == "cheaper-runtime"
    assert leader.entry.promotion_score == 0.2


def test_efficiency_parent_selection_uses_promotion_score() -> None:
    archive = QualityDiversityArchive(delta_f=0.0)
    high_quality_eval = _evaluation().model_copy(update={"runtime_hash": "high-quality-runtime"})
    efficient_eval = _evaluation().model_copy(update={"runtime_hash": "efficient-runtime"})

    archive.insert(
        "quality",
        "high-quality-runtime",
        "code-quality",
        10,
        high_quality_eval,
        scope=["tool"],
        interface_diff_mask="1000",
        archive_kind="efficiency",
        promotion_decision=_decision("efficiency").model_copy(update={"efficiency_delta_lower": 0.1, "efficiency_delta_estimate": 0.1}),
    )
    archive.insert(
        "efficient",
        "efficient-runtime",
        "code-efficient",
        10,
        efficient_eval,
        scope=["mem"],
        interface_diff_mask="0100",
        archive_kind="efficiency",
        promotion_decision=_decision("efficiency").model_copy(update={"efficiency_delta_lower": 0.9, "efficiency_delta_estimate": 0.9}),
    )

    selected = archive.select_parent("sbar:global", seed=1, archive_kind="efficiency")

    assert selected.entry.runtime_hash == "efficient-runtime"


def test_route_promotion_decision_respects_forbidden_updates() -> None:
    decision = _decision("capability").model_copy(
        update={
            "allowed_optimizer_updates": ["capability_scheduler", "capability_predictors"],
            "forbidden_optimizer_updates": ["capability_archive", "capability_priors"],
        }
    )

    route = route_promotion_decision(decision)

    assert route.insert_archive is False
    assert route.scheduler_credit_kind == "capability"
    assert route.updates_capability_priors is False


def test_export_candidates_ignore_non_capability_archives() -> None:
    archive = QualityDiversityArchive()
    archive.insert(
        "capability",
        "capability-runtime",
        "cap-code",
        10,
        _evaluation().model_copy(update={"runtime_hash": "capability-runtime"}),
        scope=["tool"],
        archive_kind="capability",
        promotion_decision=_decision("capability"),
    )
    archive.insert(
        "subskill",
        "subskill-runtime",
        "sub-code",
        10,
        _evaluation().model_copy(update={"runtime_hash": "subskill-runtime"}),
        scope=["tool"],
        archive_kind="subskill",
        promotion_decision=_decision("subskill"),
    )
    engine = SimpleNamespace(archive=archive)

    candidates = _export_candidate_records(engine, ["sbar:global"])

    assert [record.entry.runtime_hash for record in candidates] == ["capability-runtime"]
    assert all(record.archive_kind == "capability" for record in candidates)


def test_export_candidates_ignore_unrequested_capability_islands() -> None:
    archive = QualityDiversityArchive()
    archive.insert(
        "mem",
        "mem-runtime",
        "mem-code",
        10,
        _evaluation().model_copy(
            update={
                "runtime_hash": "mem-runtime",
                "objective_scores": {"sbar:mem": 1.0},
            }
        ),
        scope=["mem"],
        archive_kind="capability",
        promotion_decision=_decision("capability"),
        objectives=["sbar:mem"],
    )
    archive.insert(
        "tool",
        "tool-runtime",
        "tool-code",
        10,
        _evaluation().model_copy(update={"runtime_hash": "tool-runtime"}),
        scope=["tool"],
        archive_kind="capability",
        promotion_decision=_decision("capability"),
        objectives=["sbar:tool"],
    )
    engine = SimpleNamespace(archive=archive)

    candidates = _export_candidate_records(engine, ["sbar:tool"])

    assert [record.entry.runtime_hash for record in candidates] == ["tool-runtime"]


def test_engine_archive_objectives_follow_improved_axes_without_unrelated_islands() -> None:
    engine = EvolutionEngine.__new__(EvolutionEngine)
    engine.suite = SimpleNamespace(
        train=[SimpleNamespace(task_id="tool.frontier.challenge", family="tool")],
        by_id=lambda task_id: SimpleNamespace(task_id=task_id, family="tool"),
    )
    evaluation = _evaluation().model_copy(
        update={
            "objective_scores": {
                "s:tool.frontier.challenge": 1.0,
                "s:mem.unrelated": 1.0,
                "sbar:tool": 1.0,
                "sbar:mem": 1.0,
                "sbar:global": 1.0,
            }
        }
    )
    progress_signal = SimpleNamespace(improved_axes=["tool.frontier.challenge"])
    decision = _decision("capability").model_copy(update={"progress_signal": progress_signal})

    objectives = engine._archive_objectives_for_promotion(
        evaluation,
        decision,
        ObjectiveSpec(name="s:tool.frontier.challenge", kind=ObjectiveKind.SINGLE_TASK, task_id="tool.frontier.challenge"),
    )

    assert "s:tool.frontier.challenge" in objectives
    assert "sbar:tool" in objectives
    assert "s:mem.unrelated" not in objectives
    assert "sbar:mem" not in objectives


def test_engine_archive_objectives_index_global_for_semantic_capability_axes() -> None:
    engine = EvolutionEngine.__new__(EvolutionEngine)
    engine.suite = SimpleNamespace(
        train=[SimpleNamespace(task_id="tool.frontier.challenge", family="tool")],
        by_id=lambda task_id: SimpleNamespace(task_id=task_id, family="tool"),
    )
    evaluation = _evaluation().model_copy(
        update={
            "objective_scores": {
                "s:tool.frontier.challenge": 1.0,
                "sbar:tool": 1.0,
                "sbar:global": 1.0,
                "rhobar:global": 1.0,
            }
        }
    )
    progress_signal = SimpleNamespace(
        improved_axes=["expression_generalization"],
        pairwise_comparisons=[
            SimpleNamespace(axis_task_ids={"expression_generalization": ["tool.frontier.challenge"]})
        ],
    )
    decision = _decision("capability").model_copy(update={"progress_signal": progress_signal})

    objectives = engine._archive_objectives_for_promotion(
        evaluation,
        decision,
        ObjectiveSpec(name="s:tool.frontier.challenge", kind=ObjectiveKind.SINGLE_TASK, task_id="tool.frontier.challenge"),
    )

    assert "s:tool.frontier.challenge" in objectives
    assert "sbar:global" in objectives
    assert "rhobar:global" in objectives


def test_engine_archive_objectives_index_efficiency_into_global_island() -> None:
    engine = EvolutionEngine.__new__(EvolutionEngine)
    engine.suite = SimpleNamespace(train=[], by_id=lambda task_id: (_ for _ in ()).throw(KeyError(task_id)))
    evaluation = _evaluation().model_copy(
        update={
            "objective_scores": {
                "s:tool.frontier.challenge": 1.0,
                "sbar:global": 1.0,
                "rhobar:global": 1.0,
            }
        }
    )
    decision = _decision("efficiency")

    objectives = engine._archive_objectives_for_promotion(
        evaluation,
        decision,
        ObjectiveSpec(name="s:tool.frontier.challenge", kind=ObjectiveKind.SINGLE_TASK, task_id="tool.frontier.challenge"),
    )

    assert "sbar:global" in objectives
    assert "rhobar:global" in objectives


def test_engine_does_not_index_unrelated_sampled_task_objective() -> None:
    engine = EvolutionEngine.__new__(EvolutionEngine)
    engine.suite = SimpleNamespace(
        train=[
            SimpleNamespace(task_id="tool.frontier.challenge", family="tool"),
            SimpleNamespace(task_id="mem.unrelated", family="mem"),
        ],
        by_id=lambda task_id: SimpleNamespace(task_id=task_id, family="tool" if task_id.startswith("tool.") else "mem"),
    )
    evaluation = _evaluation().model_copy(
        update={
            "objective_scores": {
                "s:tool.frontier.challenge": 1.0,
                "s:mem.unrelated": 0.0,
                "sbar:tool": 1.0,
                "sbar:global": 0.5,
            }
        }
    )
    progress_signal = SimpleNamespace(improved_axes=["tool.frontier.challenge"])
    decision = _decision("capability").model_copy(update={"progress_signal": progress_signal})

    objectives = engine._archive_objectives_for_promotion(
        evaluation,
        decision,
        ObjectiveSpec(name="s:mem.unrelated", kind=ObjectiveKind.SINGLE_TASK, task_id="mem.unrelated"),
    )

    assert "s:tool.frontier.challenge" in objectives
    assert "sbar:tool" in objectives
    assert "s:mem.unrelated" not in objectives
