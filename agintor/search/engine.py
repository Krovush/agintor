from __future__ import annotations

import json
import random
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from ..storage.artifacts import ArtifactMode, ArtifactPolicy
from ..search.archive import PHASE_SCOPES, QualityDiversityArchive, ScopeScheduler, objective_specs_from_oracle_package, objective_specs_from_suite
from ..evaluation.benchmarks import BenchmarkSuite
from ..search.crossover import crossover_runtime
from ..evaluation.evaluator import RuntimeEvaluator
from ..search.mutators import HeuristicPatchMutator, MutationContext, ProviderPatchMutator
from ..search.spec_mutator import HeuristicSpecActionMutator, ProviderSpecActionMutator, SpecMutationContext
from ..learning.predictors import DecisionFamilyModelBank
from ..providers import ModelProvider
from ..factory.prompt_builder import METHOD_CONTRACTS
from ..runtime.loader import load_runtime
from ..runtime.profile import RuntimeProfile, load_runtime_profile, resolve_runtime_profile
from ..contracts import EvolutionHistoryRow, ObjectiveSpec, OpenAITraceContext, PromotionDecision, decision_attr, decision_type_value
from ..learning.observations import extract_predictor_observations
from ..utils import ensure_directory, mean, stable_hash


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
    evidence_ledger_path: str = ""
    paired_comparisons_path: str = ""
    promotion_ledger_path: str = ""
    signal_sufficiency_path: str = ""
    promotion_counts: dict[str, int] = field(default_factory=dict)
    decision_counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class PromotionRoute:
    archive_name: str | None
    insert_archive: bool
    scheduler_credit_kind: str | None
    predictor_family_prefix: str | None
    updates_capability_priors: bool


PROGRESS_PARENT_ARCHIVE_KINDS = ("capability", "efficiency", "subskill", "preference")
PROGRESS_CREDIT_DECISIONS = {"capability", "subskill"}
PROGRESS_COUNTERFACTUAL_DECISIONS = {"capability", "subskill"}
PROMOTING_DECISIONS = {"capability", "efficiency", "subskill", "preference"}


def _decision_updates(decision: PromotionDecision | Mapping[str, Any] | None, name: str) -> set[str]:
    return {
        str(getattr(update, "value", update))
        for update in (decision_attr(decision, name, []) or [])
    }


def _update_allowed(decision: PromotionDecision | Mapping[str, Any] | None, update: str) -> bool:
    allowed = _decision_updates(decision, "allowed_optimizer_updates")
    forbidden = _decision_updates(decision, "forbidden_optimizer_updates")
    return update in allowed and update not in forbidden


def _predictor_updates_allowed(decision: PromotionDecision | Mapping[str, Any] | None) -> bool:
    decision_type = decision_type_value(decision)
    if decision_type == "capability":
        return _update_allowed(decision, "capability_predictors")
    if decision_type == "efficiency":
        return _update_allowed(decision, "efficiency_predictors")
    if decision_type == "preference":
        return _update_allowed(decision, "preference_model")
    if decision_type == "subskill":
        return _update_allowed(decision, "subskill_predictors")
    if decision_type == "reject":
        return _update_allowed(decision, "hard_failure_stats") or _update_allowed(decision, "diagnostic_predictors")
    if decision_type in {"abstain", "no_progress"}:
        return _update_allowed(decision, "diagnostic_predictors")
    if decision_type == "quarantine":
        return _update_allowed(decision, "hard_failure_stats") or _update_allowed(decision, "diagnostic_predictors")
    return False


def route_promotion_decision(decision: PromotionDecision | Mapping[str, Any] | None) -> PromotionRoute:
    decision_type = decision_type_value(decision)
    if decision_type == "capability":
        return PromotionRoute(
            "capability",
            _update_allowed(decision, "capability_archive"),
            "capability" if _update_allowed(decision, "capability_scheduler") else None,
            "capability" if _update_allowed(decision, "capability_predictors") else None,
            _update_allowed(decision, "capability_priors"),
        )
    if decision_type == "efficiency":
        return PromotionRoute(
            "efficiency",
            _update_allowed(decision, "efficiency_archive"),
            "efficiency" if _update_allowed(decision, "efficiency_predictors") else None,
            "efficiency" if _update_allowed(decision, "efficiency_predictors") else None,
            False,
        )
    if decision_type == "subskill":
        return PromotionRoute(
            "subskill",
            _update_allowed(decision, "subskill_archive"),
            "subskill" if _update_allowed(decision, "subskill_scheduler") else None,
            None,
            False,
        )
    if decision_type == "preference":
        return PromotionRoute(
            "preference",
            _update_allowed(decision, "preference_archive"),
            None,
            "preference" if _update_allowed(decision, "preference_model") else None,
            False,
        )
    return PromotionRoute(None, False, None, None, False)


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
        trace_context: OpenAITraceContext | None = None,
        oracle_package: Any | None = None,
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
        self.trace_context = trace_context
        self.predictors = DecisionFamilyModelBank()
        self.archive = QualityDiversityArchive()
        self.scheduler = ScopeScheduler()
        baseline_runtime = self._load_runtime(self.baseline_runtime_dir)
        self._baseline_manifest = baseline_runtime.manifest
        runtime_kind = str(getattr(self._baseline_manifest, "runtime_kind", "policy_modules") or "policy_modules")
        self.spec_backed = runtime_kind in {
            "langgraph_spec",
            "tradingagents_langgraph",
        }
        if oracle_package is not None and not self.spec_backed:
            raise ValueError(
                f"oracle package scoring requires a spec-backed runtime; runtime_kind={runtime_kind!r} is not supported"
            )
        self.oracle_package = oracle_package
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
            trace_context=trace_context,
            oracle_package=oracle_package,
        )
        self.oracle_package = self.evaluator.oracle_package
        self.objectives = (
            objective_specs_from_oracle_package(self.oracle_package, partition="train")
            if self.oracle_package is not None
            else objective_specs_from_suite(suite, partition="train")
        )
        if not self.objectives:
            raise ValueError("oracle package produced no search objectives")
        self.objective_ids = {spec.name for spec in self.objectives}
        self.history: list[EvolutionHistoryRow] = []
        normalized_mutator = mutator_type.strip().lower()
        if normalized_mutator in {"heuristic-spec", "spec", "heuristic"} and self.spec_backed:
            self.mutator = HeuristicSpecActionMutator()
        elif normalized_mutator in {"provider-spec", "openai-spec", "provider"} and self.spec_backed:
            if getattr(provider, "provider_name", "local") == "local":
                raise ValueError("provider spec mutator requires a hosted provider, not the local deterministic provider")
            self.mutator = ProviderSpecActionMutator(provider)
        elif normalized_mutator == "heuristic":
            self.mutator = HeuristicPatchMutator()
        elif normalized_mutator in {"provider", "openai"}:
            if getattr(provider, "provider_name", "local") == "local":
                raise ValueError("provider mutator requires a hosted provider, not the local deterministic provider")
            self.mutator = ProviderPatchMutator(provider)
        else:
            raise ValueError(f"unknown mutator_type {mutator_type}")
        self.best_val_score = float("-inf")
        self.validation_history: list[dict[str, Any]] = []
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

    def _active_objective_ids(self) -> list[str]:
        return [spec.name for spec in self.objectives]

    def _validation_objective_name(self) -> str:
        return "sbar:global" if "sbar:global" in self.objective_ids else self.objectives[0].name

    def evaluate_validation_for_objective(self, runtime_dir: Path, objective_name: str | None = None):
        objective_name = objective_name or self._validation_objective_name()
        if getattr(self, "oracle_package", None) is None:
            return self.evaluator.evaluate_validation(runtime_dir)
        objective = next((spec for spec in self.objectives if spec.name == objective_name), self.objectives[0])
        tasks = self.evaluator._objective_subset(objective)
        return self.evaluator.evaluate_runtime(
            runtime_dir,
            partition="train",
            seeds=self.runtime_profile.evaluation.validation_seeds,
            tasks_override=tasks,
        )

    def _evaluation_objectives_aligned(self, evaluation) -> bool:
        if getattr(self, "oracle_package", None) is None:
            return True
        return set(self._active_objective_ids()).issubset(set(evaluation.objective_scores))

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
        runtime_kind = str(getattr(runtime.manifest, "runtime_kind", "policy_modules") or "policy_modules")
        if runtime_kind in {"langgraph_spec", "tradingagents_langgraph"}:
            baseline_spec = getattr(self._load_runtime(self.baseline_runtime_dir), "runtime_spec", None)
            runtime_spec = getattr(runtime, "runtime_spec", None)
            if baseline_spec is None or runtime_spec is None:
                return "0000"
            return "0000" if baseline_spec.spec_digest == runtime_spec.spec_digest else "1111"
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
            objectives=self._active_objective_ids() if getattr(self, "oracle_package", None) is not None else None,
            oracle_package_hash=str(
                getattr(getattr(self, "oracle_package", None), "package_hash", "")
                or getattr(baseline_runtime.manifest, "oracle_package_hash", "")
                or ""
            ),
            runtime_spec_digest=str(getattr(baseline_runtime.manifest, "runtime_spec_digest", "") or ""),
        )

    def _objective_by_name(self, name: str) -> ObjectiveSpec:
        return next(spec for spec in self.objectives if spec.name == name)

    def _archive_objectives_for_promotion(
        self,
        evaluation,
        promotion_decision: PromotionDecision | Mapping[str, Any] | None,
        objective: ObjectiveSpec,
    ) -> list[str]:
        available = set(evaluation.objective_scores)
        if getattr(self, "oracle_package", None) is not None:
            oracle_available = available & self.objective_ids
            if promotion_decision is None:
                return [name for name in self._active_objective_ids() if name in oracle_available]
            progress_signal = decision_attr(promotion_decision, "progress_signal")
            improved_axes = {
                str(axis).split("task:", 1)[1] if str(axis).startswith("task:") else str(axis)
                for axis in (decision_attr(progress_signal, "improved_axes", []) or [])
            }
            objectives: set[str] = set()
            for axis in improved_axes:
                axis_objective = f"axis:{axis}"
                if axis_objective in oracle_available:
                    objectives.add(axis_objective)
            for comparison in decision_attr(progress_signal, "pairwise_comparisons", []) or []:
                axis_task_ids = dict(decision_attr(comparison, "axis_task_ids", {}) or {})
                for axis in improved_axes:
                    for task_id in axis_task_ids.get(axis, []):
                        task_objective = f"s:{task_id}"
                        if task_objective in oracle_available:
                            objectives.add(task_objective)
            if objective.name in oracle_available:
                objectives.add(objective.name)
            return [name for name in self._active_objective_ids() if name in objectives]
        if promotion_decision is None:
            return sorted(available)
        decision_type = decision_type_value(promotion_decision)
        progress_signal = decision_attr(promotion_decision, "progress_signal")
        improved_axes = {
            str(axis).split("task:", 1)[1] if str(axis).startswith("task:") else str(axis)
            for axis in (decision_attr(progress_signal, "improved_axes", []) or [])
        }
        matched_task_ids: set[str] = set()
        for comparison in decision_attr(progress_signal, "pairwise_comparisons", []) or []:
            axis_task_ids = dict(decision_attr(comparison, "axis_task_ids", {}) or {})
            for axis in improved_axes:
                matched_task_ids.update(str(task_id) for task_id in axis_task_ids.get(axis, []))
        objectives: set[str] = set()
        for axis in improved_axes:
            task_objective = f"s:{axis}"
            if task_objective in available:
                matched_task_ids.add(axis)
        for task_id in sorted(matched_task_ids):
            task_objective = f"s:{task_id}"
            if task_objective in available:
                objectives.add(task_objective)
            try:
                family = self.suite.by_id(task_id).family
            except Exception:
                continue
            for family_objective in (f"sbar:{family}", f"rhobar:{family}"):
                if family_objective in available:
                    objectives.add(family_objective)
        if decision_type in PROGRESS_CREDIT_DECISIONS:
            if matched_task_ids:
                objectives.update(name for name in ("sbar:global", "rhobar:global") if name in available)
            elif objective.name in {"sbar:global", "rhobar:global"} and objective.name in available:
                objectives.add(objective.name)
        elif decision_type == "efficiency":
            objectives.update(name for name in ("sbar:global", "rhobar:global") if name in available)
            if objective.name in available:
                objectives.add(objective.name)
        elif objective.name in available:
            objectives.add(objective.name)
        if not objectives:
            objectives.add(objective.name if objective.name in available else "sbar:global")
        return [name for name in sorted(objectives) if name in available]

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
        island = self._progress_island(objective_name)
        exemplars = sorted(island, key=lambda record: record.entry.scores.get(objective_name, float("-inf")), reverse=True)[:limit]
        return [{"runtime_hash": record.entry.runtime_hash, "score": record.entry.scores.get(objective_name), "scope": record.entry.scope_tag} for record in exemplars]

    def _progress_island(self, objective_name: str):
        records = []
        for archive_kind in PROGRESS_PARENT_ARCHIVE_KINDS:
            records.extend(self.archive.island(objective_name, archive_kind=archive_kind))
        return records

    def _validation_tick(self, iteration: int) -> None:
        if iteration % 5 != 0:
            return
        if getattr(self, "oracle_package", None) is not None:
            validation_scores: dict[str, float] = {}
            objective_scores: dict[str, float] = {}
            leaders_by_objective: dict[str, dict[str, str]] = {}
            first_leader = None
            for objective_name in self._active_objective_ids():
                island = self._progress_island(objective_name)
                if not island:
                    validation_scores[objective_name] = float("-inf")
                    objective_scores[objective_name] = float("-inf")
                    continue
                leader = max(island, key=lambda record: record.entry.scores.get(objective_name, float("-inf")))
                first_leader = first_leader or leader
                val_eval = self.evaluate_validation_for_objective(Path(leader.runtime_dir), objective_name)
                objective_scores.update(val_eval.objective_scores)
                objective_score = val_eval.objective_scores.get(objective_name, float("-inf"))
                validation_scores[objective_name] = objective_score
                objective_scores[objective_name] = objective_score
                leaders_by_objective[objective_name] = {
                    "runtime_hash": str(leader.entry.runtime_hash),
                    "runtime_dir": str(leader.runtime_dir),
                }
            if first_leader is None:
                return
            val_score = min(validation_scores.values()) if validation_scores else float("-inf")
            self.validation_history.append(
                {
                    "iteration": iteration,
                    "runtime_hash": first_leader.entry.runtime_hash,
                    "runtime_dir": first_leader.runtime_dir,
                    "objective_scores": objective_scores,
                    "validation_score": val_score,
                    "validation_scores_by_objective": validation_scores,
                    "leaders_by_objective": leaders_by_objective,
                }
            )
        else:
            objective_name = self._validation_objective_name()
            island = self._progress_island(objective_name)
            if not island:
                return
            leader = max(island, key=lambda record: record.entry.scores.get(objective_name, float("-inf")))
            val_eval = self.evaluate_validation_for_objective(Path(leader.runtime_dir), objective_name)
            val_score = val_eval.objective_scores.get(objective_name, float("-inf"))
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
        accepted_scopes = [row.scope for row in self.history if row.accepted and row.promotion_type in PROGRESS_CREDIT_DECISIONS]
        admissible_sizes = {"local": {1}, "pair": {2}, "joint": {3, 4}}.get(self.scheduler.phase, {4})
        order = {name: idx for idx, name in enumerate(["top", "mem", "tool", "ctl"])}
        covered_scopes = {
            tuple(sorted(scope, key=lambda item: order.get(item, 99)))
            for scope in accepted_scopes
            if len(scope) in admissible_sizes
        }
        coverage = len(covered_scopes) / max(1, len(PHASE_SCOPES[self.scheduler.phase]))
        pass_rate = sum(1 for row in self.history[-5:] if row.accepted and row.promotion_type in PROGRESS_CREDIT_DECISIONS) / max(1, len(self.history[-5:]))
        self.scheduler.maybe_advance_phase(improvement, coverage, pass_rate)

    def _predictor_summaries(self) -> dict[str, object]:
        summary = self.predictors.summary()
        summary["phase"] = self.scheduler.phase
        return summary

    def _signal_sufficiency_report(
        self,
        *,
        promotion_counts: Mapping[str, int],
        decision_counts: Mapping[str, int],
    ) -> dict[str, object]:
        stage4_decisions = sum(int(count) for count in decision_counts.values())
        host_verified_promotions = sum(
            1
            for row in self.history
            if row.accepted
            and row.promotion_type in PROMOTING_DECISIONS
            and bool(row.evidence_contract_id)
        )
        held_out_evidence_available = False
        enough_stage4_evidence = host_verified_promotions >= 10
        safe = enough_stage4_evidence and held_out_evidence_available
        reasons: list[str] = []
        if not enough_stage4_evidence:
            reasons.append("insufficient_host_verified_stage4_evidence")
        if not held_out_evidence_available:
            reasons.append("missing_held_out_evidence")
        return {
            "status": "sufficient" if safe else "insufficient",
            "safe_for_predictor_backed_ws5_control": safe,
            "stage4_decision_count": stage4_decisions,
            "host_verified_promotion_count": host_verified_promotions,
            "required_host_verified_promotion_count": 10,
            "held_out_evidence_available": held_out_evidence_available,
            "promotion_counts": dict(promotion_counts),
            "decision_counts": dict(decision_counts),
            "reasons": reasons,
        }

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
        if getattr(self, "spec_backed", False):
            return Path(parent_record.runtime_dir)
        donor_pool = [record for record in self._progress_island(objective_name) if record.entry.runtime_hash != parent_record.entry.runtime_hash]
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

    def _update_predictors(
        self,
        evaluation,
        *,
        promotion_decision: PromotionDecision | Mapping[str, Any] | None,
        retained: bool | None = None,
    ) -> None:
        if getattr(self, "oracle_package", None) is not None:
            task_family_map = {
                oracle_task.public_task().task_id: oracle_task.public_task().family
                for task_set in self.oracle_package.task_sets
                for oracle_task in task_set.tasks
            }
        else:
            task_family_map = {task.task_id: task.family for task in self.suite.train}
        if not _predictor_updates_allowed(promotion_decision):
            return
        for observation in extract_predictor_observations(
            evaluation,
            task_family_map,
            promotion_decision=promotion_decision,
            retained=retained,
        ):
            self.predictors.add_observation(
                observation.family,
                observation.feature_vector,
                probability_label=observation.label_probability,
                positive_label=observation.label_positive_scalar,
                metadata=observation.metadata,
            )
        self.fully_evaluated_since_retrain += 1
        if retained:
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
            parent_record = self.archive.select_parent(objective.name, seed=step, archive_kinds=PROGRESS_PARENT_ARCHIVE_KINDS)
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
            mutation_action_ids: list[str] = []
            if getattr(self, "spec_backed", False):
                spec_context = SpecMutationContext(
                    objective=objective.name,
                    touched_scope=scope,
                    runtime_dir=parent_dir,
                    workspace=self.workspace / "candidates",
                    seed=step,
                    predictor_summaries=self._predictor_summaries(),
                    failing_train_traces=self._failing_train_traces(parent_eval),
                    exemplars=self._exemplars(objective.name),
                    oracle_package_hash=str(parent_record.entry.oracle_package_hash or ""),
                    evidence_digest=str(parent_record.entry.evidence_digest or ""),
                )
                spec_candidate = self.mutator.mutate(spec_context)
                mutation_action_ids = [action.action_id for action in spec_candidate.actions]
                stage_results, child_dir = self.evaluator.staged_evaluate_runtime_pair(
                    parent_dir,
                    spec_candidate.child_runtime_dir,
                    objective,
                    scope=scope,
                    mutation_action_ids=mutation_action_ids,
                )
            else:
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
                    trace_context=self.trace_context,
                )
                candidate = self.mutator.mutate(context)
                stage_results, child_dir = self.evaluator.staged_evaluate(parent_dir, candidate, objective)
            self._record_stage_pass_rates(stage_results)
            inserted_keys: list[str] = []
            child_hash = None
            accepted_flag = False
            promotion_decision = None
            decision_type = None
            stage4 = self._stage4_result(stage_results)
            if stage4 is not None:
                promotion_decision = stage4.promotion_decision
                decision_type = decision_type_value(promotion_decision)
            if (
                child_dir is not None
                and stage4 is not None
                and stage4.passed
                and stage4.suite_evaluation is not None
                and not stage4.suite_evaluation.invalid
            ):
                if not self._evaluation_objectives_aligned(stage4.suite_evaluation):
                    self.scheduler.note_hard_failure(scope)
                    self._cleanup_path(child_dir, failed=True)
                    accepted_flag = False
                    if parent_dir != Path(parent_record.runtime_dir):
                        self._cleanup_path(parent_dir)
                    progress_signal = decision_attr(promotion_decision, "progress_signal")
                    row = EvolutionHistoryRow(
                        step=step,
                        objective=objective.name,
                        parent_runtime_hash=parent_record.entry.runtime_hash,
                        child_runtime_hash=None,
                        scope=scope,
                        stage_results=stage_results,
                        accepted=False,
                        inserted_keys=[],
                        promotion_type="quarantine",
                        promotion_decision_ref=decision_attr(promotion_decision, "decision_id"),
                        progress_signal_ref=decision_attr(promotion_decision, "progress_signal_ref"),
                        evidence_contract_id=str(decision_attr(promotion_decision, "contract_id", "") or ""),
                        evidence_digest=str(decision_attr(promotion_decision, "evidence_digest", "") or ""),
                        oracle_package_hash=str(decision_attr(promotion_decision, "oracle_package_hash", "") or ""),
                        runtime_spec_digest=str(decision_attr(promotion_decision, "child_runtime_spec_digest", "") or ""),
                        mutation_action_ids=mutation_action_ids,
                        allowed_optimizer_updates=[],
                        forbidden_optimizer_updates=["capability_archive", "capability_scheduler", "capability_predictors", "capability_priors"],
                        improved_axes=list(decision_attr(progress_signal, "improved_axes", []) or []),
                        regressed_axes=list(decision_attr(progress_signal, "regressed_axes", []) or []),
                        tied_axes=list(decision_attr(progress_signal, "tied_axes", []) or []),
                    )
                    self.history.append(row)
                    self.scheduler.note_iteration([])
                    self._validation_tick(step)
                    continue
                child_runtime = self._load_runtime(child_dir)
                child_hash = child_runtime.runtime_hash
                route = route_promotion_decision(promotion_decision)
                if route.scheduler_credit_kind in PROGRESS_CREDIT_DECISIONS:
                    delta = float(decision_attr(promotion_decision, "quality_delta_lower", 0.0) or 0.0)
                    self.scheduler.update_scope_credit(objective.name, scope, delta / max(1, len(scope)))
                if route.insert_archive and route.archive_name is not None:
                    inserted_keys = self.archive.insert(
                        str(child_dir),
                        child_runtime.runtime_hash,
                        child_runtime.code_hash,
                        child_runtime.mutable_loc,
                        stage4.suite_evaluation,
                        scope=scope,
                        mutable_ast_nodes=child_runtime.mutable_ast_nodes,
                        interface_diff_mask=self._interface_diff_mask(child_dir),
                        archive_kind=route.archive_name,
                        promotion_decision=promotion_decision,
                        objectives=self._archive_objectives_for_promotion(stage4.suite_evaluation, promotion_decision, objective),
                        oracle_package_hash=str(decision_attr(promotion_decision, "oracle_package_hash", "") or ""),
                        runtime_spec_digest=str(decision_attr(promotion_decision, "child_runtime_spec_digest", "") or ""),
                        mutation_action_ids=mutation_action_ids,
                    )
                    accepted_flag = bool(inserted_keys)
                if decision_type in {"reject", "quarantine"}:
                    self.scheduler.note_hard_failure(scope)
                if accepted_flag:
                    accepted += 1
                    if decision_type in PROGRESS_COUNTERFACTUAL_DECISIONS and not getattr(self, "spec_backed", False):
                        singleton, pairwise = self._counterfactual_contributions(parent_dir, child_dir, scope)
                        self.scheduler.update_counterfactuals(scope, singleton, pairwise)
                else:
                    self._cleanup_path(child_dir)
                if promotion_decision is not None:
                    self._update_predictors(stage4.suite_evaluation, promotion_decision=promotion_decision, retained=accepted_flag)
            elif self._is_hard_failure(stage_results):
                if child_dir is not None:
                    try:
                        child_hash = self._load_runtime(child_dir).runtime_hash
                    except Exception:
                        child_hash = None
                self.scheduler.note_hard_failure(scope)
                if stage4 is not None and stage4.suite_evaluation is not None and promotion_decision is not None:
                    self._update_predictors(stage4.suite_evaluation, promotion_decision=promotion_decision, retained=False)
            elif child_dir is not None:
                self._cleanup_path(child_dir)
                if decision_type in {"reject", "quarantine"}:
                    self.scheduler.note_hard_failure(scope)
                if stage4 is not None and stage4.suite_evaluation is not None and promotion_decision is not None:
                    self._update_predictors(stage4.suite_evaluation, promotion_decision=promotion_decision, retained=False)
            if parent_dir != Path(parent_record.runtime_dir):
                self._cleanup_path(parent_dir)
            progress_signal = decision_attr(promotion_decision, "progress_signal")
            row = EvolutionHistoryRow(
                step=step,
                objective=objective.name,
                parent_runtime_hash=parent_record.entry.runtime_hash,
                child_runtime_hash=child_hash,
                scope=scope,
                stage_results=stage_results,
                accepted=accepted_flag,
                inserted_keys=inserted_keys,
                promotion_type=decision_type,
                promotion_decision_ref=decision_attr(promotion_decision, "decision_id"),
                progress_signal_ref=decision_attr(promotion_decision, "progress_signal_ref"),
                evidence_contract_id=str(decision_attr(promotion_decision, "contract_id", "") or ""),
                evidence_digest=str(decision_attr(promotion_decision, "evidence_digest", "") or ""),
                oracle_package_hash=str(decision_attr(promotion_decision, "oracle_package_hash", "") or ""),
                runtime_spec_digest=str(decision_attr(promotion_decision, "child_runtime_spec_digest", "") or ""),
                mutation_action_ids=mutation_action_ids,
                allowed_optimizer_updates=list(decision_attr(promotion_decision, "allowed_optimizer_updates", []) or []),
                forbidden_optimizer_updates=list(decision_attr(promotion_decision, "forbidden_optimizer_updates", []) or []),
                improved_axes=list(decision_attr(progress_signal, "improved_axes", []) or []),
                regressed_axes=list(decision_attr(progress_signal, "regressed_axes", []) or []),
                tied_axes=list(decision_attr(progress_signal, "tied_axes", []) or []),
            )
            self.history.append(row)
            capability_iteration_scopes = [row.scope] if row.accepted and row.promotion_type in PROGRESS_CREDIT_DECISIONS else []
            self.scheduler.note_iteration(capability_iteration_scopes)
            self._validation_tick(step)
        history_path = self.workspace / "evolution_history.json"
        history_path.write_text(json.dumps([(row).model_dump() for row in self.history], indent=2), encoding="utf-8")
        archive_index_path = self.workspace / "archive_index.json"
        archive_records = sorted(
            self.archive.archive_records(),
            key=lambda record: (record.archive_kind, record.objective, record.key),
        )
        archive_index_path.write_text(
            json.dumps([(record).model_dump() for record in archive_records], indent=2),
            encoding="utf-8",
        )
        validation_history_path = self.workspace / "validation_history.json"
        validation_history_path.write_text(json.dumps(self.validation_history, indent=2), encoding="utf-8")
        stage_failures_path = self.workspace / "stage_failures.json"
        stage_failures = []
        for row in self.history:
            failures = [(stage).model_dump() for stage in row.stage_results if not stage.passed]
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
        promotion_counts: dict[str, int] = {}
        decision_counts: dict[str, int] = {}
        for row in self.history:
            if row.promotion_type:
                decision_counts[row.promotion_type] = decision_counts.get(row.promotion_type, 0) + 1
                if row.accepted and row.promotion_type in PROMOTING_DECISIONS:
                    promotion_counts[row.promotion_type] = promotion_counts.get(row.promotion_type, 0) + 1
        signal_sufficiency_path = self.workspace / "signal_sufficiency.json"
        signal_sufficiency_path.write_text(
            json.dumps(
                self._signal_sufficiency_report(
                    promotion_counts=promotion_counts,
                    decision_counts=decision_counts,
                ),
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        best_objective = self._validation_objective_name()
        best_train = max((record.entry.scores.get(best_objective, float("-inf")) for record in self._progress_island(best_objective)), default=float("-inf"))
        return EvolutionSummary(
            steps=steps,
            accepted=accepted,
            archive_cells=len(self.archive.archive_records()),
            best_train_score=best_train,
            best_val_score=self.best_val_score,
            history_path=str(history_path),
            archive_index_path=str(archive_index_path),
            validation_history_path=str(validation_history_path),
            stage_failures_path=str(stage_failures_path),
            evidence_ledger_path=str(self.evaluator.evidence_ledger_path),
            paired_comparisons_path=str(self.evaluator.paired_comparison_ledger_path),
            promotion_ledger_path=str(self.evaluator.promotion_ledger_path),
            signal_sufficiency_path=str(signal_sufficiency_path),
            promotion_counts=promotion_counts,
            decision_counts=decision_counts,
        )
