from __future__ import annotations

import json
import random
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

from .artifacts import ArtifactMode, ArtifactPolicy
from .archive import PHASE_SCOPES, QualityDiversityArchive, ScopeScheduler, objective_specs_from_suite
from .benchmarks import BenchmarkSuite
from .crossover import crossover_runtime
from .evaluator import RuntimeEvaluator
from .mutator import HeuristicPatchMutator, MutationContext, ProviderPatchMutator
from .predictors import DecisionFamilyModelBank
from .providers import ModelProvider
from .prompt_builder import METHOD_CONTRACTS
from .runtime_loader import load_runtime
from .runtime_profile import RuntimeProfile, load_runtime_profile, resolve_runtime_profile
from .pydantic_compat import model_dump
from .schemas import EvolutionHistoryRow, ObjectiveSpec
from .trace_labeler import extract_predictor_observations
from .utils import ensure_directory, mean, stable_hash


@dataclass
class EvolutionSummary:
    steps: int
    accepted: int
    archive_cells: int
    best_train_score: float
    best_val_score: float
    history_path: str
    archive_index_path: str = ""
    validation_history_path: str = ""
    stage_failures_path: str = ""


class EvolutionEngine:
    def __init__(
        self,
        suite: BenchmarkSuite,
        workspace: Path,
        provider: ModelProvider,
        baseline_runtime_dir: Path,
        mutator_type: str = "heuristic",
        reference_runtime_dir: Path | None = None,
        budget_overrides: Dict[str, Any] | None = None,
        runtime_backend: str | None = None,
        runtime_profile: RuntimeProfile | None = None,
        profile_path: Path | None = None,
        artifact_mode: str | ArtifactMode | None = None,
        sandbox_root: Path | None = None,
    ) -> None:
        self.suite = suite
        self.workspace = Path(workspace)
        self.provider = provider
        self.artifact_policy = ArtifactPolicy.resolve(
            artifact_mode=artifact_mode,
            sandbox_root=sandbox_root,
        )
        self.retain_artifacts = self.artifact_policy.keep_successes
        self.baseline_runtime_dir = baseline_runtime_dir
        self.profile_path = Path(profile_path) if profile_path is not None else None
        self.runtime_profile = runtime_profile or load_runtime_profile(baseline_runtime_dir, profile_path=self.profile_path)
        self.predictors = DecisionFamilyModelBank()
        self.archive = QualityDiversityArchive()
        self.scheduler = ScopeScheduler()
        self.evaluator = RuntimeEvaluator(
            suite,
            self.workspace / "evaluator",
            provider,
            baseline_runtime_dir=reference_runtime_dir if reference_runtime_dir is not None else baseline_runtime_dir,
            budget_overrides=budget_overrides,
            predictors=self.predictors,
            runtime_backend=runtime_backend,
            runtime_profile=self.runtime_profile,
            profile_path=self.profile_path,
            artifact_mode=self.artifact_policy.mode,
            sandbox_root=self.artifact_policy.sandbox_root,
        )
        self.objectives = objective_specs_from_suite(suite, partition="train")
        self.history: list[EvolutionHistoryRow] = []
        normalized_mutator = mutator_type.strip().lower()
        if normalized_mutator == "heuristic":
            self.mutator = HeuristicPatchMutator()
        elif normalized_mutator in {"provider", "openai"}:
            if getattr(provider, "provider_name", "local") == "local":
                raise ValueError("provider mutator requires a hosted provider, not the local deterministic provider")
            self.mutator = ProviderPatchMutator(provider)
        else:
            raise ValueError(f"unknown mutator_type {mutator_type}")
        self.best_val_score = float("-inf")
        self.validation_history: list[dict[str, Any]] = []
        self._baseline_manifest = self._load_runtime(self.baseline_runtime_dir).manifest
        self.phase_remaining = dict(self.runtime_profile.evolution.phase_budgets)
        self.pass_rate_caps = dict(self.runtime_profile.evaluation.pass_rate_caps)
        self.stage_counters = {
            "stage1": {"passed": 0, "total": 0},
            "stage2": {"passed": 0, "total": 0},
            "stage3": {"passed": 0, "total": 0},
        }
        self.fully_evaluated_since_retrain = 0
        self.accepted_since_retrain = 0
        self.crossover_probability = self.runtime_profile.evolution.crossover_probability

    def _cleanup_path(self, path: Path | None, *, failed: bool = False) -> None:
        if path is None or not path.exists():
            return
        if failed and self.artifact_policy.keep_failures:
            return
        if not failed and self.artifact_policy.keep_successes:
            return
        shutil.rmtree(path, ignore_errors=True)

    def _load_runtime(self, runtime_dir: str | Path):
        runtime_profile = resolve_runtime_profile(
            runtime_dir,
            fallback_profile=self.runtime_profile,
            profile_path=self.profile_path,
        )
        return load_runtime(runtime_dir, runtime_profile=runtime_profile)

    def _interface_diff_mask(self, runtime_dir: Path) -> str:
        runtime = self._load_runtime(runtime_dir)
        bits: list[str] = []
        for interface in ["top", "mem", "tool", "ctl"]:
            baseline_rel = self._baseline_manifest.policy_modules[interface].split(":", 1)[0]
            runtime_rel = runtime.manifest.policy_modules[interface].split(":", 1)[0]
            baseline_source = (self.baseline_runtime_dir / baseline_rel).read_text(encoding="utf-8")
            runtime_source = (runtime_dir / runtime_rel).read_text(encoding="utf-8")
            bits.append("1" if baseline_source != runtime_source else "0")
        return "".join(bits)

    def _proxy_tasks_for_scope(self, scope: Sequence[str]) -> list[Any]:
        proxy_tasks = [task for task in self.suite.proxy if set(task.proxy_scope_tags) & set(scope)]
        return proxy_tasks or self.suite.proxy[:1]

    def _proxy_mean_score(self, evaluation, tasks: Sequence[Any]) -> float:
        return mean([evaluation.objective_scores.get(f"s:{task.task_id}", 0.0) for task in tasks])

    def _counterfactual_variant(self, parent_dir: Path, child_dir: Path, reverted_scope: Sequence[str]) -> Path:
        order = {name: idx for idx, name in enumerate(["top", "mem", "tool", "ctl"])}
        variant_name = "revert_" + "_".join(sorted(reverted_scope, key=lambda item: order.get(item, 99)))
        variant_dir = self.workspace / variant_name
        if variant_dir.exists():
            shutil.rmtree(variant_dir)
        shutil.copytree(child_dir, variant_dir)
        child_runtime = self._load_runtime(child_dir)
        parent_runtime = self._load_runtime(parent_dir)
        for interface in reverted_scope:
            child_rel = child_runtime.manifest.policy_modules[interface].split(":", 1)[0]
            parent_rel = parent_runtime.manifest.policy_modules[interface].split(":", 1)[0]
            shutil.copyfile(parent_dir / parent_rel, variant_dir / child_rel)
        return variant_dir

    def _counterfactual_contributions(self, parent_dir: Path, child_dir: Path, scope: Sequence[str]) -> tuple[dict[str, float], dict[tuple[str, str], float]]:
        order = {name: idx for idx, name in enumerate(["top", "mem", "tool", "ctl"])}
        proxy_tasks = self._proxy_tasks_for_scope(scope)
        seeds = self.runtime_profile.evaluation.proxy_seeds
        child_eval = self.evaluator.evaluate_runtime(child_dir, partition="proxy", seeds=seeds, use_cache=False, tasks_override=proxy_tasks)
        child_score = self._proxy_mean_score(child_eval, proxy_tasks)
        singleton: dict[str, float] = {}
        pairwise: dict[tuple[str, str], float] = {}
        for interface in scope:
            revert_path = self._counterfactual_variant(parent_dir, child_dir, [interface])
            revert_eval = self.evaluator.evaluate_runtime(
                revert_path,
                partition="proxy",
                seeds=seeds,
                use_cache=False,
                tasks_override=proxy_tasks,
            )
            self._cleanup_path(revert_path)
            singleton[interface] = child_score - self._proxy_mean_score(revert_eval, proxy_tasks)
        ordered_scope = list(scope)
        for idx, left in enumerate(ordered_scope):
            for right in ordered_scope[idx + 1 :]:
                revert_pair_path = self._counterfactual_variant(parent_dir, child_dir, [left, right])
                revert_pair_eval = self.evaluator.evaluate_runtime(
                    revert_pair_path,
                    partition="proxy",
                    seeds=seeds,
                    use_cache=False,
                    tasks_override=proxy_tasks,
                )
                self._cleanup_path(revert_pair_path)
                pair_key = tuple(sorted((left, right), key=lambda item: order.get(item, 99)))
                pairwise[pair_key] = child_score - (child_score - singleton[left]) - (child_score - singleton[right]) + self._proxy_mean_score(revert_pair_eval, proxy_tasks)
        return singleton, pairwise

    def seed_archive(self) -> None:
        baseline_runtime = self._load_runtime(self.baseline_runtime_dir)
        baseline_eval = self.evaluator.evaluate_runtime(
            self.baseline_runtime_dir,
            partition="train",
            seeds=self.runtime_profile.evaluation.full_train_seeds,
            use_cache=False,
        )
        self.archive.insert(
            str(self.baseline_runtime_dir),
            baseline_runtime.runtime_hash,
            baseline_runtime.code_hash,
            baseline_runtime.mutable_loc,
            baseline_eval,
            scope=[],
            mutable_ast_nodes=baseline_runtime.mutable_ast_nodes,
            interface_diff_mask=self._interface_diff_mask(self.baseline_runtime_dir),
        )

    def _objective_by_name(self, name: str) -> ObjectiveSpec:
        return next(spec for spec in self.objectives if spec.name == name)

    def _select_objective(self, seed: int) -> ObjectiveSpec:
        rng = random.Random(seed)
        return self.objectives[rng.randrange(len(self.objectives))]

    def _failing_train_traces(self, evaluation) -> list[dict[str, object]]:
        rows = []
        for run in evaluation.run_results:
            if run.verifier_score < 1.0:
                trace_payload: object = run.trace_rows() if hasattr(run, "trace_rows") else []
                rows.append(
                    {
                        "task_id": run.task_id,
                        "trace_path": run.trace_path,
                        "trace_ref": run.trace_ref() if hasattr(run, "trace_ref") else run.trace_path,
                        "trace": trace_payload,
                        "verifier_score": run.verifier_score,
                        "invalid": run.hard_invalid,
                        "invalid_reason": run.invalid_reason,
                    }
                )
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
        self.validation_history.append(
            {
                "iteration": iteration,
                "runtime_hash": leader.entry.runtime_hash,
                "runtime_dir": leader.runtime_dir,
                "objective_scores": val_eval.objective_scores,
                "validation_score": val_score,
            }
        )
        improvement = val_score - self.best_val_score if self.best_val_score != float("-inf") else val_score
        self.best_val_score = max(self.best_val_score, val_score)
        accepted_scopes = [row.scope for row in self.history if row.accepted]
        admissible_sizes = {"local": {1}, "pair": {2}, "joint": {3, 4}}.get(self.scheduler.phase, {4})
        order = {name: idx for idx, name in enumerate(["top", "mem", "tool", "ctl"])}
        covered_scopes = {
            tuple(sorted(scope, key=lambda item: order.get(item, 99)))
            for scope in accepted_scopes
            if len(scope) in admissible_sizes
        }
        coverage = len(covered_scopes) / max(1, len(PHASE_SCOPES[self.scheduler.phase]))
        pass_rate = sum(1 for row in self.history[-5:] if any(stage.stage == 4 and stage.passed for stage in row.stage_results)) / max(1, len(self.history[-5:]))
        self.scheduler.maybe_advance_phase(improvement, coverage, pass_rate)

    def _predictor_summaries(self) -> dict[str, object]:
        summary = self.predictors.summary()
        summary["phase"] = self.scheduler.phase
        return summary

    def _prepare_phase(self) -> bool:
        order = ["local", "pair", "joint"]
        while self.phase_remaining.get(self.scheduler.phase, 0) <= 0:
            current_index = order.index(self.scheduler.phase)
            if current_index >= len(order) - 1:
                return False
            self.scheduler.phase = order[current_index + 1]
        return True

    def _consume_phase_budget(self) -> None:
        self.phase_remaining[self.scheduler.phase] = max(0, self.phase_remaining.get(self.scheduler.phase, 0) - 1)

    def _record_stage_pass_rates(self, stage_results) -> None:
        stage_map = {1: "stage1", 2: "stage2", 3: "stage3"}
        for stage in stage_results:
            stage_name = stage_map.get(stage.stage)
            if stage_name is None:
                continue
            counters = self.stage_counters[stage_name]
            counters["total"] += 1
            if stage.passed:
                counters["passed"] += 1
            pass_rate = counters["passed"] / max(1, counters["total"])
            if counters["total"] >= 10 and pass_rate > self.pass_rate_caps[stage_name]:
                self.evaluator.tighten_thresholds(stage_name)

    def _maybe_crossover(self, parent_record, objective_name: str, scope: Sequence[str], seed: int) -> Path:
        donor_pool = [record for record in self.archive.island(objective_name) if record.entry.runtime_hash != parent_record.entry.runtime_hash]
        if not donor_pool:
            return Path(parent_record.runtime_dir)
        rng = random.Random(seed)
        if rng.random() >= self.crossover_probability:
            return Path(parent_record.runtime_dir)
        donor = donor_pool[rng.randrange(len(donor_pool))]
        interface_methods: dict[str, list[str]] = {}
        for interface in scope:
            methods = METHOD_CONTRACTS.get(interface, [])
            if methods:
                interface_methods[interface] = [methods[rng.randrange(len(methods))]]
        if not interface_methods:
            return Path(parent_record.runtime_dir)
        try:
            return crossover_runtime(
                Path(parent_record.runtime_dir),
                [Path(donor.runtime_dir)],
                interface_methods,
                self.workspace / "crossover",
            )
        except Exception:
            return Path(parent_record.runtime_dir)

    def _update_predictors(self, evaluation, *, accepted: bool) -> None:
        task_family_map = {task.task_id: task.family for task in self.suite.train}
        for observation in extract_predictor_observations(evaluation, task_family_map, accepted=accepted):
            self.predictors.add_observation(
                observation.family,
                observation.feature_vector,
                probability_label=observation.label_probability,
                positive_label=observation.label_positive_scalar,
                metadata=observation.metadata,
            )
        self.fully_evaluated_since_retrain += 1
        if accepted:
            self.accepted_since_retrain += 1
        if self.fully_evaluated_since_retrain >= 50 or self.accepted_since_retrain >= 10:
            self.predictors.maybe_retrain(self.fully_evaluated_since_retrain, self.accepted_since_retrain)
            self.fully_evaluated_since_retrain = 0
            self.accepted_since_retrain = 0

    def _stage4_result(self, stage_results):
        return next((stage for stage in stage_results if stage.stage == 4 and stage.suite_evaluation is not None), None)

    def _is_hard_failure(self, stage_results) -> bool:
        for stage in stage_results:
            if stage.stage in {0, 1} and not stage.passed:
                return True
            if stage.suite_evaluation is not None and stage.suite_evaluation.invalid:
                return True
        return False

    def run(self, steps: int = 10) -> EvolutionSummary:
        ensure_directory(self.workspace)
        self.seed_archive()
        accepted = 0
        for step in range(1, steps + 1):
            if not self._prepare_phase():
                break
            self._consume_phase_budget()
            objective = self._select_objective(step)
            scope = self.scheduler.sample_scope(objective.name, seed=step)
            parent_record = self.archive.select_parent(objective.name, seed=step)
            parent_dir = self._maybe_crossover(parent_record, objective.name, scope, step)
            if parent_dir == Path(parent_record.runtime_dir):
                parent_eval = self.archive.runtime_evaluations[parent_record.entry.runtime_hash]
            else:
                parent_eval = self.evaluator.evaluate_runtime(
                    parent_dir,
                    partition="train",
                    seeds=self.runtime_profile.evaluation.full_train_seeds,
                    use_cache=False,
                )
            context = MutationContext(
                objective=objective.name,
                touched_scope=scope,
                runtime_dir=parent_dir,
                workspace=self.workspace / "candidates",
                runtime_profile=self.runtime_profile,
                predictor_summaries=self._predictor_summaries(),
                failing_train_traces=self._failing_train_traces(parent_eval),
                exemplars=self._exemplars(objective.name),
                seed=step,
            )
            candidate = self.mutator.mutate(context)
            stage_results, child_dir = self.evaluator.staged_evaluate(parent_dir, candidate, objective)
            self._record_stage_pass_rates(stage_results)
            inserted_keys: list[str] = []
            child_hash = None
            accepted_flag = False
            stage4 = self._stage4_result(stage_results)
            if child_dir is not None and stage4 is not None and stage4.suite_evaluation is not None and not stage4.suite_evaluation.invalid:
                delta = stage4.suite_evaluation.objective_scores.get(objective.name, 0.0) - parent_eval.objective_scores.get(objective.name, 0.0)
                self.scheduler.update_scope_credit(objective.name, scope, delta / max(1, len(scope)))
                child_runtime = self._load_runtime(child_dir)
                child_hash = child_runtime.runtime_hash
                inserted_keys = self.archive.insert(
                    str(child_dir),
                    child_runtime.runtime_hash,
                    child_runtime.code_hash,
                    child_runtime.mutable_loc,
                    stage4.suite_evaluation,
                    scope=scope,
                    mutable_ast_nodes=child_runtime.mutable_ast_nodes,
                    interface_diff_mask=self._interface_diff_mask(child_dir),
                )
                accepted_flag = bool(inserted_keys)
                self._update_predictors(stage4.suite_evaluation, accepted=accepted_flag)
                if accepted_flag:
                    accepted += 1
                    singleton, pairwise = self._counterfactual_contributions(parent_dir, child_dir, scope)
                    self.scheduler.update_counterfactuals(scope, singleton, pairwise)
                else:
                    self._cleanup_path(child_dir)
            elif self._is_hard_failure(stage_results):
                self.scheduler.note_hard_failure(scope)
            if parent_dir != Path(parent_record.runtime_dir):
                self._cleanup_path(parent_dir)
            row = EvolutionHistoryRow(step=step, objective=objective.name, parent_runtime_hash=parent_record.entry.runtime_hash, child_runtime_hash=child_hash, scope=scope, stage_results=stage_results, accepted=accepted_flag, inserted_keys=inserted_keys)
            self.history.append(row)
            self.scheduler.note_iteration([row.scope] if row.accepted else [])
            self._validation_tick(step)
        history_path = self.workspace / "evolution_history.json"
        history_path.write_text(json.dumps([model_dump(row) for row in self.history], indent=2), encoding="utf-8")
        archive_index_path = self.workspace / "archive_index.json"
        archive_records = sorted(
            self.archive.cells.values(),
            key=lambda record: (record.objective, record.key),
        )
        archive_index_path.write_text(
            json.dumps([model_dump(record) for record in archive_records], indent=2),
            encoding="utf-8",
        )
        validation_history_path = self.workspace / "validation_history.json"
        validation_history_path.write_text(json.dumps(self.validation_history, indent=2), encoding="utf-8")
        stage_failures_path = self.workspace / "stage_failures.json"
        stage_failures = []
        for row in self.history:
            failures = [model_dump(stage) for stage in row.stage_results if not stage.passed]
            if not failures:
                continue
            stage_failures.append(
                {
                    "step": row.step,
                    "objective": row.objective,
                    "scope": row.scope,
                    "child_runtime_hash": row.child_runtime_hash,
                    "failures": failures,
                }
            )
        stage_failures_path.write_text(json.dumps(stage_failures, indent=2), encoding="utf-8")
        best_train = max((record.entry.scores.get("sbar:global", float("-inf")) for record in self.archive.island("sbar:global")), default=float("-inf"))
        return EvolutionSummary(
            steps=steps,
            accepted=accepted,
            archive_cells=len(self.archive.cells),
            best_train_score=best_train,
            best_val_score=self.best_val_score,
            history_path=str(history_path),
            archive_index_path=str(archive_index_path),
            validation_history_path=str(validation_history_path),
            stage_failures_path=str(stage_failures_path),
        )
