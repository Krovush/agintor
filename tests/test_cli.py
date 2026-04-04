from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import agintor.cli as cli_module

from agintor.cli import app
from agintor.providers import LocalDeterministicProvider
from agintor.runtime_profile import HostedProviderProfile, RuntimeProfile
from agintor.schemas import RunResult, SuiteEvaluation

pytestmark = pytest.mark.usefixtures("module_failure_artifact_bucket")


runner = CliRunner()



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

    solve = runner.invoke(app, ["solve", str(runtime_dir), "top.sum_product", "--suite", str(suite_path), "--provider", "local"])
    assert solve.exit_code == 0, solve.output
    solve_payload = json.loads(solve.output)
    assert solve_payload["result"]["trace_path"] is None
    assert (Path.cwd() / ".agintor_runs").exists() is False

    evaluation = runner.invoke(app, ["eval", str(runtime_dir), "--suite", str(suite_path), "--partition", "train", "--seeds", "0", "--provider", "local"])
    assert evaluation.exit_code == 0, evaluation.output
    eval_payload = json.loads(evaluation.output)
    assert all(run["trace_path"] is None for run in eval_payload["run_results"])
    assert (Path.cwd() / ".agintor_runs").exists() is False


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
    assert payload["solve_result"]["status"] == "best_effort"
    assert payload["solve_result"]["verified"] is False
    assert payload["solve_result"]["best_effort"] is True
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


def test_cli_build_runtime_uses_external_implicit_workspace_and_cleans_it(monkeypatch, tmp_path: Path) -> None:
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
        force=False,
        retain_artifacts=True,
    ):
        del provider, steps, mutator_type, profile_path, runtime_backend, force
        workspace_path = Path(workspace)
        captured["workspace"] = workspace_path
        captured["retain_artifacts"] = retain_artifacts
        workspace_path.mkdir(parents=True, exist_ok=True)
        (workspace_path / "build_marker.txt").write_text(prompt_text, encoding="utf-8")
        return type(
            "BuildResult",
            (),
            {
                "build_id": "build-id",
                "goal_id": "goal-id",
                "goal_prompt": prompt_text,
                "goal_spec_path": "",
                "success_criteria_path": "",
                "benchmark_plan_path": "",
                "verifier_bundle_path": "",
                "runtime_plan_path": "",
                "output_runtime_dir": str(destination),
                "workspace": str(workspace_path),
                "agintor_provider": "local",
                "runtime_provider": "minimax",
                "mutator_type": "heuristic",
                "best_train_score": 0.0,
                "best_goal_score": 0.0,
                "best_val_score": 0.0,
                "archive_cells": 0,
                "accepted_mutations": 0,
                "export_bundle_file": "",
                "provenance_bundle_file": "",
                "export_summary_path": "",
                "summary_path": "",
            },
        )()

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
    workspace_path = captured["workspace"]
    assert isinstance(workspace_path, Path)
    assert captured["retain_artifacts"] is False
    assert Path.cwd() not in workspace_path.resolve().parents
    assert workspace_path.exists() is False


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
