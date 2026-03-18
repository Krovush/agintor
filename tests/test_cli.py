from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

import agintor.cli as cli_module

from agintor.cli import app
from agintor.providers import LocalDeterministicProvider
from agintor.runtime_profile import HostedProviderProfile, RuntimeProfile


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
