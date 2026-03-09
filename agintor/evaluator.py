from __future__ import annotations

import ast
import json
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from .archive import objective_specs_from_suite
from .benchmarks import BenchmarkSuite
from .exceptions import HardInvalidation, PatchApplyError
from .mutator import MutationCandidate
from .patches import parse_patch
from .providers import ModelProvider
from .runtime_loader import LoadedRuntime, load_runtime
from .runner import TaskRuntime
from .scoring import ScoreCalculator, estimate_reference_scales, mean_improvement
from .schemas import EvaluationStageResult, ObjectiveKind, ObjectiveSpec, SuiteEvaluation
from .shell import FixedShell
from .utils import ensure_directory, stable_hash


class RuntimeEvaluator:
    def __init__(self, suite: BenchmarkSuite, workspace: Path, provider: ModelProvider, baseline_runtime_dir: Path | None = None) -> None:
        self.suite = suite
        self.workspace = ensure_directory(workspace)
        self.provider = provider
        self.cache: dict[tuple[str, str, tuple[int, ...]], SuiteEvaluation] = {}
        self.reference_scales = ({}, {})
        if baseline_runtime_dir is not None:
            baseline_eval = self.evaluate_runtime(baseline_runtime_dir, partition="train", seeds=[0], use_cache=False)
            self.reference_scales = estimate_reference_scales(baseline_eval.run_results)

    def _score_calculator(self) -> ScoreCalculator:
        costs, latencies = self.reference_scales
        return ScoreCalculator(baseline_costs=costs, baseline_latencies=latencies)

    def evaluate_runtime(self, runtime_dir: str | Path, partition: str = "train", seeds: Sequence[int] = (0, 1, 2), use_cache: bool = True, tasks_override: Sequence[Any] | None = None) -> SuiteEvaluation:
        runtime = load_runtime(runtime_dir)
        cache_key = (runtime.runtime_hash, partition, tuple(seeds), tuple(task.task_id for task in tasks_override) if tasks_override is not None else ())
        if use_cache and cache_key in self.cache:
            return self.cache[cache_key]
        shell = FixedShell(self.workspace / f"eval_{runtime.runtime_hash[:10]}_{partition}")
        runner = TaskRuntime(runtime, shell, self.provider)
        tasks = list(tasks_override) if tasks_override is not None else self.suite.all_tasks(partition)
        run_results = []
        for task in tasks:
            for seed in seeds:
                run_results.append(runner.run_task(task, int(seed)))
        task_family_map = {task.task_id: task.family for task in tasks}
        evaluation = self._score_calculator().suite_score(runtime.runtime_hash, task_family_map, run_results)
        if use_cache:
            self.cache[cache_key] = evaluation
        return evaluation

    def _apply_patch_uniquely(self, parent_dir: Path, candidate: MutationCandidate) -> Path:
        runtime = load_runtime(parent_dir)
        child_dir = ensure_directory(self.workspace / f"patched_{stable_hash(parent_dir, candidate.patch_text)[:10]}")
        if child_dir.exists():
            shutil.rmtree(child_dir)
        shutil.copytree(parent_dir, child_dir)
        blocks = parse_patch(candidate.patch_text)
        for block in blocks:
            matches = []
            for rel_path in runtime.manifest.mutable_files:
                path = child_dir / rel_path
                source = path.read_text(encoding="utf-8")
                count = source.count(block.search)
                if count == 1:
                    matches.append(path)
                elif count > 1:
                    raise PatchApplyError(f"SEARCH block matched multiple locations in {rel_path}")
            if len(matches) != 1:
                raise PatchApplyError("SEARCH block must match exactly one mutable file")
            path = matches[0]
            source = path.read_text(encoding="utf-8")
            path.write_text(source.replace(block.search, block.replace, 1), encoding="utf-8")
        return child_dir

    def stage0_patch_integrity(self, parent_dir: Path, candidate: MutationCandidate) -> tuple[EvaluationStageResult, Path | None]:
        try:
            child_dir = self._apply_patch_uniquely(parent_dir, candidate)
            runtime = load_runtime(child_dir)
            for rel_path in runtime.manifest.mutable_files:
                source = (child_dir / rel_path).read_text(encoding="utf-8")
                ast.parse(source)
            return EvaluationStageResult(stage=0, passed=True, reason="patch applied and parsed"), child_dir
        except Exception as exc:
            return EvaluationStageResult(stage=0, passed=False, reason=str(exc)), None

    def stage1_smoke(self, child_dir: Path) -> EvaluationStageResult:
        smoke_task = self.suite.proxy[0]
        first = self.evaluate_runtime(child_dir, partition="proxy", seeds=[0], use_cache=False, tasks_override=[smoke_task])
        second = self.evaluate_runtime(child_dir, partition="proxy", seeds=[0], use_cache=False, tasks_override=[smoke_task])
        first_run = first.run_results[0]
        second_run = second.run_results[0]
        passed = not first.invalid and not second.invalid and first_run.artifact == second_run.artifact and first_run.verifier_score == second_run.verifier_score
        reason = "deterministic smoke passed" if passed else "smoke task nondeterministic or invalid"
        return EvaluationStageResult(stage=1, passed=passed, reason=reason, metrics={"artifact": first_run.artifact})

    def stage2_proxy(self, parent_dir: Path, child_dir: Path, scope: Sequence[str], epsilon_proxy: float = 0.01) -> EvaluationStageResult:
        proxy_tasks = [task for task in self.suite.proxy if set(task.proxy_scope_tags) & set(scope)]
        if not proxy_tasks:
            proxy_tasks = self.suite.proxy[:1]
        parent_eval = self.evaluate_runtime(parent_dir, partition="proxy", seeds=[0], tasks_override=proxy_tasks)
        child_eval = self.evaluate_runtime(child_dir, partition="proxy", seeds=[0], tasks_override=proxy_tasks)
        parent_scores = [parent_eval.objective_scores.get(f"s:{task.task_id}", 0.0) for task in proxy_tasks]
        child_scores = [child_eval.objective_scores.get(f"s:{task.task_id}", 0.0) for task in proxy_tasks]
        avg, se, lcb = mean_improvement(child_scores, parent_scores)
        passed = lcb > -epsilon_proxy and not child_eval.invalid
        return EvaluationStageResult(stage=2, passed=passed, reason="proxy LCB gate", metrics={"delta": avg, "se": se, "lcb": lcb}, suite_evaluation=child_eval)

    def _objective_subset(self, objective: ObjectiveSpec) -> list[Any]:
        train_tasks = self.suite.train
        if objective.kind == ObjectiveKind.SINGLE_TASK and objective.task_id:
            target = self.suite.by_id(objective.task_id)
            same_family = [task for task in train_tasks if task.family == target.family and task.task_id != target.task_id][:2]
            return [target] + same_family
        if objective.kind in {ObjectiveKind.FAMILY, ObjectiveKind.FAMILY_ROBUST} and objective.family:
            return self.suite.representative_family_tasks(objective.family, partition="train", limit=4)
        return [next(task for task in train_tasks if task.family == family) for family in ["top", "mem", "tool", "e2e"]]

    def stage3_local_subset(self, parent_dir: Path, child_dir: Path, objective: ObjectiveSpec, epsilon_part: float = 0.01) -> EvaluationStageResult:
        subset = self._objective_subset(objective)
        parent_eval = self.evaluate_runtime(parent_dir, partition="train", seeds=[0], tasks_override=subset)
        child_eval = self.evaluate_runtime(child_dir, partition="train", seeds=[0], tasks_override=subset)
        parent_scores = [parent_eval.objective_scores.get(f"s:{task.task_id}", 0.0) for task in subset]
        child_scores = [child_eval.objective_scores.get(f"s:{task.task_id}", 0.0) for task in subset]
        avg, se, lcb = mean_improvement(child_scores, parent_scores)
        passed = lcb > -epsilon_part and not child_eval.invalid
        return EvaluationStageResult(stage=3, passed=passed, reason="local subset LCB gate", metrics={"delta": avg, "se": se, "lcb": lcb}, suite_evaluation=child_eval)

    def stage4_full(self, parent_dir: Path, child_dir: Path) -> EvaluationStageResult:
        parent_eval = self.evaluate_runtime(parent_dir, partition="train", seeds=[0, 1, 2])
        child_eval = self.evaluate_runtime(child_dir, partition="train", seeds=[0, 1, 2])
        task_ids = [task.task_id for task in self.suite.train]
        parent_scores = [parent_eval.objective_scores.get(f"s:{task_id}", 0.0) for task_id in task_ids]
        child_scores = [child_eval.objective_scores.get(f"s:{task_id}", 0.0) for task_id in task_ids]
        avg, se, lcb = mean_improvement(child_scores, parent_scores)
        passed = not child_eval.invalid
        return EvaluationStageResult(stage=4, passed=passed, reason="full train evaluation", metrics={"delta": avg, "se": se, "lcb": lcb}, suite_evaluation=child_eval)

    def evaluate_validation(self, runtime_dir: Path) -> SuiteEvaluation:
        return self.evaluate_runtime(runtime_dir, partition="val", seeds=[0, 1, 2, 3, 4])

    def staged_evaluate(self, parent_dir: Path, candidate: MutationCandidate, objective: ObjectiveSpec) -> tuple[list[EvaluationStageResult], Path | None]:
        results: list[EvaluationStageResult] = []
        stage0, child_dir = self.stage0_patch_integrity(parent_dir, candidate)
        results.append(stage0)
        if not stage0.passed or child_dir is None:
            return results, None
        stage1 = self.stage1_smoke(child_dir)
        results.append(stage1)
        if not stage1.passed:
            return results, child_dir
        stage2 = self.stage2_proxy(parent_dir, child_dir, candidate.touched_scope)
        results.append(stage2)
        if not stage2.passed:
            return results, child_dir
        stage3 = self.stage3_local_subset(parent_dir, child_dir, objective)
        results.append(stage3)
        if not stage3.passed:
            return results, child_dir
        stage4 = self.stage4_full(parent_dir, child_dir)
        results.append(stage4)
        return results, child_dir
