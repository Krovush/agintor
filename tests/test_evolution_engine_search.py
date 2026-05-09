from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agintor.contracts import EvaluationStageResult, ObjectiveKind, ObjectiveSpec, PromotionDecision, RunResult, SuiteEvaluation, TaskScore
from agintor.search.archive import QualityDiversityArchive, ScopeScheduler
from agintor.search.engine import EvolutionEngine


TASK_ID = "tool.frontier.challenge"


class RecordingPredictors:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def add_observation(self, family, feature_vector, *, probability_label=None, positive_label=None, metadata=None) -> None:
        self.records.append(
            {
                "family": family,
                "probability_label": probability_label,
                "positive_label": positive_label,
                "metadata": dict(metadata or {}),
            }
        )

    def maybe_retrain(self, *_args) -> None:
        return

    def summary(self) -> dict[str, object]:
        return {}


class FakeMutator:
    def mutate(self, context):
        return SimpleNamespace(patch_text="", touched_scope=list(context.touched_scope))


class FakeEvaluator:
    def __init__(self, tmp_path: Path, decision_types: list[str]) -> None:
        self.tmp_path = tmp_path
        self.decision_types = list(decision_types)
        self.parent_dirs: list[Path] = []
        self.evidence_ledger_path = tmp_path / "evidence.jsonl"
        self.paired_comparison_ledger_path = tmp_path / "comparisons.jsonl"
        self.promotion_ledger_path = tmp_path / "promotions.jsonl"

    def staged_evaluate(self, parent_dir: Path, _candidate, _objective):
        self.parent_dirs.append(Path(parent_dir))
        step = len(self.parent_dirs)
        requested_decision_type = self.decision_types[min(step - 1, len(self.decision_types) - 1)]
        decision_type = requested_decision_type.replace("_no_archive", "").replace("_no_predictors", "").replace("_failed_gate", "")
        child_hash = f"child-{step}"
        decision = _decision(requested_decision_type, child_hash)
        stage4 = EvaluationStageResult(
            stage=4,
            passed=decision_type in {"capability", "efficiency", "subskill", "preference"} and not requested_decision_type.endswith("_failed_gate"),
            reason=f"fake {decision_type}",
            metrics={},
            suite_evaluation=_evaluation(child_hash, float(step)),
            promotion_decision=decision,
            promotion_type=decision.decision_type,
        )
        return [stage4], self.tmp_path / child_hash


def _run(runtime_hash: str, score: float) -> RunResult:
    return RunResult(
        runtime_hash=runtime_hash,
        task_id=TASK_ID,
        seed=0,
        artifact={"answer": score},
        verifier_score=score,
        cost=1.0,
        latency=1.0,
        faults=0,
        trace=[{"event": "tool_operation"}, {"event": "checks_requested"}],
    )


def _evaluation(runtime_hash: str, score: float) -> SuiteEvaluation:
    task_score = TaskScore(
        s=score,
        rho=score,
        cvar=score,
        utilities=[score],
        verifier_scores=[score],
        costs=[1.0],
        latencies=[1.0],
        faults=[0],
    )
    return SuiteEvaluation(
        runtime_hash=runtime_hash,
        objective_scores={f"s:{TASK_ID}": score, "sbar:tool": score, "sbar:global": score},
        task_scores={TASK_ID: task_score},
        family_scores={"tool": {"s": score, "rho": score}},
        run_results=[_run(runtime_hash, score)],
        task_metadata={TASK_ID: {"domain_kind": "generated_tool_workflow", "slice_tags": ["frontier"]}},
    )


def _decision(requested_decision_type: str, child_hash: str) -> PromotionDecision:
    decision_type = requested_decision_type.replace("_no_archive", "").replace("_no_predictors", "").replace("_failed_gate", "")
    allowed = {
        "capability": ["capability_archive", "capability_scheduler", "capability_predictors", "capability_priors"],
        "capability_no_predictors": ["capability_archive", "capability_scheduler"],
        "capability_failed_gate": ["capability_archive", "capability_scheduler", "capability_predictors", "capability_priors"],
        "efficiency": ["efficiency_archive", "efficiency_predictors"],
        "subskill": ["subskill_archive", "subskill_scheduler", "subskill_predictors"],
        "subskill_no_archive": ["subskill_scheduler"],
        "no_progress": ["diagnostic_log", "diagnostic_predictors"],
        "quarantine": ["hard_failure_stats", "diagnostic_predictors"],
    }
    forbidden = {
        "capability": ["efficiency_archive"],
        "capability_no_predictors": ["capability_predictors", "capability_priors", "efficiency_archive"],
        "capability_failed_gate": ["efficiency_archive"],
        "efficiency": ["capability_archive", "capability_scheduler", "capability_predictors", "capability_priors"],
        "subskill": ["capability_archive", "capability_predictors", "capability_priors", "efficiency_archive"],
        "subskill_no_archive": ["capability_archive", "capability_predictors", "capability_priors", "efficiency_archive", "subskill_archive"],
        "no_progress": ["capability_archive", "capability_scheduler", "capability_predictors", "capability_priors", "efficiency_archive"],
        "quarantine": ["capability_archive", "capability_scheduler", "capability_predictors", "capability_priors", "efficiency_archive"],
    }
    return PromotionDecision(
        decision_id=f"decision-{decision_type}-{child_hash}",
        decision_type=decision_type,
        contract_id="test-contract",
        winning_runtime_hash=child_hash if decision_type in {"capability", "efficiency", "subskill"} else "",
        parent_runtime_hash="parent",
        child_runtime_hash=child_hash,
        comparison_ref=f"cmp-{child_hash}",
        allowed_optimizer_updates=allowed[requested_decision_type],
        forbidden_optimizer_updates=forbidden[requested_decision_type],
        reason_codes=[decision_type],
        quality_delta_lower=0.5 if decision_type in {"capability", "subskill"} else 0.0,
        quality_delta_estimate=0.5 if decision_type in {"capability", "subskill"} else 0.0,
        efficiency_delta_lower=0.5 if decision_type == "efficiency" else 0.0,
        efficiency_delta_estimate=0.5 if decision_type == "efficiency" else 0.0,
    )


def _engine(tmp_path: Path, decision_types: list[str]) -> EvolutionEngine:
    engine = EvolutionEngine.__new__(EvolutionEngine)
    engine.workspace = tmp_path
    engine.archive = QualityDiversityArchive(delta_f=0.0)
    engine.scheduler = ScopeScheduler()
    engine.evaluator = FakeEvaluator(tmp_path, decision_types)
    engine.predictors = RecordingPredictors()
    engine.mutator = FakeMutator()
    engine.objectives = [ObjectiveSpec(name="sbar:global", kind=ObjectiveKind.GLOBAL)]
    engine.history = []
    engine.best_val_score = float("-inf")
    engine.validation_history = []
    engine.phase_remaining = {"local": 10, "pair": 0, "joint": 0}
    engine.pass_rate_caps = {"stage1": 1.0, "stage2": 1.0, "stage3": 1.0}
    engine.stage_counters = {
        "stage1": {"passed": 0, "total": 0},
        "stage2": {"passed": 0, "total": 0},
        "stage3": {"passed": 0, "total": 0},
    }
    engine.fully_evaluated_since_retrain = 0
    engine.accepted_since_retrain = 0
    engine.crossover_probability = 0.0
    engine.runtime_profile = SimpleNamespace()
    engine.trace_context = None
    task = SimpleNamespace(task_id=TASK_ID, family="tool")
    engine.suite = SimpleNamespace(train=[task], by_id=lambda _task_id: task)
    baseline_dir = tmp_path / "baseline"
    runtime_by_hash = {
        "baseline": SimpleNamespace(runtime_hash="baseline", code_hash="code-baseline", mutable_loc=10, mutable_ast_nodes=10),
    }

    def seed_archive() -> None:
        engine.archive.insert(
            str(baseline_dir),
            "baseline",
            "code-baseline",
            10,
            _evaluation("baseline", 0.0),
            scope=[],
            mutable_ast_nodes=10,
            interface_diff_mask="0000",
        )

    def load_runtime(runtime_dir: Path):
        runtime_hash = Path(runtime_dir).name
        return runtime_by_hash.setdefault(
            runtime_hash,
            SimpleNamespace(runtime_hash=runtime_hash, code_hash=f"code-{runtime_hash}", mutable_loc=10, mutable_ast_nodes=10),
        )

    engine.seed_archive = seed_archive
    engine._load_runtime = load_runtime
    engine._interface_diff_mask = lambda _runtime_dir: "1000"
    engine._counterfactual_contributions = lambda *_args: ({}, {})
    engine._cleanup_path = lambda *_args, **_kwargs: None
    return engine


def test_accepted_child_becomes_future_parent(tmp_path: Path) -> None:
    engine = _engine(tmp_path, ["capability", "capability", "capability"])

    engine.run(steps=3)

    assert engine.history[0].parent_runtime_hash == "baseline"
    assert engine.history[1].parent_runtime_hash == "child-1"


def test_efficiency_promotions_can_be_future_parents(tmp_path: Path) -> None:
    engine = _engine(tmp_path, ["efficiency", "capability"])

    engine.run(steps=2)

    assert engine.history[0].promotion_type == "efficiency"
    assert engine.history[0].accepted is True
    assert engine.history[1].parent_runtime_hash == "child-1"


def test_predictors_receive_non_promoted_stage4_observations(tmp_path: Path) -> None:
    engine = _engine(tmp_path, ["no_progress"])

    engine.run(steps=1)

    assert any(record["metadata"].get("promotion_type") == "no_progress" for record in engine.predictors.records)
    assert all(record["metadata"].get("accepted") is False for record in engine.predictors.records)


def test_predictors_use_actual_archive_retention_for_promoting_decisions(tmp_path: Path) -> None:
    engine = _engine(tmp_path, ["subskill_no_archive"])

    engine.run(steps=1)

    assert engine.history[0].accepted is False
    assert engine.history[0].promotion_type == "subskill"
    assert all(record["metadata"].get("accepted") is False for record in engine.predictors.records)


def test_promotion_counts_only_retained_promotions(tmp_path: Path) -> None:
    engine = _engine(tmp_path, ["subskill_no_archive"])

    summary = engine.run(steps=1)

    assert summary.decision_counts == {"subskill": 1}
    assert summary.promotion_counts == {}


def test_signal_sufficiency_blocks_ws5_predictor_control_without_heldout_evidence(tmp_path: Path) -> None:
    engine = _engine(tmp_path, ["capability"])

    summary = engine.run(steps=1)
    report = json.loads(Path(summary.signal_sufficiency_path).read_text(encoding="utf-8"))

    assert Path(summary.signal_sufficiency_path).name == "signal_sufficiency.json"
    assert report["safe_for_predictor_backed_ws5_control"] is False
    assert report["status"] == "insufficient"
    assert "insufficient_host_verified_stage4_evidence" in report["reasons"]
    assert "missing_held_out_evidence" in report["reasons"]


def test_predictors_respect_forbidden_predictor_updates(tmp_path: Path) -> None:
    engine = _engine(tmp_path, ["capability_no_predictors"])

    engine.run(steps=1)

    assert engine.history[0].promotion_type == "capability"
    assert engine.predictors.records == []


def test_quarantine_records_hard_failure_and_diagnostic_predictors(tmp_path: Path) -> None:
    engine = _engine(tmp_path, ["quarantine"])

    engine.run(steps=1)

    assert engine.history[0].promotion_type == "quarantine"
    assert engine.history[0].accepted is False
    assert any(value > 0 for value in engine.scheduler.hardfail.values())
    assert any(record["metadata"].get("promotion_type") == "quarantine" for record in engine.predictors.records)


def test_failed_stage4_promotive_decision_is_not_archived(tmp_path: Path) -> None:
    engine = _engine(tmp_path, ["capability_failed_gate"])

    summary = engine.run(steps=1)

    assert engine.history[0].promotion_type == "capability"
    assert engine.history[0].accepted is False
    assert "child-1" not in {record.entry.runtime_hash for record in engine.archive.archive_records()}
    assert summary.promotion_counts == {}
    assert all(record["metadata"].get("accepted") is False for record in engine.predictors.records)
