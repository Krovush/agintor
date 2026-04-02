from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from agintor.archive import QualityDiversityArchive
from agintor.benchmarks import build_demo_suite
from agintor.archive import PHASE_SCOPES
from agintor.evolution import EvolutionEngine
from agintor.providers import LocalDeterministicProvider
from agintor.project import init_runtime
from agintor.schemas import ArchiveEntry, ArchiveRecord, EvaluationStageResult, EvolutionHistoryRow, MutationCandidate, ObjectiveKind, ObjectiveSpec, RunResult, SuiteEvaluation

pytestmark = pytest.mark.usefixtures("module_failure_artifact_bucket")


def _run_result(task_id: str, verifier_score: float) -> RunResult:
    return RunResult(
        task_id=task_id,
        seed=0,
        artifact={"task_id": task_id},
        verifier_score=verifier_score,
        cost=0.0,
        latency=0.0,
        faults=0,
        trace_path=f"{task_id}.json",
        hard_invalid=False,
    )



def test_evolution_engine_runs_smoke(tmp_path: Path) -> None:
    runtime_dir = init_runtime(tmp_path / "runtime")
    engine = EvolutionEngine(build_demo_suite(), tmp_path / "evo", LocalDeterministicProvider(), runtime_dir, mutator_type="heuristic")
    summary = engine.run(steps=2)
    assert summary.archive_cells > 0
    assert summary.steps == 2
    assert len(engine.history) == 2


def test_stage4_full_rejects_children_when_mean_delta_is_negative(tmp_path: Path) -> None:
    suite = build_demo_suite()
    parent_dir = init_runtime(tmp_path / "parent")
    child_dir = init_runtime(tmp_path / "child")
    evaluator = EvolutionEngine(suite, tmp_path / "evo", LocalDeterministicProvider(), parent_dir, mutator_type="heuristic").evaluator
    task_ids = [task.task_id for task in suite.train]
    parent_scores = {f"s:{task_id}": 1.0 for task_id in task_ids}
    child_task_scores = dict(parent_scores)
    child_task_scores[f"s:{task_ids[0]}"] = 0.0
    parent_eval = SuiteEvaluation(
        runtime_hash="parent",
        objective_scores=parent_scores,
        task_scores={},
        family_scores={},
        run_results=[_run_result(task_id, 1.0) for task_id in task_ids],
        invalid=False,
    )

    def fake_evaluate_runtime(runtime_dir, partition="train", seeds=(0, 1, 2), use_cache=True, tasks_override=None):
        runtime_name = Path(runtime_dir).name
        if runtime_name == parent_dir.name:
            return parent_eval
        batch = list(tasks_override or suite.train)
        return SuiteEvaluation(
            runtime_hash="child",
            objective_scores={f"s:{task.task_id}": child_task_scores[f"s:{task.task_id}"] for task in batch},
            task_scores={},
            family_scores={},
            run_results=[_run_result(task.task_id, child_task_scores[f"s:{task.task_id}"]) for task in batch],
            invalid=False,
        )

    evaluator.evaluate_runtime = fake_evaluate_runtime  # type: ignore[method-assign]
    stage4 = evaluator.stage4_full(parent_dir, child_dir)
    assert stage4.passed is False
    assert stage4.suite_evaluation is not None
    assert stage4.metrics["delta"] < 0.0


def test_evolution_updates_scope_credit_for_fully_evaluated_rejected_child_without_counterfactuals(tmp_path: Path) -> None:
    suite = build_demo_suite()
    runtime_dir = init_runtime(tmp_path / "runtime")
    engine = EvolutionEngine(suite, tmp_path / "evo", LocalDeterministicProvider(), runtime_dir, mutator_type="heuristic")
    objective = ObjectiveSpec(name="sbar:tool", kind=ObjectiveKind.FAMILY, family="tool")
    scope = ["tool", "ctl"]
    child_dir = init_runtime(tmp_path / "child_rejected")
    parent_eval = SuiteEvaluation(
        runtime_hash="parent",
        objective_scores={"sbar:tool": 0.50, "sbar:global": 0.50},
        task_scores={},
        family_scores={},
        run_results=[_run_result("tool.generated_sum_squares_mod", 1.0)],
        invalid=False,
    )
    parent_record = ArchiveRecord(
        objective=objective.name,
        key="parent-cell",
        entry=ArchiveEntry(
            code_hash="parent-code",
            runtime_hash="parent",
            scores=parent_eval.objective_scores,
            behavior_bin=["single", "low", "low", "low"],
            scope_tag="seed",
            complexity_bucket=0,
            mutable_loc=10,
            trace_refs=[],
        ),
        runtime_dir=str(runtime_dir),
    )
    child_eval = SuiteEvaluation(
        runtime_hash="child",
        objective_scores={"sbar:tool": 0.40, "sbar:global": 0.40},
        task_scores={},
        family_scores={},
        run_results=[_run_result("tool.generated_sum_squares_mod", 0.0)],
        invalid=False,
    )
    engine.seed_archive = lambda: None  # type: ignore[method-assign]
    engine._select_objective = lambda seed: objective  # type: ignore[method-assign]
    engine.scheduler.sample_scope = lambda objective_name, seed: list(scope)  # type: ignore[method-assign]
    engine.archive.select_parent = lambda objective_name, seed: parent_record  # type: ignore[method-assign]
    engine.archive.runtime_evaluations[parent_record.entry.runtime_hash] = parent_eval
    engine.mutator.mutate = lambda context: MutationCandidate(  # type: ignore[method-assign]
        runtime_dir=str(runtime_dir),
        patch_text="",
        touched_scope=list(scope),
        prompt="",
        objective=objective.name,
    )
    engine.evaluator.staged_evaluate = lambda parent_dir, candidate, objective_spec: (  # type: ignore[method-assign]
        [
            EvaluationStageResult(stage=0, passed=True, reason="ok"),
            EvaluationStageResult(stage=1, passed=True, reason="ok"),
            EvaluationStageResult(stage=2, passed=True, reason="ok"),
            EvaluationStageResult(stage=3, passed=True, reason="ok"),
            EvaluationStageResult(stage=4, passed=True, reason="full", suite_evaluation=child_eval),
        ],
        child_dir,
    )
    engine.archive.insert = lambda *args, **kwargs: []  # type: ignore[method-assign]
    credit_updates: list[tuple[str, tuple[str, ...], float]] = []
    counterfactual_updates: list[tuple[tuple[str, ...], dict[str, float], dict[tuple[str, str], float]]] = []
    engine.scheduler.update_scope_credit = lambda objective_name, stage_scope, delta: credit_updates.append((objective_name, tuple(stage_scope), delta))  # type: ignore[method-assign]
    engine.scheduler.update_counterfactuals = lambda stage_scope, singleton, pairwise: counterfactual_updates.append((tuple(stage_scope), dict(singleton), dict(pairwise)))  # type: ignore[method-assign]

    engine.run(steps=1)

    assert credit_updates == [(objective.name, tuple(scope), pytest.approx(-0.05))]
    assert counterfactual_updates == []
    assert engine.history[0].accepted is False
    assert child_dir.exists() is False


def test_evolution_updates_predictors_after_full_evaluation(tmp_path: Path) -> None:
    suite = build_demo_suite()
    runtime_dir = init_runtime(tmp_path / "runtime")
    engine = EvolutionEngine(suite, tmp_path / "evo", LocalDeterministicProvider(), runtime_dir, mutator_type="heuristic")
    objective = ObjectiveSpec(name="sbar:tool", kind=ObjectiveKind.FAMILY, family="tool")
    scope = ["tool"]
    parent_eval = SuiteEvaluation(
        runtime_hash="parent",
        objective_scores={"sbar:tool": 0.50, "sbar:global": 0.50},
        task_scores={},
        family_scores={},
        run_results=[_run_result("tool.generated_sum_squares_mod", 1.0)],
        invalid=False,
    )
    parent_record = ArchiveRecord(
        objective=objective.name,
        key="parent-cell",
        entry=ArchiveEntry(
            code_hash="parent-code",
            runtime_hash="parent",
            scores=parent_eval.objective_scores,
            behavior_bin=["single", "low", "low", "low"],
            scope_tag="seed",
            complexity_bucket=0,
            mutable_loc=10,
            trace_refs=[],
        ),
        runtime_dir=str(runtime_dir),
    )
    trace_path = tmp_path / "predictor_trace.json"
    trace_path.write_text(json.dumps([{"event": "mode_selected", "mode": "single"}, {"event": "stop", "verified": True}]), encoding="utf-8")
    child_eval = SuiteEvaluation(
        runtime_hash="child",
        objective_scores={"sbar:tool": 0.60, "sbar:global": 0.60},
        task_scores={},
        family_scores={},
        run_results=[
            RunResult(
                task_id="tool.generated_sum_squares_mod",
                seed=0,
                artifact={"value": 2},
                verifier_score=1.0,
                cost=0.0,
                latency=0.0,
                faults=0,
                trace_path=str(trace_path),
                hard_invalid=False,
                mode="single",
            )
        ],
        invalid=False,
    )
    engine.seed_archive = lambda: None  # type: ignore[method-assign]
    engine._select_objective = lambda seed: objective  # type: ignore[method-assign]
    engine.scheduler.sample_scope = lambda objective_name, seed: list(scope)  # type: ignore[method-assign]
    engine.archive.select_parent = lambda objective_name, seed: parent_record  # type: ignore[method-assign]
    engine.archive.runtime_evaluations[parent_record.entry.runtime_hash] = parent_eval
    engine.mutator.mutate = lambda context: MutationCandidate(  # type: ignore[method-assign]
        runtime_dir=str(runtime_dir),
        patch_text="",
        touched_scope=list(scope),
        prompt="",
        objective=objective.name,
    )
    engine.evaluator.staged_evaluate = lambda parent_dir, candidate, objective_spec: (  # type: ignore[method-assign]
        [
            EvaluationStageResult(stage=0, passed=True, reason="ok"),
            EvaluationStageResult(stage=1, passed=True, reason="ok"),
            EvaluationStageResult(stage=2, passed=True, reason="ok"),
            EvaluationStageResult(stage=3, passed=True, reason="ok"),
            EvaluationStageResult(stage=4, passed=True, reason="full", suite_evaluation=child_eval),
        ],
        runtime_dir,
    )
    engine.archive.insert = lambda *args, **kwargs: ["cell"]  # type: ignore[method-assign]
    engine._counterfactual_contributions = lambda *args, **kwargs: ({}, {})  # type: ignore[method-assign]

    engine.run(steps=1)

    assert engine.predictors.summary()["families"]


def test_evolution_counts_archive_insertions_as_accepted_progress(tmp_path: Path) -> None:
    suite = build_demo_suite()
    runtime_dir = init_runtime(tmp_path / "runtime")
    engine = EvolutionEngine(suite, tmp_path / "evo", LocalDeterministicProvider(), runtime_dir, mutator_type="heuristic")
    objective = ObjectiveSpec(name="sbar:tool", kind=ObjectiveKind.FAMILY, family="tool")
    scope = ["tool"]
    parent_eval = SuiteEvaluation(
        runtime_hash="parent",
        objective_scores={"sbar:tool": 0.50, "sbar:global": 0.50},
        task_scores={},
        family_scores={},
        run_results=[_run_result("tool.generated_sum_squares_mod", 1.0)],
        invalid=False,
    )
    parent_record = ArchiveRecord(
        objective=objective.name,
        key="parent-cell",
        entry=ArchiveEntry(
            code_hash="parent-code",
            runtime_hash="parent",
            scores=parent_eval.objective_scores,
            behavior_bin=["single", "low", "low", "low"],
            scope_tag="seed",
            complexity_bucket=0,
            mutable_loc=10,
            trace_refs=[],
        ),
        runtime_dir=str(runtime_dir),
    )
    child_eval = SuiteEvaluation(
        runtime_hash="child",
        objective_scores={"sbar:tool": 0.50, "sbar:global": 0.60},
        task_scores={},
        family_scores={},
        run_results=[_run_result("tool.generated_sum_squares_mod", 1.0)],
        invalid=False,
    )
    counterfactual_updates: list[tuple[tuple[str, ...], dict[str, float], dict[tuple[str, str], float]]] = []
    engine.seed_archive = lambda: None  # type: ignore[method-assign]
    engine._select_objective = lambda seed: objective  # type: ignore[method-assign]
    engine.scheduler.sample_scope = lambda objective_name, seed: list(scope)  # type: ignore[method-assign]
    engine.archive.select_parent = lambda objective_name, seed: parent_record  # type: ignore[method-assign]
    engine.archive.runtime_evaluations[parent_record.entry.runtime_hash] = parent_eval
    engine.mutator.mutate = lambda context: MutationCandidate(  # type: ignore[method-assign]
        runtime_dir=str(runtime_dir),
        patch_text="",
        touched_scope=list(scope),
        prompt="",
        objective=objective.name,
    )
    engine.evaluator.staged_evaluate = lambda parent_dir, candidate, objective_spec: (  # type: ignore[method-assign]
        [
            EvaluationStageResult(stage=0, passed=True, reason="ok"),
            EvaluationStageResult(stage=1, passed=True, reason="ok"),
            EvaluationStageResult(stage=2, passed=True, reason="ok"),
            EvaluationStageResult(stage=3, passed=True, reason="ok"),
            EvaluationStageResult(stage=4, passed=True, reason="full", suite_evaluation=child_eval),
        ],
        runtime_dir,
    )
    engine.archive.insert = lambda *args, **kwargs: ["new-cell"]  # type: ignore[method-assign]
    engine.scheduler.update_counterfactuals = lambda stage_scope, singleton, pairwise: counterfactual_updates.append((tuple(stage_scope), dict(singleton), dict(pairwise)))  # type: ignore[method-assign]
    engine._counterfactual_contributions = lambda *args, **kwargs: ({"tool": 0.1}, {})  # type: ignore[method-assign]

    engine.run(steps=1)

    assert engine.history[0].accepted is True
    assert counterfactual_updates == [(tuple(scope), {"tool": 0.1}, {})]


def test_evolution_counts_stage0_failures_as_hard_failures(tmp_path: Path) -> None:
    suite = build_demo_suite()
    runtime_dir = init_runtime(tmp_path / "runtime")
    engine = EvolutionEngine(suite, tmp_path / "evo", LocalDeterministicProvider(), runtime_dir, mutator_type="heuristic")
    objective = ObjectiveSpec(name="sbar:top", kind=ObjectiveKind.FAMILY, family="top")
    scope = ["top"]
    parent_eval = SuiteEvaluation(
        runtime_hash="parent",
        objective_scores={"sbar:top": 0.50, "sbar:global": 0.50},
        task_scores={},
        family_scores={},
        run_results=[_run_result("top.sum_product", 1.0)],
        invalid=False,
    )
    parent_record = ArchiveRecord(
        objective=objective.name,
        key="parent-cell",
        entry=ArchiveEntry(
            code_hash="parent-code",
            runtime_hash="parent",
            scores=parent_eval.objective_scores,
            behavior_bin=["single", "low", "low", "low"],
            scope_tag="seed",
            complexity_bucket=0,
            mutable_loc=10,
            trace_refs=[],
        ),
        runtime_dir=str(runtime_dir),
    )
    engine.seed_archive = lambda: None  # type: ignore[method-assign]
    engine._select_objective = lambda seed: objective  # type: ignore[method-assign]
    engine.scheduler.sample_scope = lambda objective_name, seed: list(scope)  # type: ignore[method-assign]
    engine.archive.select_parent = lambda objective_name, seed: parent_record  # type: ignore[method-assign]
    engine.archive.runtime_evaluations[parent_record.entry.runtime_hash] = parent_eval
    engine.mutator.mutate = lambda context: MutationCandidate(  # type: ignore[method-assign]
        runtime_dir=str(runtime_dir),
        patch_text="bad patch",
        touched_scope=list(scope),
        prompt="",
        objective=objective.name,
    )
    engine.evaluator.staged_evaluate = lambda parent_dir, candidate, objective_spec: (  # type: ignore[method-assign]
        [EvaluationStageResult(stage=0, passed=False, reason="patch exceeded max block count")],
        None,
    )
    hardfail_scopes: list[tuple[str, ...]] = []
    engine.scheduler.note_hard_failure = lambda failed_scope: hardfail_scopes.append(tuple(failed_scope))  # type: ignore[method-assign]

    engine.run(steps=1)

    assert hardfail_scopes == [tuple(scope)]


def test_evolution_tightens_thresholds_when_stage_pass_rates_exceed_caps(tmp_path: Path) -> None:
    runtime_dir = init_runtime(tmp_path / "runtime_caps")
    engine = EvolutionEngine(build_demo_suite(), tmp_path / "evo_caps", LocalDeterministicProvider(), runtime_dir, mutator_type="heuristic")
    tightened: list[str] = []
    engine.evaluator.tighten_thresholds = lambda stage_name: tightened.append(stage_name)  # type: ignore[method-assign]
    for stage_name in engine.stage_counters:
        engine.stage_counters[stage_name]["total"] = 9
        engine.stage_counters[stage_name]["passed"] = 9

    engine._record_stage_pass_rates(
        [
            EvaluationStageResult(stage=1, passed=True, reason="ok"),
            EvaluationStageResult(stage=2, passed=True, reason="ok"),
            EvaluationStageResult(stage=3, passed=True, reason="ok"),
        ]
    )

    assert tightened == ["stage1", "stage2", "stage3"]


def test_failing_train_traces_embed_trace_rows_not_just_paths(tmp_path: Path) -> None:
    runtime_dir = init_runtime(tmp_path / "runtime")
    engine = EvolutionEngine(build_demo_suite(), tmp_path / "evo", LocalDeterministicProvider(), runtime_dir, mutator_type="heuristic")
    trace_path = tmp_path / "trace.json"
    trace = [{"event": "tool_fault"}, {"event": "stop"}]
    trace_path.write_text(json.dumps(trace), encoding="utf-8")
    evaluation = SuiteEvaluation(
        runtime_hash="runtime",
        objective_scores={},
        task_scores={},
        family_scores={},
        run_results=[
            RunResult(
                task_id="tool.generated_sum_squares_mod",
                seed=0,
                artifact={"error": "failed"},
                verifier_score=0.0,
                cost=0.0,
                latency=0.0,
                faults=1,
                trace_path=str(trace_path),
                hard_invalid=False,
            )
        ],
        invalid=False,
    )
    failing = engine._failing_train_traces(evaluation)
    assert failing[0]["trace"] == trace


def test_counterfactual_scope_credit_uses_proxy_reversions(tmp_path: Path) -> None:
    suite = build_demo_suite()
    parent_dir = init_runtime(tmp_path / "parent")
    child_dir = tmp_path / "child"
    shutil.copytree(parent_dir, child_dir)
    engine = EvolutionEngine(suite, tmp_path / "evo", LocalDeterministicProvider(), parent_dir, mutator_type="heuristic")
    proxy_task = next(task for task in suite.proxy if {"top", "tool"} & set(task.proxy_scope_tags))
    score_by_name = {
        "child": 0.9,
        "revert_top": 0.5,
        "revert_tool": 0.6,
        "revert_top_tool": 0.4,
    }

    def fake_evaluate_runtime(runtime_dir, partition="proxy", seeds=(0,), use_cache=True, tasks_override=None):
        name = Path(runtime_dir).name
        score = score_by_name[name]
        task_ids = [task.task_id for task in tasks_override or [proxy_task]]
        return SuiteEvaluation(
            runtime_hash=name,
            objective_scores={f"s:{task_id}": score for task_id in task_ids},
            task_scores={},
            family_scores={},
            run_results=[_run_result(task_id, score) for task_id in task_ids],
            invalid=False,
        )

    engine.evaluator.evaluate_runtime = fake_evaluate_runtime  # type: ignore[method-assign]

    singleton, pairwise = engine._counterfactual_contributions(parent_dir, child_dir, ["top", "tool"])

    assert singleton == {"top": pytest.approx(0.4), "tool": pytest.approx(0.3)}
    assert pairwise == {("top", "tool"): pytest.approx(0.2)}


def test_evolution_maybe_crossover_invokes_runtime_crossover(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent_dir = init_runtime(tmp_path / "parent_xover")
    donor_dir = init_runtime(tmp_path / "donor_xover")
    engine = EvolutionEngine(build_demo_suite(), tmp_path / "evo_xover", LocalDeterministicProvider(), parent_dir, mutator_type="heuristic")
    parent_record = ArchiveRecord(
        objective="sbar:tool",
        key="parent",
        entry=ArchiveEntry(
            code_hash="code-parent",
            runtime_hash="parent",
            scores={"sbar:tool": 1.0},
            behavior_bin=["single", "low", "low", "low"],
            scope_tag="tool",
            complexity_bucket=0,
            mutable_loc=1,
            trace_refs=[],
        ),
        runtime_dir=str(parent_dir),
    )
    donor_record = ArchiveRecord(
        objective="sbar:tool",
        key="donor",
        entry=ArchiveEntry(
            code_hash="code-donor",
            runtime_hash="donor",
            scores={"sbar:tool": 1.1},
            behavior_bin=["single", "low", "low", "low"],
            scope_tag="tool",
            complexity_bucket=0,
            mutable_loc=1,
            trace_refs=[],
        ),
        runtime_dir=str(donor_dir),
    )
    engine.archive.by_objective["sbar:tool"] = [parent_record, donor_record]
    engine.crossover_probability = 1.0
    captured: list[tuple[Path, list[Path], dict[str, list[str]]]] = []

    def fake_crossover_runtime(base_runtime_dir, donor_runtime_dirs, interface_methods, workspace):
        captured.append((base_runtime_dir, list(donor_runtime_dirs), dict(interface_methods)))
        return donor_dir

    monkeypatch.setitem(engine._maybe_crossover.__globals__, "crossover_runtime", fake_crossover_runtime)
    result = engine._maybe_crossover(parent_record, "sbar:tool", ["tool"], seed=0)

    assert result == donor_dir
    assert captured
    assert captured[0][0] == parent_dir
    assert captured[0][1] == [donor_dir]
    assert "tool" in captured[0][2]


def test_validation_tick_counts_four_way_joint_scopes_in_phase_coverage(tmp_path: Path) -> None:
    runtime_dir = init_runtime(tmp_path / "runtime")
    engine = EvolutionEngine(build_demo_suite(), tmp_path / "evo", LocalDeterministicProvider(), runtime_dir, mutator_type="heuristic")
    engine.scheduler.phase = "joint"
    engine.history = [
        EvolutionHistoryRow(
            step=1,
            objective="sbar:global",
            parent_runtime_hash="parent",
            child_runtime_hash="child",
            scope=["top", "mem", "tool", "ctl"],
            stage_results=[EvaluationStageResult(stage=4, passed=True, reason="full")],
            accepted=True,
            inserted_keys=["cell"],
        )
    ]
    engine.archive.island = lambda objective_name: [  # type: ignore[method-assign]
        ArchiveRecord(
            objective=objective_name,
            key="leader",
            entry=ArchiveEntry(
                code_hash="code",
                runtime_hash="runtime",
                scores={"sbar:global": 1.0},
                behavior_bin=["single", "low", "low", "low"],
                scope_tag="seed",
                complexity_bucket=0,
                mutable_loc=1,
                trace_refs=[],
            ),
            runtime_dir=str(runtime_dir),
        )
    ]
    engine.evaluator.evaluate_validation = lambda runtime_path: SuiteEvaluation(  # type: ignore[method-assign]
        runtime_hash="runtime",
        objective_scores={"sbar:global": 1.0},
        task_scores={},
        family_scores={},
        run_results=[],
        invalid=False,
    )
    captured: list[tuple[float, float, float]] = []
    engine.scheduler.maybe_advance_phase = lambda improvement, coverage, pass_rate: captured.append((improvement, coverage, pass_rate)) or False  # type: ignore[method-assign]

    engine._validation_tick(5)

    assert captured
    assert captured[0][1] == pytest.approx(1.0 / len(PHASE_SCOPES["joint"]))
