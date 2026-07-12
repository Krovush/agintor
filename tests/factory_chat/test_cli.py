from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from agintor.cli import app


def test_legacy_destination_factory_form_is_removed_at_v1_cutover(
    tmp_path: Path,
) -> None:
    project = tmp_path / "legacy-destination"
    result = CliRunner().invoke(
        app,
        [
            "build-runtime",
            "Build runtime",
            "--destination",
            str(project),
            "--steps",
            "1",
        ],
    )

    assert result.exit_code != 0
    assert not project.exists()


def test_legacy_prompt_backend_factory_form_is_removed_at_v1_cutover(
    tmp_path: Path,
) -> None:
    project = tmp_path / "legacy-chat"
    result = CliRunner().invoke(
        app,
        [
            "build-runtime",
            str(project),
            "--prompt",
            "Build chat runtime",
            "--runtime-backend",
            "docker",
        ],
    )

    assert result.exit_code != 0
    assert not project.exists()
