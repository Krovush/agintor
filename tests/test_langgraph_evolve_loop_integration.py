from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agintor.contracts import (
    EvaluationStageResult,
    ObjectiveKind,
    ObjectiveSpec,
    PromotionDecision,
    RunResult,
    SuiteEvaluation,
    TaskScore,
    baseline_langgraph_runtime_spec,
)
from agintor.evaluation.benchmarks import load_suite
from agintor.providers import LocalDeterministicProvider
from agintor.runtime.langgraph.compiler import RuntimeSpecCompiler
from agintor.runtime.profile import load_runtime_profile
from agintor.search.archive import QualityDiversityArchive, ScopeScheduler
from agintor.search.engine import EvolutionEngine


TASK_ID = "top.echo"


class SpecLoopEvaluator:
    def __init__(self, tmp_path: Path) -> None:
        self.calls: list[dict[str, object]] = []
        self.evidence_ledger_path = tmp_path / "evidence.jsonl"
        self.paired_comparison_ledger_path = tmp_path / "comparisons.jsonl"
        self.promotion_ledger_path = tmp_path / "promotions.jsonl"

    def staged_evaluate(self, *_args, **_kwargs):  # pragma: no cover - must not be used for spec-backed loops.
        raise AssertionError("spec-backed evolution must use staged_evaluate_runtime_pair")

    def staged_evaluate_runtime_pair(
        self,
        parent_dir: Path,
        child_dir: Path,
        objective: ObjectiveSpec,
        *,
        scope,
        mutation_action_ids=(),
    ):
        self.calls.append(
            {
                "parent_dir": Path(parent_dir),
                "child_dir": Path(child_dir),
                "objective": objective.name,
                "scope": list(scope),
                "mutation_action_ids": list(mutation_action_ids),
            }
        )
        child_hash = f"child-{len(self.calls)}"
        decision = PromotionDecision(
            decision_id=f"decision.{child_hash}",
            decision_type="capability",
            contract_id="contract.spec-loop",
            parent_runtime_hash="baseline",
            child_runtime_hash=child_hash,
            winning_runtime_hash=child_hash,
            allowed_optimizer_updates=[
                "capability_archive",
                "capability_scheduler",
                "capability_predictors",
                "capability_priors",
            ],
            forbidden_optimizer_updates=["efficiency_archive"],
            reason_codes=["spec_loop"],
            quality_delta_lower=0.5,
            quality_delta_estimate=0.5,
            oracle_package_hash="oracle.hash",
            child_runtime_spec_digest="child.spec.digest",
        )
        stage4 = EvaluationStageResult(
            stage=4,
            passed=True,
            reason="spec loop accepted",
            metrics={},
            suite_evaluation=_evaluation(child_hash, 1.0),
            promotion_decision=decision,
            promotion_type=decision.decision_type,
        )
        return [stage4], Path(child_dir)


class RecordingPredictors:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def add_observation(self, family, feature_vector, *, probability_label=None, positive_label=None, metadata=None) -> None:
        self.records.append({"family": family, "metadata": dict(metadata or {})})

    def maybe_retrain(self, *_args) -> None:
        return

    def summary(self) -> dict[str, object]:
        return {}


def _run(runtime_hash: str, score: float) -> RunResult:
    return RunResult(
        runtime_hash=runtime_hash,
        task_id=TASK_ID,
        seed=0,
        artifact={"score": score},
        verifier_score=score,
        cost=0.0,
        latency=0.0,
        faults=0,
    )


def _evaluation(runtime_hash: str, score: float) -> SuiteEvaluation:
    task_score = TaskScore(
        s=score,
        rho=score,
        cvar=score,
        utilities=[score],
        verifier_scores=[score],
        costs=[0.0],
        latencies=[0.0],
        faults=[0],
    )
    return SuiteEvaluation(
        runtime_hash=runtime_hash,
        objective_scores={f"s:{TASK_ID}": score, "sbar:top": score, "sbar:global": score},
        task_scores={TASK_ID: task_score},
        family_scores={"top": {"s": score, "rho": score}},
        run_results=[_run(runtime_hash, score)],
        task_metadata={TASK_ID: {"domain_kind": "spec_loop"}},
    )


def test_langgraph_evolve_loop_integration_runs_spec_mutator_and_runtime_pair_path(tmp_path: Path) -> None:
    baseline_dir = RuntimeSpecCompiler().compile_to_directory(
        baseline_langgraph_runtime_spec(runtime_id="runtime.evolve-loop"),
        tmp_path / "baseline",
        force=True,
    )
    profile = load_runtime_profile()
    engine = EvolutionEngine(
        load_suite("demo"),
        tmp_path / "evolution",
        LocalDeterministicProvider(),
        baseline_dir,
        runtime_profile=profile,
        runtime_backend="local",
        artifact_mode="none",
    )
    engine.archive = QualityDiversityArchive(delta_f=0.0)
    engine.scheduler = ScopeScheduler()
    engine.evaluator = SpecLoopEvaluator(tmp_path)
    engine.predictors = RecordingPredictors()
    engine.objectives = [ObjectiveSpec(name="sbar:global", kind=ObjectiveKind.GLOBAL)]
    engine.phase_remaining = {"local": 1, "pair": 0, "joint": 0}
    engine.crossover_probability = 0.0
    engine._cleanup_path = lambda *_args, **_kwargs: None

    baseline_runtime = engine._load_runtime(baseline_dir)

    def seed_archive() -> None:
        engine.archive.insert(
            str(baseline_dir),
            "baseline",
            baseline_runtime.code_hash,
            baseline_runtime.mutable_loc,
            _evaluation("baseline", 0.0),
            scope=[],
            mutable_ast_nodes=baseline_runtime.mutable_ast_nodes,
            interface_diff_mask="0000",
            oracle_package_hash="oracle.hash",
            runtime_spec_digest=baseline_runtime.manifest.runtime_spec_digest,
        )

    engine.seed_archive = seed_archive

    summary = engine.run(steps=1)
    call = engine.evaluator.calls[0]

    assert summary.steps == 1
    assert engine.spec_backed is True
    assert call["parent_dir"] == baseline_dir
    assert Path(call["child_dir"], "runtime_spec.json").is_file()
    assert call["mutation_action_ids"]
    assert engine.history[0].mutation_action_ids == call["mutation_action_ids"]
    assert engine.history[0].accepted is True
    child_runtime_hash = engine._load_runtime(call["child_dir"]).runtime_hash
    assert child_runtime_hash in {record.entry.runtime_hash for record in engine.archive.archive_records()}
