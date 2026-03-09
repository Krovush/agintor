from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from agintor.cli import app


runner = CliRunner()



def test_cli_init_solve_eval(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    suite_path = tmp_path / "demo_suite.json"
    result = runner.invoke(app, ["init-runtime", str(runtime_dir), "--write-demo-suite", str(suite_path)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert Path(payload["runtime_dir"]).exists()
    assert Path(payload["suite_path"]).exists()

    solve = runner.invoke(app, ["solve", str(runtime_dir), "top.sum_product", "--suite", str(suite_path), "--workspace", str(tmp_path / "solve_ws")])
    assert solve.exit_code == 0, solve.output
    solve_payload = json.loads(solve.output)
    assert solve_payload["result"]["verifier_score"] == 1.0

    evaluation = runner.invoke(app, ["eval", str(runtime_dir), "--suite", str(suite_path), "--partition", "train", "--seeds", "0", "--workspace", str(tmp_path / "eval_ws")])
    assert evaluation.exit_code == 0, evaluation.output
    eval_payload = json.loads(evaluation.output)
    assert eval_payload["invalid"] is False
