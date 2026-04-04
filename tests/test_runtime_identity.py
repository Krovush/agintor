from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import agintor.evaluator as evaluator_module

from agintor.benchmarks import build_demo_suite
from agintor.exceptions import RuntimeLoadError
from agintor.evaluator import RuntimeEvaluator
from agintor.project import init_runtime
from agintor.providers import LocalDeterministicProvider
from agintor.runtime_loader import load_runtime
from agintor.runtime_sdk import KERNEL_MANIFEST_FILE
from agintor.runtime_profile import RUNTIME_PROFILE_FILE, load_runtime_profile, profile_to_json
from agintor.schemas import CapabilityExchange, RunResult, RuntimeBatchResponse
from agintor.utils import file_digest

pytestmark = pytest.mark.usefixtures("module_failure_artifact_bucket")


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


def test_runtime_loader_rejects_tampered_bundled_kernel_file(runtime_dir: Path, tmp_path: Path) -> None:
    tampered_runtime = tmp_path / "tampered_runtime"
    shutil.copytree(runtime_dir, tampered_runtime)
    kernel_file = tampered_runtime / "runtime_sdk" / "agintor_runtime" / "runner.py"
    kernel_file.write_text(kernel_file.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(RuntimeLoadError):
        load_runtime(tampered_runtime)


def test_runtime_identity_changes_when_bundled_kernel_digest_changes(runtime_dir: Path, tmp_path: Path) -> None:
    baseline = load_runtime(runtime_dir)
    variant_runtime = tmp_path / "variant_runtime"
    shutil.copytree(runtime_dir, variant_runtime)
    kernel_file = variant_runtime / "runtime_sdk" / "agintor_runtime" / "runner.py"
    kernel_file.write_text(kernel_file.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    kernel_manifest_path = variant_runtime / "runtime_sdk" / KERNEL_MANIFEST_FILE
    kernel_manifest = json.loads(kernel_manifest_path.read_text(encoding="utf-8"))
    kernel_manifest["files"]["agintor_runtime/runner.py"] = file_digest(kernel_file)
    kernel_manifest_path.write_text(json.dumps(kernel_manifest, indent=2, sort_keys=True), encoding="utf-8")

    variant = load_runtime(variant_runtime)

    assert baseline.code_hash != variant.code_hash
    assert baseline.runtime_hash != variant.runtime_hash


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

    def fake_run_batch(self, runtime_dir_arg, task_runs, *, provider, runtime_profile=None, budget_overrides=None):
        del self, provider
        assert runtime_profile is not None
        assert budget_overrides in (None, {})
        max_steps = int(runtime_profile.execution.max_steps)
        observed_steps.append(max_steps)
        return RuntimeBatchResponse(
            request_id=f"batch.{max_steps}",
            capability_exchange=CapabilityExchange(
                runtime_abi="agintor-runtime-abi-v3",
                kernel_version="agintor-kernel-v1",
                storage_schema_version="agintor-storage-v1",
                supported_backends=["local"],
                tool_runtimes=["python"],
                checkpoint_support=True,
                runtime_asset_capabilities={"traces": True, "checkpoints": True, "runtime_sdk": True},
                side_effect_receipts=False,
                required_env_names=[],
                capability_flags=["inspect", "run_batch"],
            ),
            run_results=[
                RunResult(
                    task_id=task.task_id,
                    seed=seed,
                    artifact={"max_steps": max_steps, "runtime_dir": str(runtime_dir_arg)},
                    verifier_score=float(max_steps),
                    cost=0.0,
                    latency=0.0,
                    faults=0,
                    trace_path=str(tmp_path / f"trace_{max_steps}.json"),
                    hard_invalid=False,
                    mode="single",
                )
                for task, seed in task_runs
            ],
            provider_usage={"calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "dollar_cost": 0.0},
        )

    monkeypatch.setattr(evaluator_module.RuntimeHost, "run_batch", fake_run_batch)

    evaluator = RuntimeEvaluator(
        suite,
        tmp_path / "eval",
        LocalDeterministicProvider(),
        baseline_runtime_dir=runtime_dir,
    )
    evaluator.prepare_reference_scales()
    observed_steps.clear()
    task = suite.proxy[0]

    baseline_eval = evaluator.evaluate_runtime(runtime_dir, partition="proxy", seeds=[0], tasks_override=[task])
    variant_eval = evaluator.evaluate_runtime(variant_dir, partition="proxy", seeds=[0], tasks_override=[task])

    assert observed_steps == [64, 5]
    assert baseline_eval.runtime_hash != variant_eval.runtime_hash
    assert baseline_eval.run_results[0].artifact["max_steps"] == 64
    assert variant_eval.run_results[0].artifact["max_steps"] == 5
    assert len(evaluator.cache) == 2


def test_evaluator_forwards_budget_overrides_to_runtime_host(runtime_dir: Path, tmp_path: Path, monkeypatch) -> None:
    suite = build_demo_suite()
    captured: list[dict[str, int]] = []

    def fake_run_batch(self, runtime_dir_arg, task_runs, *, provider, runtime_profile=None, budget_overrides=None):
        del self, runtime_dir_arg, task_runs, provider, runtime_profile
        captured.append(dict(budget_overrides or {}))
        return RuntimeBatchResponse(
            request_id="batch",
            capability_exchange=CapabilityExchange(
                runtime_abi="agintor-runtime-abi-v3",
                kernel_version="agintor-kernel-v1",
                storage_schema_version="agintor-storage-v1",
                supported_backends=["local"],
                tool_runtimes=["python"],
                checkpoint_support=True,
                runtime_asset_capabilities={"traces": True, "checkpoints": True, "runtime_sdk": True},
                side_effect_receipts=False,
                required_env_names=[],
                capability_flags=["inspect", "run_batch"],
            ),
            run_results=[
                RunResult(
                    task_id=suite.proxy[0].task_id,
                    seed=0,
                    artifact={"ok": True},
                    verifier_score=0.0,
                    cost=0.0,
                    latency=0.0,
                    faults=0,
                )
            ],
            provider_usage={"calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "dollar_cost": 0.0},
        )

    monkeypatch.setattr(evaluator_module.RuntimeHost, "run_batch", fake_run_batch)

    evaluator = RuntimeEvaluator(
        suite,
        tmp_path / "eval_budget_forward",
        LocalDeterministicProvider(),
        baseline_runtime_dir=runtime_dir,
        budget_overrides={"M_max": 3, "Q_max": 1},
    )
    evaluator.evaluate_runtime(runtime_dir, partition="proxy", seeds=[0], use_cache=False, tasks_override=[suite.proxy[0]])

    assert captured
    assert all(entry == {"M_max": 3, "Q_max": 1} for entry in captured)
