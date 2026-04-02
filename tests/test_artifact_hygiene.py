from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from agintor.artifacts import resolve_recent_timestamped_subfolder
from agintor.benchmarks import BenchmarkSuite, build_demo_suite
from agintor.evaluator import RuntimeEvaluator
from agintor.providers import LocalDeterministicProvider
from agintor.schemas import BenchmarkTask, OperationSpec

pytestmark = pytest.mark.usefixtures("module_failure_artifact_bucket")


def test_resolve_recent_timestamped_subfolder_reuses_bucket_within_one_hour(tmp_path: Path) -> None:
    root = tmp_path / "artifact_root"
    start = datetime(2026, 4, 2, 10, 0, 0)

    first = resolve_recent_timestamped_subfolder(root, now=start, create=True)
    second = resolve_recent_timestamped_subfolder(
        root,
        now=start + timedelta(minutes=59, seconds=59),
        create=True,
    )

    assert first is not None
    assert second == first


def test_resolve_recent_timestamped_subfolder_rolls_after_one_hour(tmp_path: Path) -> None:
    root = tmp_path / "artifact_root"
    start = datetime(2026, 4, 2, 10, 0, 0)

    first = resolve_recent_timestamped_subfolder(root, now=start, create=True)
    second = resolve_recent_timestamped_subfolder(
        root,
        now=start + timedelta(hours=1, minutes=1),
        create=True,
    )

    assert first is not None
    assert second is not None
    assert second != first


def test_runtime_evaluator_constructor_is_side_effect_free(runtime_dir: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "eval_ctor"

    RuntimeEvaluator(
        build_demo_suite(),
        workspace,
        LocalDeterministicProvider(),
        baseline_runtime_dir=runtime_dir,
    )

    assert workspace.exists() is False


def test_runtime_evaluator_reuses_shared_sandbox_cache_without_workspace_clutter(runtime_dir: Path, tmp_path: Path) -> None:
    suite = build_demo_suite()
    task = suite.by_id("tool.generated_sum_squares_mod")
    workspace = tmp_path / "eval"
    sandbox_root = tmp_path / "sandbox_cache"
    evaluator = RuntimeEvaluator(
        suite,
        workspace,
        LocalDeterministicProvider(),
        baseline_runtime_dir=None,
        artifact_mode="none",
        sandbox_root=sandbox_root,
    )

    evaluation = evaluator.evaluate_runtime(
        runtime_dir,
        partition="train",
        seeds=[0, 1],
        use_cache=False,
        tasks_override=[task],
    )

    assert evaluation.invalid is False
    assert workspace.exists() is False
    manifest_files = list(sandbox_root.rglob("manifest.json"))
    tool_sources = list(sandbox_root.rglob("generated_*.py"))
    pyc_files = list(sandbox_root.rglob("*.pyc"))
    assert manifest_files
    assert len(manifest_files) <= 2
    assert len(tool_sources) <= 2
    assert pyc_files == []


def test_runtime_evaluator_on_failure_keeps_trace_artifacts(runtime_dir: Path, tmp_path: Path) -> None:
    failing_task = BenchmarkTask(
        task_id="mem.missing_symbol",
        family="mem",
        prompt="Lookup the exact symbol MISSING_1.",
        task_type="memory_query",
        symbolic_seeds=["MISSING_1"],
        operations=[
            OperationSpec(
                op_id="lookup",
                kind="memory_lookup",
                output_key="answer",
                description="Lookup exact symbol value",
                requires_exact_symbol="MISSING_1",
            )
        ],
        expected="0",
        verifier_type="string_exact",
    )
    suite = BenchmarkSuite(name="artifact_failure", train=[failing_task], val=[], test=[], proxy=[failing_task])
    workspace = tmp_path / "eval_failure"
    evaluator = RuntimeEvaluator(
        suite,
        workspace,
        LocalDeterministicProvider(),
        baseline_runtime_dir=None,
        artifact_mode="on_failure",
        sandbox_root=tmp_path / "sandbox_cache",
    )

    evaluation = evaluator.evaluate_runtime(runtime_dir, partition="train", seeds=[0], use_cache=False)

    assert evaluation.invalid is True
    assert list(workspace.rglob("traces/*.json"))
