from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from agintor.artifacts import ArtifactAllocator, WorkspaceOrigin, is_path_within, resolve_recent_timestamped_subfolder
from agintor.benchmarks import BenchmarkSuite, build_demo_suite
from agintor.evaluator import RuntimeEvaluator
from agintor.providers import LocalDeterministicProvider
from agintor.schemas import BenchmarkTask, OperationSpec

pytestmark = pytest.mark.usefixtures("module_failure_artifact_bucket")
PROJECT_ROOT = Path(__file__).resolve().parents[1]


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


def test_artifact_allocator_places_implicit_workspaces_outside_repo_and_releases_them(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    artifact_root = tmp_path / "external_artifacts"
    repo_root.mkdir()
    allocator = ArtifactAllocator.resolve(repo_root=repo_root, artifact_root=artifact_root)

    lease = allocator.workspace(None, purpose="solve", mode="none", prefix="solve")

    assert lease.origin == WorkspaceOrigin.IMPLICIT
    assert lease.path.exists()
    assert str(lease.path.resolve()).startswith(str((artifact_root / "solve").resolve()))
    lease.release()
    assert lease.path.exists() is False


def test_artifact_allocator_allows_explicit_repo_local_workspaces(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    allocator = ArtifactAllocator.resolve(repo_root=repo_root, artifact_root=tmp_path / "external_artifacts")

    lease = allocator.workspace(repo_root / "user_workspace", purpose="build", mode="always")

    assert lease.origin == WorkspaceOrigin.EXPLICIT
    assert lease.path == repo_root / "user_workspace"


def test_artifact_allocator_rejects_repo_local_implicit_artifact_roots_without_creating_them(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    artifact_root = repo_root / ".tmp_artifacts"
    repo_root.mkdir()
    allocator = ArtifactAllocator.resolve(repo_root=repo_root, artifact_root=artifact_root)

    with pytest.raises(ValueError):
        allocator.workspace(None, purpose="eval", mode="none", prefix="eval")

    assert artifact_root.exists() is False
    assert (artifact_root / "eval").exists() is False


def test_pytest_temp_and_artifact_roots_are_external(external_pytest_basetemp: Path, external_artifact_root: Path) -> None:
    assert is_path_within(external_pytest_basetemp, PROJECT_ROOT) is False
    assert is_path_within(external_artifact_root, PROJECT_ROOT) is False
    assert str(external_artifact_root).startswith(str(external_pytest_basetemp.resolve()))


def test_pytest_repo_local_basetemp_is_rewritten_outside_repo() -> None:
    requested = PROJECT_ROOT / f".tmp_pytest_rewrite_probe_{os.getpid()}"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--basetemp",
            str(requested),
            "tests/test_artifact_hygiene.py::test_resolve_recent_timestamped_subfolder_reuses_bucket_within_one_hour",
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert requested.exists() is False


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
