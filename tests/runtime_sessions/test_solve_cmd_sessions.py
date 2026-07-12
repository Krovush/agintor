from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from agintor import cli
from agintor.cli import app


def test_v1_cli_has_no_callable_legacy_solve_function() -> None:
    assert not hasattr(cli, "solve_cmd")
    assert not hasattr(cli, "RuntimeHost")
    assert not hasattr(cli, "RuntimeSessionStore")


def test_legacy_unstructured_solve_surface_is_rejected_without_state(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "legacy-runtime"
    workspace = tmp_path / "legacy-workspace"
    result = CliRunner().invoke(
        app,
        [
            "solve",
            str(runtime),
            "--prompt",
            "hello",
            "--provider",
            "local",
            "--runtime-backend",
            "docker",
            "--workspace",
            str(workspace),
        ],
    )

    assert result.exit_code != 0
    assert not runtime.exists()
    assert not workspace.exists()
