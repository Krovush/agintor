from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import agintor.cli as cli_module

from agintor.artifacts import ArtifactMode, is_path_within
from agintor.cli import app
from agintor.providers import LocalDeterministicProvider
from agintor.runtime_profile import HostedProviderProfile, RuntimeProfile
from agintor.schemas import RunResult, SuiteEvaluation

pytestmark = pytest.mark.usefixtures("module_failure_artifact_bucket")
PROJECT_ROOT = Path(__file__).resolve().parents[1]


runner = CliRunner()


def _repo_scratch_entries() -> set[str]:
    prefixes = (".tmp", "tmp", ".agintor")
    names: set[str] = set()
    for entry in PROJECT_ROOT.iterdir():
        if entry.name.startswith(prefixes):
            names.add(entry.name)
        if entry.name.startswith("pytest-cache-files-"):
            names.add(entry.name)
    return names



def test_cli_init_solve_eval(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    suite_path = tmp_path / "demo_suite.json"
    result = runner.invoke(app, ["init-runtime", str(runtime_dir), "--write-demo-suite", str(suite_path)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert Path(payload["runtime_dir"]).exists()
    assert Path(payload["suite_path"]).exists()

    solve = runner.invoke(app, ["solve", str(runtime_dir), "top.sum_product", "--suite", str(suite_path), "--provider", "local", "--workspace", str(tmp_path / "solve_ws")])
    assert solve.exit_code == 0, solve.output
    solve_payload = json.loads(solve.output)
    assert solve_payload["result"]["verifier_score"] == 1.0
    assert solve_payload["provider_usage"]["calls"] == solve_payload["result"]["model_calls"]

    evaluation = runner.invoke(app, ["eval", str(runtime_dir), "--suite", str(suite_path), "--partition", "train", "--seeds", "0", "--provider", "local", "--workspace", str(tmp_path / "eval_ws")])
    assert evaluation.exit_code == 0, evaluation.output
    eval_payload = json.loads(evaluation.output)
    assert eval_payload["invalid"] is False
    assert eval_payload["provider_usage"]["calls"] == sum(run["model_calls"] for run in eval_payload["run_results"])


def test_cli_default_solve_eval_do_not_create_repo_workspace_dirs(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    suite_path = tmp_path / "demo_suite.json"
    init_result = runner.invoke(app, ["init-runtime", str(runtime_dir), "--write-demo-suite", str(suite_path)])
    assert init_result.exit_code == 0, init_result.output
    before = _repo_scratch_entries()

    solve = runner.invoke(app, ["solve", str(runtime_dir), "top.sum_product", "--suite", str(suite_path), "--provider", "local"])
    assert solve.exit_code == 0, solve.output
    solve_payload = json.loads(solve.output)
    assert solve_payload["result"]["trace_path"] is None

    evaluation = runner.invoke(app, ["eval", str(runtime_dir), "--suite", str(suite_path), "--partition", "train", "--seeds", "0", "--provider", "local"])
    assert evaluation.exit_code == 0, evaluation.output
    eval_payload = json.loads(evaluation.output)
    assert all(run["trace_path"] is None for run in eval_payload["run_results"])
    assert _repo_scratch_entries() == before


def test_cli_solve_supports_prompt_mode_with_verified_result(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    init_result = runner.invoke(app, ["init-runtime", str(runtime_dir)])
    assert init_result.exit_code == 0, init_result.output

    solve = runner.invoke(
        app,
        [
            "solve",
            str(runtime_dir),
            "--prompt",
            "Given the numbers [2, 3, 5], compute the sum and product and return JSON with keys sum and product.",
            "--provider",
            "local",
            "--workspace",
            str(tmp_path / "solve_prompt_ws"),
        ],
    )

    assert solve.exit_code == 0, solve.output
    payload = json.loads(solve.output)
    assert payload["mode"] == "user_request"
    assert payload["solve_result"]["status"] == "verified"
    assert payload["solve_result"]["verified"] is True
    assert payload["solve_result"]["artifact"] == {"product": 30, "sum": 10}


def test_cli_solve_prompt_mode_reports_best_effort_for_generic_requests(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    init_result = runner.invoke(app, ["init-runtime", str(runtime_dir)])
    assert init_result.exit_code == 0, init_result.output

    solve = runner.invoke(
        app,
        [
            "solve",
            str(runtime_dir),
            "--prompt",
            "Write one short sentence about why deterministic runtime contracts are useful.",
            "--provider",
            "local",
            "--workspace",
            str(tmp_path / "solve_best_effort_ws"),
        ],
    )

    assert solve.exit_code == 0, solve.output
    payload = json.loads(solve.output)
    assert payload["mode"] == "user_request"
    assert payload["solve_result"]["status"] == "partially_checked"
    assert payload["solve_result"]["verification_status"] == "partially_checked"
    assert payload["solve_result"]["verified"] is False
    assert payload["solve_result"]["best_effort"] is False
    assert payload["solve_result"]["artifact"]


def test_cli_solve_prompt_mode_passes_budget_overrides_to_evaluator(tmp_path: Path, monkeypatch) -> None:
    runtime_dir = tmp_path / "runtime"
    init_result = runner.invoke(app, ["init-runtime", str(runtime_dir)])
    assert init_result.exit_code == 0, init_result.output
    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "prompt": "Write one short sentence about runtime profiles.",
                "budget_overrides": {"M_max": 3, "Q_max": 1},
            }
        ),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    class FakeEvaluator:
        def __init__(self, suite, workspace, provider, **kwargs):
            del suite, workspace, provider
            captured["budget_overrides"] = kwargs.get("budget_overrides")

        def evaluate_runtime(self, runtime_dir, partition="train", seeds=(0,), use_cache=False, tasks_override=None):
            del runtime_dir, partition, seeds, use_cache, tasks_override
            return SuiteEvaluation(
                runtime_hash="runtime",
                objective_scores={},
                task_scores={},
                family_scores={},
                run_results=[
                    RunResult(
                        task_id="user.solve.request",
                        seed=0,
                        artifact="ok",
                        verifier_score=0.0,
                        cost=0.0,
                        latency=0.0,
                        faults=0,
                    )
                ],
                invalid=False,
            )

    monkeypatch.setattr(cli_module, "RuntimeEvaluator", FakeEvaluator)

    solve = runner.invoke(
        app,
        [
            "solve",
            str(runtime_dir),
            "--prompt-file",
            str(request_path),
            "--provider",
            "local",
            "--workspace",
            str(tmp_path / "solve_budget_ws"),
        ],
    )

    assert solve.exit_code == 0, solve.output
    assert captured["budget_overrides"] == {"M_max": 3, "Q_max": 1}


def test_cli_build_runtime_keeps_external_implicit_workspace_and_valid_artifact_paths(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_build_runtime_from_goal(
        prompt_text,
        *,
        destination,
        workspace,
        provider,
        steps=10,
        mutator_type="heuristic",
        profile_path=None,
        runtime_backend="docker",
        artifact_mode=ArtifactMode.ALWAYS,
        force=False,
    ):
        del provider, steps, mutator_type, profile_path, runtime_backend, force
        workspace_path = Path(workspace)
        goal_dir = workspace_path / "goal"
        planning_dir = workspace_path / "planning"
        export_dir = workspace_path / "export"
        goal_dir.mkdir(parents=True, exist_ok=True)
        planning_dir.mkdir(parents=True, exist_ok=True)
        export_dir.mkdir(parents=True, exist_ok=True)
        goal_spec_path = goal_dir / "goal_spec.json"
        success_path = goal_dir / "success_criteria.json"
        benchmark_path = planning_dir / "benchmark_plan.json"
        verifier_path = planning_dir / "verifier_bundle.json"
        runtime_plan_path = planning_dir / "runtime_plan.json"
        export_summary_path = export_dir / "export_summary.json"
        summary_path = export_dir / "build_summary.json"
        for path in [
            goal_spec_path,
            success_path,
            benchmark_path,
            verifier_path,
            runtime_plan_path,
            export_summary_path,
            summary_path,
        ]:
            path.write_text(prompt_text, encoding="utf-8")
        captured["artifact_mode"] = artifact_mode.value if isinstance(artifact_mode, ArtifactMode) else str(artifact_mode)
        return SimpleNamespace(
            build_id="build-id",
            goal_id="goal-id",
            goal_prompt=prompt_text,
            goal_spec_path=str(goal_spec_path),
            success_criteria_path=str(success_path),
            benchmark_plan_path=str(benchmark_path),
            verifier_bundle_path=str(verifier_path),
            runtime_plan_path=str(runtime_plan_path),
            output_runtime_dir=str(destination),
            workspace=str(workspace_path),
            agintor_provider="local",
            runtime_provider="minimax",
            mutator_type="heuristic",
            best_train_score=0.0,
            best_goal_score=0.0,
            best_val_score=0.0,
            archive_cells=0,
            accepted_mutations=0,
            export_bundle_file="",
            provenance_bundle_file="",
            export_summary_path=str(export_summary_path),
            summary_path=str(summary_path),
        )

    monkeypatch.setattr(cli_module, "build_runtime_from_goal", fake_build_runtime_from_goal)

    result = runner.invoke(
        app,
        [
            "build-runtime",
            "Build a runtime specialized for exact retrieval.",
            "--destination",
            str(tmp_path / "exported"),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    workspace_path = Path(payload["workspace"])
    assert captured["artifact_mode"] == ArtifactMode.ALWAYS.value
    assert workspace_path.exists()
    assert is_path_within(workspace_path, PROJECT_ROOT) is False
    assert Path(payload["goal_spec_path"]).exists()
    assert Path(payload["success_criteria_path"]).exists()
    assert Path(payload["benchmark_plan_path"]).exists()
    assert Path(payload["verifier_bundle_path"]).exists()
    assert Path(payload["runtime_plan_path"]).exists()
    assert Path(payload["summary_path"]).exists()
    assert Path(payload["export_summary_path"]).exists()


def test_cli_solve_prompt_mode_returns_controlled_failure_when_tool_scope_blocks_exact_path(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    init_result = runner.invoke(app, ["init-runtime", str(runtime_dir)])
    assert init_result.exit_code == 0, init_result.output
    request_path = tmp_path / "restricted_request.json"
    request_path.write_text(
        json.dumps(
            {
                "prompt": "Compute the sum of squares modulo 7 for [2, 3].",
                "verification_preference": "required",
                "allowed_tool_categories": ["memory"],
            }
        ),
        encoding="utf-8",
    )

    solve = runner.invoke(
        app,
        [
            "solve",
            str(runtime_dir),
            "--prompt-file",
            str(request_path),
            "--provider",
            "local",
            "--workspace",
            str(tmp_path / "solve_restricted_ws"),
        ],
    )

    assert solve.exit_code == 0, solve.output
    payload = json.loads(solve.output)
    assert payload["mode"] == "user_request"
    assert payload["solve_result"]["status"] == "controlled_failure"
    assert payload["solve_result"]["verified"] is False
    assert payload["solve_result"]["best_effort"] is False


def test_cli_solve_defaults_to_runtime_provider_profile(tmp_path: Path, monkeypatch) -> None:
    runtime_dir = tmp_path / "runtime"
    suite_path = tmp_path / "demo_suite.json"
    init_result = runner.invoke(app, ["init-runtime", str(runtime_dir), "--write-demo-suite", str(suite_path)])
    assert init_result.exit_code == 0, init_result.output
    calls: list[tuple[str, str | None]] = []

    def fake_build_provider(name: str = "local", *, provider_profile=None, **kwargs):
        calls.append((name, None if provider_profile is None else provider_profile.name))
        return LocalDeterministicProvider()

    monkeypatch.setattr(cli_module, "build_provider", fake_build_provider)
    solve = runner.invoke(app, ["solve", str(runtime_dir), "top.sum_product", "--suite", str(suite_path), "--workspace", str(tmp_path / "solve_ws")])

    assert solve.exit_code == 0, solve.output
    assert calls
    assert calls[0] == ("minimax", "minimax")


def test_build_provider_does_not_apply_runtime_profile_when_not_defaulting(monkeypatch) -> None:
    calls: list[tuple[str, object | None]] = []

    def fake_build_provider(name: str = "local", *, provider_profile=None, **kwargs):
        calls.append((name, provider_profile))
        return LocalDeterministicProvider()

    monkeypatch.setattr(cli_module, "build_provider", fake_build_provider)

    cli_module._build_provider(
        "openai",
        None,
        RuntimeProfile(
            provider=HostedProviderProfile(
                name="openai",
                base_url="https://runtime.example",
            )
        ),
        default_to_runtime_profile=False,
    )

    assert calls == [("openai", None)]
