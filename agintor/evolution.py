from __future__ import annotations

import json
import random
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

from .archive import PHASE_SCOPES, QualityDiversityArchive, ScopeScheduler, objective_specs_from_suite
from .benchmarks import BenchmarkSuite
from .evaluator import RuntimeEvaluator
from .mutator import HeuristicPatchMutator, MutationContext, OpenAIPatchMutator
from .providers import ModelProvider
from .runtime_loader import load_runtime
from .pydantic_compat import model_dump
from .schemas import EvolutionHistoryRow, ObjectiveSpec
from .utils import ensure_directory, stable_hash


@dataclass
class EvolutionSummary:
    steps: int
    accepted: int
    archive_cells: int
    best_train_score: float
    best_val_score: float
    history_path: str


class EvolutionEngine:
    def __init__(self, suite: BenchmarkSuite, workspace: Path, provider: ModelProvider, baseline_runtime_dir: Path, mutator_type: str = "heuristic") -> None:
        self.suite = suite
        self.workspace = ensure_directory(workspace)
        self.provider = provider
        self.baseline_runtime_dir = baseline_runtime_dir
        self.archive = QualityDiversityArchive()
        self.scheduler = ScopeScheduler()
        self.evaluator = RuntimeEvaluator(suite, self.workspace / "evaluator", provider, baseline_runtime_dir=baseline_runtime_dir)
        self.objectives = objective_specs_from_suite(suite, partition="train")
        self.history: list[EvolutionHistoryRow] = []
        self.mutator = HeuristicPatchMutator() if mutator_type == "heuristic" else OpenAIPatchMutator(provider)
        self.best_val_score = float("-inf")

    def seed_archive(self) -> None:
        baseline_runtime = load_runtime(self.baseline_runtime_dir)
        baseline_eval = self.evaluator.evaluate_runtime(self.baseline_runtime_dir, partition="train", seeds=[0, 1, 2], use_cache=False)
        self.archive.insert(str(self.baseline_runtime_dir), baseline_runtime.runtime_hash, baseline_runtime.code_hash, baseline_runtime.mutable_loc, baseline_eval, scope=[])
        # Four subsystem-local seeded variants.
        for scope, objective_name in [(["top"], "sbar:top"), (["mem"], "sbar:mem"), (["tool"], "sbar:tool"), (["ctl"], "rhobar:global")]:
            objective = next(spec for spec in self.objectives if spec.name == objective_name)
            context = MutationContext(objective=objective.name, touched_scope=scope, runtime_dir=self.baseline_runtime_dir, workspace=self.workspace / "seeded", predictor_summaries={}, failing_train_traces=[], exemplars=[], seed=len(self.history) + 1)
            candidate = self.mutator.mutate(context)
            stages, child_dir = self.evaluator.staged_evaluate(self.baseline_runtime_dir, candidate, objective)
            stage4 = next((stage for stage in stages if stage.stage == 4 and stage.suite_evaluation is not None), None)
            if child_dir is not None and stage4 is not None:
                child_runtime = load_runtime(child_dir)
                self.archive.insert(str(child_dir), child_runtime.runtime_hash, child_runtime.code_hash, child_runtime.mutable_loc, stage4.suite_evaluation, scope=scope)

    def _objective_by_name(self, name: str) -> ObjectiveSpec:
        return next(spec for spec in self.objectives if spec.name == name)

    def _select_objective(self, seed: int) -> ObjectiveSpec:
        rng = random.Random(seed)
        return self.objectives[rng.randrange(len(self.objectives))]

    def _failing_train_traces(self, evaluation) -> list[dict[str, object]]:
        rows = []
        for run in evaluation.run_results:
            if run.verifier_score < 1.0:
                rows.append({"task_id": run.task_id, "trace_path": run.trace_path, "verifier_score": run.verifier_score, "invalid": run.hard_invalid})
        return rows[:4]

    def _exemplars(self, objective_name: str, limit: int = 4) -> list[dict[str, object]]:
        island = self.archive.island(objective_name)
        exemplars = sorted(island, key=lambda record: record.entry.scores.get(objective_name, float("-inf")), reverse=True)[:limit]
        return [{"runtime_hash": record.entry.runtime_hash, "score": record.entry.scores.get(objective_name), "scope": record.entry.scope_tag} for record in exemplars]

    def _validation_tick(self, iteration: int) -> None:
        if iteration % 5 != 0:
            return
        island = self.archive.island("sbar:global")
        if not island:
            return
        leader = max(island, key=lambda record: record.entry.scores.get("sbar:global", float("-inf")))
        val_eval = self.evaluator.evaluate_validation(Path(leader.runtime_dir))
        val_score = val_eval.objective_scores.get("sbar:global", float("-inf"))
        improvement = val_score - self.best_val_score if self.best_val_score != float("-inf") else val_score
        self.best_val_score = max(self.best_val_score, val_score)
        accepted_scopes = [row.scope for row in self.history if row.accepted]
        coverage = len({tuple(sorted(scope)) for scope in accepted_scopes if len(scope) == {"local": 1, "pair": 2, "joint": 3}.get(self.scheduler.phase, 4)}) / max(1, len(PHASE_SCOPES[self.scheduler.phase]))
        pass_rate = sum(1 for row in self.history[-5:] if any(stage.stage == 4 and stage.passed for stage in row.stage_results)) / max(1, len(self.history[-5:]))
        self.scheduler.maybe_advance_phase(improvement, coverage, pass_rate)

    def run(self, steps: int = 10) -> EvolutionSummary:
        self.seed_archive()
        accepted = 0
        for step in range(1, steps + 1):
            objective = self._select_objective(step)
            scope = self.scheduler.sample_scope(objective.name, seed=step)
            parent_record = self.archive.select_parent(objective.name, seed=step)
            parent_eval = self.archive.runtime_evaluations[parent_record.entry.runtime_hash]
            context = MutationContext(
                objective=objective.name,
                touched_scope=scope,
                runtime_dir=Path(parent_record.runtime_dir),
                workspace=self.workspace / "candidates",
                predictor_summaries={"phase": self.scheduler.phase},
                failing_train_traces=self._failing_train_traces(parent_eval),
                exemplars=self._exemplars(objective.name),
                seed=step,
            )
            candidate = self.mutator.mutate(context)
            stage_results, child_dir = self.evaluator.staged_evaluate(Path(parent_record.runtime_dir), candidate, objective)
            inserted_keys: list[str] = []
            child_hash = None
            accepted_flag = False
            if child_dir is not None:
                stage4 = next((stage for stage in stage_results if stage.stage == 4 and stage.suite_evaluation is not None), None)
                if stage4 is not None and stage4.passed:
                    child_runtime = load_runtime(child_dir)
                    child_hash = child_runtime.runtime_hash
                    inserted_keys = self.archive.insert(str(child_dir), child_runtime.runtime_hash, child_runtime.code_hash, child_runtime.mutable_loc, stage4.suite_evaluation, scope=scope)
                    delta = stage4.suite_evaluation.objective_scores.get(objective.name, 0.0) - parent_eval.objective_scores.get(objective.name, 0.0)
                    self.scheduler.update_scope_credit(objective.name, scope, delta / max(1, len(scope)))
                    singleton = {item: delta for item in scope}
                    pairwise = {tuple(sorted((a, b))): delta / 2.0 for idx, a in enumerate(scope) for b in scope[idx + 1 :]}
                    self.scheduler.update_counterfactuals(scope, singleton, pairwise)
                    accepted_flag = bool(inserted_keys)
                    if accepted_flag:
                        accepted += 1
                else:
                    self.scheduler.note_hard_failure(scope)
            row = EvolutionHistoryRow(step=step, objective=objective.name, parent_runtime_hash=parent_record.entry.runtime_hash, child_runtime_hash=child_hash, scope=scope, stage_results=stage_results, accepted=accepted_flag, inserted_keys=inserted_keys)
            self.history.append(row)
            self.scheduler.note_iteration([row.scope] if row.accepted else [])
            self._validation_tick(step)
        history_path = self.workspace / "evolution_history.json"
        history_path.write_text(json.dumps([model_dump(row) for row in self.history], indent=2), encoding="utf-8")
        best_train = max((record.entry.scores.get("sbar:global", float("-inf")) for record in self.archive.island("sbar:global")), default=float("-inf"))
        return EvolutionSummary(steps=steps, accepted=accepted, archive_cells=len(self.archive.cells), best_train_score=best_train, best_val_score=self.best_val_score, history_path=str(history_path))
