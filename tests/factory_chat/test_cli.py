from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


def test_build_runtime_cli_accepts_documented_destination_form(tmp_path: Path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from agintor import cli
    from agintor.cli import app

    project_dir = tmp_path / "project.cli"
    workspace_dir = tmp_path / "workspace"
    captured: dict[str, object] = {}

    class FakeLease:
        path = workspace_dir

        def release(self, *, failed: bool) -> None:
            captured["released_failed"] = failed

    def fake_apply_factory_message(project_dir_arg, instruction, **kwargs):
        captured["project_dir"] = project_dir_arg
        captured["instruction"] = instruction
        captured["workspace"] = kwargs["workspace"]
        return SimpleNamespace(
            chat=SimpleNamespace(
                chat_id="chat.cli",
                project_dir=str(project_dir),
            ),
            message=SimpleNamespace(
                message_id="msg.cli",
                message_index=0,
                parent_message_id=None,
                leader_runtime_hash="runtime.hash.cli",
                leader_runtime_dir=str(project_dir),
                build_id="build.cli",
            ),
            result=SimpleNamespace(build_id="build.cli"),
        )

    monkeypatch.setattr(cli, "_build_provider", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "_resolve_workspace", lambda *args, **kwargs: FakeLease())
    monkeypatch.setattr(cli, "apply_factory_message", fake_apply_factory_message)

    result = CliRunner().invoke(
        app,
        ["build-runtime", "Build runtime", "--destination", str(project_dir), "--steps", "1"],
    )

    assert result.exit_code == 0, result.output
    assert captured["project_dir"] == str(project_dir)
    assert captured["instruction"] == "Build runtime"
    assert captured["workspace"] == workspace_dir
    assert captured["released_failed"] is False


def test_build_runtime_cli_accepts_project_prompt_chat_form(tmp_path: Path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from agintor import cli
    from agintor.cli import app

    project_dir = tmp_path / "project.chat"
    workspace_dir = tmp_path / "workspace"
    captured: dict[str, object] = {}

    class FakeLease:
        path = workspace_dir

        def release(self, *, failed: bool) -> None:
            captured["released_failed"] = failed

    def fake_apply_factory_message(project_dir_arg, instruction, **kwargs):
        captured["project_dir"] = project_dir_arg
        captured["instruction"] = instruction
        captured["workspace"] = kwargs["workspace"]
        captured["runtime_backend"] = kwargs["runtime_backend"]
        return SimpleNamespace(
            chat=SimpleNamespace(
                chat_id="chat.cli",
                project_dir=str(project_dir),
            ),
            message=SimpleNamespace(
                message_id="msg.cli",
                message_index=0,
                parent_message_id=None,
                leader_runtime_hash="runtime.hash.cli",
                leader_runtime_dir=str(project_dir),
                build_id="build.cli",
            ),
            result=SimpleNamespace(build_id="build.cli"),
        )

    monkeypatch.setattr(cli, "_build_provider", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "_resolve_workspace", lambda *args, **kwargs: FakeLease())
    monkeypatch.setattr(cli, "apply_factory_message", fake_apply_factory_message)

    result = CliRunner().invoke(
        app,
        ["build-runtime", str(project_dir), "--prompt", "Build chat runtime", "--runtime-backend", "docker"],
    )

    assert result.exit_code == 0, result.output
    assert captured["project_dir"] == str(project_dir)
    assert captured["instruction"] == "Build chat runtime"
    assert captured["workspace"] == workspace_dir
    assert captured["runtime_backend"] == "docker"
    assert captured["released_failed"] is False
