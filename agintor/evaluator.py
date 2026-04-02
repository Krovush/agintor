from __future__ import annotations

import ast
import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence
import inspect

from .artifacts import ArtifactMode, ArtifactPolicy
from .archive import objective_specs_from_suite
from .benchmarks import BenchmarkSuite
from .container_runtime import DockerRuntimeExecutor
from .exceptions import HardInvalidation, PatchApplyError
from .mutator import MutationCandidate
from .patches import parse_patch
from .predictors import DecisionFamilyModelBank
from .prompt_builder import METHOD_CONTRACTS
from .providers import ModelProvider
from .runtime_loader import load_runtime
from .runtime_profile import RuntimeProfile, resolve_runtime_profile
from .runner import TaskRuntime
from .pydantic_compat import model_dump
from .scoring import ScoreCalculator, estimate_reference_scales, mean_improvement
from .schemas import EvaluationStageResult, ObjectiveKind, ObjectiveSpec, SuiteEvaluation
from .shell import FixedShell
from .utils import ensure_directory, stable_hash


class RuntimeEvaluator:
    def __init__(
        self,
        suite: BenchmarkSuite,
        workspace: Path,
        provider: ModelProvider,
        baseline_runtime_dir: Path | None = None,
        budget_overrides: Mapping[str, Any] | None = None,
        predictors: DecisionFamilyModelBank | None = None,
        runtime_backend: str | None = None,
        runtime_profile: RuntimeProfile | None = None,
        profile_path: Path | None = None,
        artifact_mode: str | ArtifactMode | None = None,
        sandbox_root: Path | None = None,
        retain_artifacts: bool = False,
    ) -> None:
        self.suite = suite
        self.workspace = Path(workspace)
        self.provider = provider
        self.artifact_policy = ArtifactPolicy.resolve(
            artifact_mode=artifact_mode,
            retain_artifacts=retain_artifacts,
            sandbox_root=sandbox_root,
        )
        self.retain_artifacts = self.artifact_policy.keep_successes
        self.budget_overrides = dict(budget_overrides or {})
        self.predictors = predictors or DecisionFamilyModelBank()
        self.profile_path = Path(profile_path) if profile_path is not None else None
        self._baseline_runtime_dir = Path(baseline_runtime_dir) if baseline_runtime_dir is not None else None
        self._preparing_reference_scales = False
        self._reference_scales_ready = False
        self.reference_profile = runtime_profile or resolve_runtime_profile(
            baseline_runtime_dir,
            profile_path=self.profile_path,
        )
        self.runtime_backend = (runtime_backend or os.environ.get("AGINTOR_RUNTIME_BACKEND", "local")).strip().lower()
        self.container_executor = (
            DockerRuntimeExecutor(
                self.workspace / ".runtime_container_cache",
                artifact_mode=self.artifact_policy.mode,
                sandbox_root=self.artifact_policy.sandbox_root,
                retain_artifacts=retain_artifacts,
            )
            if self.runtime_backend == "docker"
            else None
        )
        self.stage1_replays = self.reference_profile.evaluation.stage1_replays
        self.epsilon_proxy = self.reference_profile.evaluation.epsilon_proxy
        self.epsilon_part = self.reference_profile.evaluation.epsilon_part
        self.epsilon_full = self.reference_profile.evaluation.epsilon_full
        self.stage4_minibatch_size = self.reference_profile.evaluation.stage4_minibatch_size
        self.delta_rej = self.reference_profile.evaluation.delta_rej
        self.cache: dict[tuple[str, str, tuple[int, ...], tuple[str, ...]], SuiteEvaluation] = {}
        self.reference_scales = ({}, {})
 

    def prepare_reference_scales(self, force: bool = False) -> None:
        if self._baseline_runtime_dir is None:
            return
        if self._reference_scales_ready and not force:
            return
        self._preparing_reference_scales = True
        try:
            kwargs = {
                "partition": "train",
                "seeds": self.reference_profile.evaluation.reference_scale_seeds,
                "use_cache": False,
                "use_reference_scales": False,
            }
            try:
                params = inspect.signature(self.evaluate_runtime).parameters
            except (TypeError, ValueError):
                params = {}
            filtered_kwargs = {key: value for key, value in kwargs.items() if key in params}
            baseline_eval = self.evaluate_runtime(self._baseline_runtime_dir, **filtered_kwargs)
        finally:
            self._preparing_reference_scales = False
        self.reference_scales = estimate_reference_scales(baseline_eval.run_results)
        self._reference_scales_ready = True

    def _score_calculator(self, *, use_reference_scales: bool = True) -> ScoreCalculator:
        if (
            use_reference_scales
            and self._baseline_runtime_dir is not None
            and not self._reference_scales_ready
            and not self._preparing_reference_scales
        ):
            self.prepare_reference_scales()
        costs, latencies = self.reference_scales if use_reference_scales and self._reference_scales_ready else ({}, {})
        return ScoreCalculator(
            baseline_costs=costs,
            baseline_latencies=latencies,
            family_weights=self.reference_profile.evaluation.family_weights,
            lambdas=self.reference_profile.evaluation.lambdas,
            robustness=self.reference_profile.evaluation.robustness,
        )

    def _effective_runtime_profile(self, runtime_dir: str | Path) -> RuntimeProfile:
        return resolve_runtime_profile(
            runtime_dir,
            fallback_profile=self.reference_profile,
            profile_path=self.profile_path,
        )

    def _load_runtime(self, runtime_dir: str | Path, *, runtime_profile: RuntimeProfile | None = None):
        return load_runtime(
            runtime_dir,
            runtime_profile=runtime_profile or self._effective_runtime_profile(runtime_dir),
            runtime_backend=self.runtime_backend,
        )

    def _evaluation_units(self, tasks: Sequence[Any]) -> list[list[Any]]:
        units: list[list[Any]] = []
        episodes: dict[str, list[Any]] = {}
        for task in tasks:
            episode_id = getattr(task, "episode_id", None)
            if getattr(task, "transfer_scored", False) and episode_id:
                if episode_id not in episodes:
                    episodes[episode_id] = []
                    units.append(episodes[episode_id])
                episodes[episode_id].append(task)
                continue
            units.append([task])
        for unit in units:
            if len(unit) > 1 and getattr(unit[0], "episode_id", None):
                unit.sort(key=lambda task: (getattr(task, "episode_order", 0), task.task_id))
        return units

    def _normalize_trace_payload(self, value: Any) -> Any:
        volatile_keys = {
            "dollar_cost",
            "handle_id",
            "latency_s",
            "launch_time",
            "node_id",
            "process_pid",
            "raw_id",
            "stderr_path",
            "stdout_path",
        }
        if isinstance(value, dict):
            return {
                key: self._normalize_trace_payload(item)
                for key, item in sorted(value.items())
                if key not in volatile_keys
            }
        if isinstance(value, list):
            return [self._normalize_trace_payload(item) for item in value]
        return value

    def _normalize_trace(self, trace: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self._normalize_trace_payload(event) for event in trace]

    def _trace_rows(self, run) -> list[dict[str, Any]]:
        return run.trace_rows() if hasattr(run, "trace_rows") else []

    def _cleanup_path(self, path: Path | None, *, failed: bool = False) -> None:
        if path is None or not path.exists():
            return
        if failed and self.artifact_policy.keep_failures:
            return
        if not failed and self.artifact_policy.keep_successes:
            return
        shutil.rmtree(path, ignore_errors=True)

    def _file_contract_snapshot(self, source: str, allowed_methods: Sequence[str]) -> tuple[tuple[str, ...], dict[str, dict[str, str]]]:
        tree = ast.parse(source)
        top_level: list[str] = []
        class_contracts: dict[str, dict[str, str]] = {}
        allowed = set(allowed_methods)
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                top_level.append(f"class:{node.name}")
                class_snapshot: dict[str, str] = {}
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if item.name not in allowed:
                            class_snapshot[f"method:{item.name}"] = ast.dump(item, include_attributes=False)
                    else:
                        class_snapshot[f"node:{ast.dump(item, include_attributes=False)}"] = ast.dump(item, include_attributes=False)
                class_contracts[node.name] = class_snapshot
            else:
                top_level.append(ast.dump(node, include_attributes=False))
        return tuple(top_level), class_contracts

    def _ensure_only_allowed_methods_changed(self, parent_source: str, child_source: str, allowed_methods: Sequence[str]) -> None:
        parent_snapshot = self._file_contract_snapshot(parent_source, allowed_methods)
        child_snapshot = self._file_contract_snapshot(child_source, allowed_methods)
        if parent_snapshot != child_snapshot:
            raise PatchApplyError("patch touched lines outside contracted mutable method boundaries")

    def _train_batches(self) -> list[list[Any]]:
        units = self._evaluation_units(list(self.suite.train))
        batch_size = max(1, self.stage4_minibatch_size)
        batches: list[list[Any]] = []
        current: list[Any] = []
        current_size = 0
        for unit in units:
            unit_size = len(unit)
            if current and current_size + unit_size > batch_size:
                batches.append(current)
                current = []
                current_size = 0
            current.extend(unit)
            current_size += unit_size
            if current_size >= batch_size:
                batches.append(current)
                current = []
                current_size = 0
        if current:
            batches.append(current)
        return batches

    def tighten_thresholds(self, stage_name: str) -> None:
        if stage_name == "stage1":
            self.stage1_replays = min(4, self.stage1_replays + 1)
            return
        if stage_name == "stage2":
            self.epsilon_proxy = max(0.0, self.epsilon_proxy - 0.0025)
            return
        if stage_name == "stage3":
            self.epsilon_part = max(0.0, self.epsilon_part - 0.0025)

    def evaluate_runtime(
        self,
        runtime_dir: str | Path,
        partition: str = "train",
        seeds: Sequence[int] = (0, 1, 2),
        use_cache: bool = True,
        tasks_override: Sequence[Any] | None = None,
        *,
        use_reference_scales: bool = True,
    ) -> SuiteEvaluation:
        runtime_profile = self._effective_runtime_profile(runtime_dir)
        runtime = self._load_runtime(runtime_dir, runtime_profile=runtime_profile)
        task_key = ()
        if tasks_override is not None:
            task_key = tuple(stable_hash(model_dump(task)) for task in tasks_override)
        cache_key = (runtime.runtime_hash, partition, tuple(seeds), task_key)
        if use_cache and cache_key in self.cache:
            return self.cache[cache_key]
        tasks = list(tasks_override) if tasks_override is not None else self.suite.all_tasks(partition)
        units = self._evaluation_units(tasks)
        run_results = []
        shell_workspaces: list[Path] = []
        self.predictors.freeze()
        try:
            if self.runtime_backend == "docker" and self.container_executor is not None:
                task_runs = [
                    (task, int(seed))
                    for seed in seeds
                    for unit in units
                    for task in unit
                ]
                run_results.extend(
                    self.container_executor.run_batch(
                        runtime_dir,
                        task_runs,
                        provider=self.provider,
                        runtime_profile=runtime_profile,
                    )
                )
            else:
                for seed in seeds:
                    shell_workspace = self.workspace / f"ev_{runtime.runtime_hash[:8]}_{partition[:1]}_{seed}"
                    shell_workspaces.append(shell_workspace)
                    shell = FixedShell(
                        shell_workspace,
                        predictors=self.predictors,
                        artifact_mode=self.artifact_policy.mode,
                        sandbox_root=self.artifact_policy.sandbox_root,
                        retain_artifacts=self.retain_artifacts,
                    )
                    runner = TaskRuntime(
                        runtime,
                        shell,
                        self.provider,
                        budget_overrides=self.budget_overrides,
                        runtime_profile=runtime_profile,
                    )
                    for unit in units:
                        for task in unit:
                            run_results.append(runner.run_task(task, int(seed)))
        except Exception:
            for shell_workspace in shell_workspaces:
                self._cleanup_path(shell_workspace, failed=True)
            raise
        finally:
            self.predictors.unfreeze()
        task_family_map = {task.task_id: task.family for task in tasks}
        evaluation = self._score_calculator(use_reference_scales=use_reference_scales).suite_score(
            runtime.runtime_hash,
            task_family_map,
            run_results,
        )
        if not evaluation.invalid:
            for shell_workspace in shell_workspaces:
                self._cleanup_path(shell_workspace)
        if use_cache:
            self.cache[cache_key] = evaluation
        return evaluation

    def _apply_patch_uniquely(self, parent_dir: Path, candidate: MutationCandidate) -> Path:
        runtime = self._load_runtime(parent_dir)
        child_dir = ensure_directory(self.workspace / f"patched_{stable_hash(parent_dir, candidate.patch_text)[:10]}")
        if child_dir.exists():
            shutil.rmtree(child_dir)
        shutil.copytree(parent_dir, child_dir)
        allowed_files = {
            runtime.manifest.policy_modules[scope].split(":", 1)[0]
            for scope in candidate.touched_scope
            if scope in runtime.manifest.policy_modules
        }
        allowed_methods_by_file = {
            runtime.manifest.policy_modules[scope].split(":", 1)[0]: set(METHOD_CONTRACTS.get(scope, []))
            for scope in candidate.touched_scope
            if scope in runtime.manifest.policy_modules
        }
        blocks = parse_patch(candidate.patch_text)
        touched_files: set[str] = set()
        for block in blocks:
            matches = []
            for rel_path in runtime.manifest.mutable_files:
                if allowed_files and rel_path not in allowed_files:
                    continue
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
            rel_path = str(path.relative_to(child_dir)).replace("\\", "/")
            source = path.read_text(encoding="utf-8")
            self._ensure_patch_within_mutable_methods(source, block.search, allowed_methods_by_file.get(rel_path, set()))
            path.write_text(source.replace(block.search, block.replace, 1), encoding="utf-8")
            touched_files.add(rel_path)
        for rel_path in touched_files:
            allowed_methods = allowed_methods_by_file.get(rel_path, set())
            parent_source = (parent_dir / rel_path).read_text(encoding="utf-8")
            child_source = (child_dir / rel_path).read_text(encoding="utf-8")
            self._ensure_only_allowed_methods_changed(parent_source, child_source, allowed_methods)
        return child_dir

    def _patch_stats(self, patch_text: str) -> dict[str, int]:
        blocks = parse_patch(patch_text)
        changed_lines = 0
        for block in blocks:
            search_lines = [line for line in block.search.splitlines() if line.strip()]
            replace_lines = [line for line in block.replace.splitlines() if line.strip()]
            changed_lines += max(len(search_lines), len(replace_lines))
        return {"blocks": len(blocks), "changed_lines": changed_lines}

    def _method_ranges(self, source: str, method_names: Sequence[str]) -> list[tuple[int, int]]:
        tree = ast.parse(source)
        ranges: list[tuple[int, int]] = []
        allowed_names = set(method_names)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in allowed_names:
                end_lineno = getattr(node, "end_lineno", node.lineno)
                ranges.append((node.lineno, end_lineno))
        return ranges

    def _ensure_patch_within_mutable_methods(self, source: str, search_text: str, allowed_methods: Sequence[str]) -> None:
        if not allowed_methods:
            raise PatchApplyError("patch touched lines outside contracted mutable method boundaries")
        start = source.find(search_text)
        if start < 0:
            raise PatchApplyError("SEARCH block not found")
        start_line = source[:start].count("\n") + 1
        end_line = start_line + search_text.count("\n")
        for method_start, method_end in self._method_ranges(source, allowed_methods):
            if method_start <= start_line and end_line <= method_end:
                return
        raise PatchApplyError("patch touched lines outside contracted mutable method boundaries")

    def _allowed_methods_by_file(self, runtime, touched_scope: Sequence[str]) -> dict[str, set[str]]:
        return {
            runtime.manifest.policy_modules[scope].split(":", 1)[0]: set(METHOD_CONTRACTS.get(scope, []))
            for scope in touched_scope
            if scope in runtime.manifest.policy_modules
        }

    def stage0_patch_integrity(self, parent_dir: Path, candidate: MutationCandidate) -> tuple[EvaluationStageResult, Path | None]:
        try:
            stats = self._patch_stats(candidate.patch_text)
            if stats["blocks"] > 4:
                raise PatchApplyError("patch exceeded max block count")
            if stats["changed_lines"] > 60:
                raise PatchApplyError("patch exceeded max changed lines")
            if any(len(block.search.splitlines()) > 8 for block in parse_patch(candidate.patch_text)):
                raise PatchApplyError("SEARCH block exceeded max 8 lines")
            child_dir = self._apply_patch_uniquely(parent_dir, candidate)
            runtime = self._load_runtime(child_dir)
            parent_runtime = self._load_runtime(parent_dir)
            allowed_methods_by_file = self._allowed_methods_by_file(parent_runtime, candidate.touched_scope)
            for rel_path in runtime.manifest.mutable_files:
                parent_source = (parent_dir / rel_path).read_text(encoding="utf-8")
                child_source = (child_dir / rel_path).read_text(encoding="utf-8")
                ast.parse(child_source)
                allowed_methods = allowed_methods_by_file.get(rel_path, set())
                if allowed_methods:
                    self._ensure_only_allowed_methods_changed(parent_source, child_source, allowed_methods)
                elif parent_source != child_source:
                    raise PatchApplyError("patch touched lines outside contracted mutable method boundaries")
            return EvaluationStageResult(stage=0, passed=True, reason="patch applied and parsed", metrics=stats), child_dir
        except Exception as exc:
            return EvaluationStageResult(stage=0, passed=False, reason=str(exc)), None

    def stage1_smoke(self, child_dir: Path) -> EvaluationStageResult:
        smoke_task = self.suite.proxy[0]
        runs: list[tuple[SuiteEvaluation, Any, list[dict[str, Any]]]] = []
        for _ in range(max(2, self.stage1_replays)):
            evaluation = self.evaluate_runtime(child_dir, partition="proxy", seeds=[0], use_cache=False, tasks_override=[smoke_task])
            run = evaluation.run_results[0]
            trace = self._normalize_trace(self._trace_rows(run))
            runs.append((evaluation, run, trace))
        baseline_eval, baseline_run, baseline_trace = runs[0]
        passed = True
        for evaluation, run, trace in runs[1:]:
            passed = passed and (
                not baseline_eval.invalid
                and not evaluation.invalid
                and baseline_run.artifact == run.artifact
                and baseline_run.verifier_score == run.verifier_score
                and baseline_run.mode == run.mode
                and baseline_trace == trace
            )
        reason = "deterministic smoke passed" if passed else "smoke task nondeterministic or invalid"
        return EvaluationStageResult(
            stage=1,
            passed=passed,
            reason=reason,
            metrics={"artifact": baseline_run.artifact, "mode": baseline_run.mode, "trace_events": len(baseline_trace)},
        )

    def stage2_proxy(self, parent_dir: Path, child_dir: Path, scope: Sequence[str], epsilon_proxy: float | None = None) -> EvaluationStageResult:
        epsilon_proxy = self.epsilon_proxy if epsilon_proxy is None else epsilon_proxy
        seeds = self.reference_profile.evaluation.proxy_seeds
        proxy_tasks = [task for task in self.suite.proxy if set(task.proxy_scope_tags) & set(scope)]
        if not proxy_tasks:
            proxy_tasks = self.suite.proxy[:1]
        parent_eval = self.evaluate_runtime(parent_dir, partition="proxy", seeds=seeds, tasks_override=proxy_tasks)
        child_eval = self.evaluate_runtime(child_dir, partition="proxy", seeds=seeds, tasks_override=proxy_tasks)
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

    def stage3_local_subset(self, parent_dir: Path, child_dir: Path, objective: ObjectiveSpec, epsilon_part: float | None = None) -> EvaluationStageResult:
        epsilon_part = self.epsilon_part if epsilon_part is None else epsilon_part
        seeds = self.reference_profile.evaluation.subset_seeds
        subset = self._objective_subset(objective)
        parent_eval = self.evaluate_runtime(parent_dir, partition="train", seeds=seeds, tasks_override=subset)
        child_eval = self.evaluate_runtime(child_dir, partition="train", seeds=seeds, tasks_override=subset)
        parent_scores = [parent_eval.objective_scores.get(f"s:{task.task_id}", 0.0) for task in subset]
        child_scores = [child_eval.objective_scores.get(f"s:{task.task_id}", 0.0) for task in subset]
        avg, se, lcb = mean_improvement(child_scores, parent_scores)
        passed = lcb > -epsilon_part and not child_eval.invalid
        return EvaluationStageResult(stage=3, passed=passed, reason="local subset LCB gate", metrics={"delta": avg, "se": se, "lcb": lcb}, suite_evaluation=child_eval)

    def stage4_full(self, parent_dir: Path, child_dir: Path, epsilon_full: float | None = None) -> EvaluationStageResult:
        epsilon_full = self.epsilon_full if epsilon_full is None else epsilon_full
        seeds = self.reference_profile.evaluation.full_train_seeds
        parent_eval = self.evaluate_runtime(parent_dir, partition="train", seeds=seeds)
        task_family_map = {task.task_id: task.family for task in self.suite.train}
        aggregated_runs = []
        parent_scores_accum: list[float] = []
        child_scores_accum: list[float] = []
        for batch in self._train_batches():
            child_batch = self.evaluate_runtime(child_dir, partition="train", seeds=seeds, use_cache=False, tasks_override=batch)
            if child_batch.invalid:
                return EvaluationStageResult(stage=4, passed=False, reason="full train evaluation invalid", metrics={"delta": 0.0, "se": 0.0, "epsilon_full": epsilon_full}, suite_evaluation=child_batch)
            aggregated_runs.extend(child_batch.run_results)
            parent_scores_accum.extend(parent_eval.objective_scores.get(f"s:{task.task_id}", 0.0) for task in batch)
            child_scores_accum.extend(child_batch.objective_scores.get(f"s:{task.task_id}", 0.0) for task in batch)
            avg_batch, se_batch, _ = mean_improvement(child_scores_accum, parent_scores_accum)
            if avg_batch + 1.96 * se_batch < -self.delta_rej:
                return EvaluationStageResult(
                    stage=4,
                    passed=False,
                    reason="stage4 early rejection",
                    metrics={"delta": avg_batch, "se": se_batch, "ucb": avg_batch + 1.96 * se_batch, "delta_rej": self.delta_rej},
                )
        try:
            runtime_hash = self._load_runtime(child_dir).runtime_hash
        except Exception:
            runtime_hash = str(child_dir)
        child_eval = self._score_calculator().suite_score(runtime_hash, task_family_map, aggregated_runs)
        task_ids = [task.task_id for task in self.suite.train]
        parent_scores = [parent_eval.objective_scores.get(f"s:{task_id}", 0.0) for task_id in task_ids]
        child_scores = [child_eval.objective_scores.get(f"s:{task_id}", 0.0) for task_id in task_ids]
        avg, se, lcb = mean_improvement(child_scores, parent_scores)
        passed = (lcb > -epsilon_full) and not child_eval.invalid
        reason = "full train evaluation completed" if passed else "full train evaluation regressed or invalid"
        return EvaluationStageResult(stage=4, passed=passed, reason=reason, metrics={"delta": avg, "se": se, "lcb": lcb, "epsilon_full": epsilon_full}, suite_evaluation=child_eval)

    def evaluate_validation(self, runtime_dir: Path) -> SuiteEvaluation:
        return self.evaluate_runtime(runtime_dir, partition="val", seeds=self.reference_profile.evaluation.validation_seeds)

    def staged_evaluate(self, parent_dir: Path, candidate: MutationCandidate, objective: ObjectiveSpec) -> tuple[list[EvaluationStageResult], Path | None]:
        results: list[EvaluationStageResult] = []
        stage0, child_dir = self.stage0_patch_integrity(parent_dir, candidate)
        results.append(stage0)
        if not stage0.passed or child_dir is None:
            return results, None
        stage1 = self.stage1_smoke(child_dir)
        results.append(stage1)
        if not stage1.passed:
            self._cleanup_path(child_dir, failed=True)
            return results, child_dir
        stage2 = self.stage2_proxy(parent_dir, child_dir, candidate.touched_scope, epsilon_proxy=self.epsilon_proxy)
        results.append(stage2)
        if not stage2.passed:
            self._cleanup_path(child_dir, failed=True)
            return results, child_dir
        stage3 = self.stage3_local_subset(parent_dir, child_dir, objective, epsilon_part=self.epsilon_part)
        results.append(stage3)
        if not stage3.passed:
            self._cleanup_path(child_dir, failed=True)
            return results, child_dir
        stage4 = self.stage4_full(parent_dir, child_dir)
        results.append(stage4)
        if not stage4.passed:
            self._cleanup_path(child_dir, failed=True)
        return results, child_dir
