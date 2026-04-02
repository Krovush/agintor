from __future__ import annotations

import json
from pathlib import Path

import agintor.evaluator as evaluator_module

from agintor.benchmarks import build_demo_suite
from agintor.evaluator import RuntimeEvaluator
from agintor.project import init_runtime
from agintor.providers import LocalDeterministicProvider
from agintor.runtime_loader import load_runtime
from agintor.runtime_profile import RUNTIME_PROFILE_FILE, load_runtime_profile, profile_to_json
from agintor.schemas import RunResult


def test_runtime_identity_changes_when_embedded_profile_changes(runtime_dir: Path, tmp_path: Path) -> None:
    variant_dir = init_runtime(tmp_path / "runtime_variant")
    variant_profile = load_runtime_profile(variant_dir)
    variant_profile.execution.max_steps = 5
    (variant_dir / RUNTIME_PROFILE_FILE).write_text(profile_to_json(variant_profile), encoding="utf-8")

    baseline_runtime = load_runtime(runtime_dir)
    variant_runtime = load_runtime(variant_dir)

    assert baseline_runtime.code_hash != variant_runtime.code_hash
    assert baseline_runtime.runtime_hash != variant_runtime.runtime_hash


def test_runtime_identity_changes_when_profile_override_changes(runtime_dir: Path, tmp_path: Path) -> None:
    override_low = tmp_path / "profile_low.json"
    override_high = tmp_path / "profile_high.json"
    override_low.write_text(json.dumps({"execution": {"max_steps": 5}}), encoding="utf-8")
    override_high.write_text(json.dumps({"execution": {"max_steps": 64}}), encoding="utf-8")

    low_runtime = load_runtime(runtime_dir, profile_path=override_low)
    high_runtime = load_runtime(runtime_dir, profile_path=override_high)

    assert low_runtime.runtime_hash != high_runtime.runtime_hash


def test_runtime_identity_ignores_factory_only_profile_changes(runtime_dir: Path, tmp_path: Path) -> None:
    override_low = tmp_path / "profile_eval_low.json"
    override_high = tmp_path / "profile_eval_high.json"
    override_low.write_text(json.dumps({"evaluation": {"stage1_replays": 2}, "evolution": {"phase_budgets": {"local": 100}}}), encoding="utf-8")
    override_high.write_text(json.dumps({"evaluation": {"stage1_replays": 4}, "evolution": {"phase_budgets": {"local": 900}}}), encoding="utf-8")

    low_runtime = load_runtime(runtime_dir, profile_path=override_low)
    high_runtime = load_runtime(runtime_dir, profile_path=override_high)

    assert low_runtime.code_hash == high_runtime.code_hash
    assert low_runtime.runtime_hash == high_runtime.runtime_hash


def test_legacy_provider_override_still_updates_runtime_provider(runtime_dir: Path, tmp_path: Path) -> None:
    override_path = tmp_path / "profile_legacy_provider.json"
    override_path.write_text(
        json.dumps({"provider": {"name": "openai", "temperature": 0.0, "reasoning_effort_map": {"large": "medium"}}}),
        encoding="utf-8",
    )

    profile = load_runtime_profile(runtime_dir, profile_path=override_path)

    assert profile.runtime_provider.name == "openai"
    assert profile.runtime_provider.temperature == 0.0
    assert profile.runtime_provider.reasoning_effort_map == {"large": "medium"}


def test_evaluator_cache_separates_effective_runtime_profiles(runtime_dir: Path, tmp_path: Path, monkeypatch) -> None:
    suite = build_demo_suite()
    variant_dir = init_runtime(tmp_path / "runtime_variant")
    baseline_profile = load_runtime_profile(runtime_dir)
    variant_profile = load_runtime_profile(variant_dir)
    baseline_profile.execution.max_steps = 64
    variant_profile.execution.max_steps = 5
    (runtime_dir / RUNTIME_PROFILE_FILE).write_text(profile_to_json(baseline_profile), encoding="utf-8")
    (variant_dir / RUNTIME_PROFILE_FILE).write_text(profile_to_json(variant_profile), encoding="utf-8")
    observed_steps: list[int] = []

    class FakeTaskRuntime:
        def __init__(self, runtime, shell, provider, budget_overrides=None, runtime_profile=None) -> None:
            self.runtime_profile = runtime_profile

        def run_task(self, task, seed):
            assert self.runtime_profile is not None
            max_steps = int(self.runtime_profile.execution.max_steps)
            observed_steps.append(max_steps)
            return RunResult(
                task_id=task.task_id,
                seed=seed,
                artifact={"max_steps": max_steps},
                verifier_score=float(max_steps),
                cost=0.0,
                latency=0.0,
                faults=0,
                trace_path=str(tmp_path / f"trace_{max_steps}.json"),
                hard_invalid=False,
                mode="single",
            )

    monkeypatch.setattr(evaluator_module, "TaskRuntime", FakeTaskRuntime)

    evaluator = RuntimeEvaluator(
        suite,
        tmp_path / "eval",
        LocalDeterministicProvider(),
        baseline_runtime_dir=runtime_dir,
    )
    observed_steps.clear()
    task = suite.proxy[0]

    baseline_eval = evaluator.evaluate_runtime(runtime_dir, partition="proxy", seeds=[0], tasks_override=[task])
    variant_eval = evaluator.evaluate_runtime(variant_dir, partition="proxy", seeds=[0], tasks_override=[task])

    assert observed_steps == [64, 5]
    assert baseline_eval.runtime_hash != variant_eval.runtime_hash
    assert baseline_eval.run_results[0].artifact == {"max_steps": 64}
    assert variant_eval.run_results[0].artifact == {"max_steps": 5}
    assert len(evaluator.cache) == 2
