from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agintor.contracts import (
    CapabilityExchange,
    LongTermGraphSnapshot,
    RuntimeSolveResponse,
    SolveResult,
)
from agintor.core.versioning import RUNTIME_CONTRACT_VERSION
from agintor.storage.runtime_session_store import RuntimeSessionMismatchError, RuntimeSessionStore

from ._support import _runtime_dir


def test_solve_cmd_records_failed_runtime_chat_turn_and_uses_effective_profile_hash(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agintor import cli
    from agintor.storage.artifacts import ArtifactMode

    runtime_dir = _runtime_dir(tmp_path)
    workspace_dir = tmp_path / "workspace"
    profile = SimpleNamespace(marker="effective-profile")
    released = {}

    class FakeLease:
        path = workspace_dir

        def release(self, *, failed: bool) -> None:
            released["failed"] = failed

    class FailingHost:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def solve(self, *args, **kwargs):
            raise RuntimeError("runtime exploded")

    def fake_load_runtime(runtime_path, *, runtime_profile=None, runtime_backend=None, **kwargs):
        assert runtime_profile is profile
        assert runtime_backend == "local"
        return SimpleNamespace(runtime_hash="runtime.hash.effective")

    monkeypatch.setattr(cli, "load_runtime_profile", lambda *args, **kwargs: profile)
    monkeypatch.setattr(cli, "_build_provider", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "_resolve_workspace", lambda *args, **kwargs: FakeLease())
    monkeypatch.setattr(cli, "load_runtime", fake_load_runtime)
    monkeypatch.setattr(cli, "RuntimeHost", FailingHost)

    with pytest.raises(RuntimeError, match="runtime exploded"):
        cli.solve_cmd(
            runtime_dir=str(runtime_dir),
            task_id=None,
            suite="demo",
            partition="train",
            prompt="hello",
            prompt_file=None,
            seed=0,
            provider="local",
            api_key_file=None,
            profile=None,
            workspace=str(workspace_dir),
            artifact_mode=ArtifactMode.NONE,
            runtime_backend="local",
            session=None,
            new_session=False,
        )

    store = RuntimeSessionStore(runtime_dir)
    sessions = store.list_sessions()
    assert len(sessions) == 1
    identity = store.load_session(sessions[0], runtime_hash="runtime.hash.effective")
    assert identity.runtime_hash == "runtime.hash.effective"
    messages = store.messages(identity.session_id)
    assert len(messages) == 1
    assert messages[0].lifecycle_state == "failed"
    assert messages[0].boundary_state_path is None
    assert store.seed_for_next_message(identity.session_id) is None
    assert released["failed"] is True


def test_solve_cmd_rejects_continuing_session_with_different_backend(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agintor import cli
    from agintor.storage.artifacts import ArtifactMode

    runtime_dir = _runtime_dir(tmp_path)
    workspace_dir = tmp_path / "workspace"
    profile = SimpleNamespace(marker="effective-profile")
    existing = RuntimeSessionStore(runtime_dir).create_session(
        runtime_hash="runtime.hash.effective",
        runtime_backend="local",
    )

    class FakeLease:
        path = workspace_dir

        def release(self, *, failed: bool) -> None:
            pass

    class UnusedHost:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def solve(self, *args, **kwargs):
            raise AssertionError("host.solve should not run after backend pin mismatch")

    monkeypatch.setattr(cli, "load_runtime_profile", lambda *args, **kwargs: profile)
    monkeypatch.setattr(cli, "_build_provider", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "_resolve_workspace", lambda *args, **kwargs: FakeLease())
    monkeypatch.setattr(
        cli,
        "load_runtime",
        lambda *args, **kwargs: SimpleNamespace(runtime_hash="runtime.hash.effective"),
    )
    monkeypatch.setattr(cli, "RuntimeHost", UnusedHost)

    with pytest.raises(RuntimeSessionMismatchError, match="runtime backend"):
        cli.solve_cmd(
            runtime_dir=str(runtime_dir),
            task_id=None,
            suite="demo",
            partition="train",
            prompt="hello",
            prompt_file=None,
            seed=0,
            provider="local",
            api_key_file=None,
            profile=None,
            workspace=str(workspace_dir),
            artifact_mode=ArtifactMode.NONE,
            runtime_backend="docker",
            session=existing.session_id,
            new_session=False,
        )


def test_solve_cmd_persists_host_rewritten_docker_carryover_for_next_turn(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agintor import cli
    from agintor.storage.artifacts import ArtifactMode
    from agintor.runtime.host.backends.docker.executor import DockerRuntimeExecutor

    runtime_dir = _runtime_dir(tmp_path)
    workspace_dir = tmp_path / "workspace"
    docker_workspace = workspace_dir / "docker-workspace"
    docker_workspace.mkdir(parents=True)
    host_file = (tmp_path / "Host Files" / "report.json").resolve()
    host_file.parent.mkdir(parents=True)
    host_file.write_text("{}", encoding="utf-8")
    container_path = "/mnt/request-files/abc123/report.json"
    profile = SimpleNamespace(marker="effective-profile")
    released: dict[str, bool] = {}
    captured: dict[str, object] = {}

    class FakeLease:
        path = workspace_dir

        def release(self, *, failed: bool) -> None:
            released["failed"] = failed

    class DockerishHost:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def solve(self, runtime_path, request, **kwargs):
            captured["request"] = request
            response = RuntimeSolveResponse(
                request_id=request.request_id,
                capability_exchange=CapabilityExchange(
                    runtime_contract_version=RUNTIME_CONTRACT_VERSION,
                    supported_backends=["docker"],
                    runtime_asset_capabilities={"runtime_sdk": True},
                    resume_support=True,
                ),
                solve_result=SolveResult(
                    request_id=request.request_id,
                    runtime_hash="runtime.hash.effective",
                    run_lifecycle_state="completed",
                    mode="user_request",
                    artifact={
                        "path": container_path,
                        "summary": f"plain text mention {container_path}",
                    },
                    status="completed",
                    verification_status="best_effort",
                    summary="ok",
                    post_message_long_term_graph=LongTermGraphSnapshot(),
                    post_message_short_term_export=[
                        {
                            "kind": "post_message_export",
                            "path": container_path,
                            "content": {
                                "artifact_ref": container_path,
                                "summary": f"plain text mention {container_path}",
                            },
                        }
                    ],
                ),
            )
            DockerRuntimeExecutor(tmp_path / "executor")._rewrite_solve_response_paths(
                response,
                docker_workspace,
                request_file_reverse_map={container_path: str(host_file)},
            )
            return response

    monkeypatch.setattr(cli, "load_runtime_profile", lambda *args, **kwargs: profile)
    monkeypatch.setattr(cli, "_build_provider", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "_resolve_workspace", lambda *args, **kwargs: FakeLease())
    monkeypatch.setattr(
        cli,
        "load_runtime",
        lambda *args, **kwargs: SimpleNamespace(runtime_hash="runtime.hash.effective"),
    )
    monkeypatch.setattr(cli, "RuntimeHost", DockerishHost)

    cli.solve_cmd(
        runtime_dir=str(runtime_dir),
        task_id=None,
        suite="demo",
        partition="train",
        prompt="summarize the attached report",
        prompt_file=None,
        seed=0,
        provider="local",
        api_key_file=None,
        profile=None,
        workspace=str(workspace_dir),
        artifact_mode=ArtifactMode.NONE,
        runtime_backend="docker",
        session=None,
        new_session=False,
    )

    request = captured["request"]
    assert request.runtime_backend == "docker"
    assert request.trace_context.runtime_session_id
    assert request.trace_context.runtime_message_id
    assert request.trace_context.runtime_message_index == 0
    store = RuntimeSessionStore(runtime_dir)
    identity = store.load_session(
        store.list_sessions()[0],
        runtime_hash="runtime.hash.effective",
        runtime_backend="docker",
    )
    seed = store.seed_for_next_message(identity.session_id)
    assert seed is not None
    assert seed.short_term_carryover == [
        {
            "kind": "post_message_export",
            "path": str(host_file),
            "content": {
                "artifact_ref": str(host_file),
                "summary": f"plain text mention {container_path}",
            },
        }
    ]
    assert released["failed"] is False


def test_solve_cmd_failed_turn_ignores_stale_paused_run_for_same_prompt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agintor import cli
    from agintor.storage.artifacts import ArtifactMode
    from agintor.storage.run_store import RunStore

    runtime_dir = _runtime_dir(tmp_path)
    workspace_dir = tmp_path / "workspace"
    profile = SimpleNamespace(marker="effective-profile")

    class FakeLease:
        path = workspace_dir

        def release(self, *, failed: bool) -> None:
            pass

    class FailingHost:
        def __init__(self, workspace, *args, **kwargs) -> None:
            self.run_store = RunStore(workspace)

        def solve(self, runtime_path, request, **kwargs):
            stale_manifest = self.run_store.create_run(
                request_id=request.request_id,
                evaluation_unit_id=request.evaluation_unit_id,
                request_mode=request.mode,
                runtime_backend=request.runtime_backend,
                trace_context={
                    "runtime_session_id": "sess.previous",
                    "runtime_message_id": "msg.previous",
                    "runtime_message_index": 0,
                },
            )
            checkpoint_ref = str(Path(stale_manifest.run_root) / "checkpoints" / "checkpoint.stale.json")
            Path(checkpoint_ref).write_text("{}", encoding="utf-8")
            self.run_store.finish_run(
                stale_manifest,
                lifecycle_state="paused",
                latest_checkpoint_ref=checkpoint_ref,
                resumable=True,
                failure_kind="runtime_crash",
            )
            raise RuntimeError("runtime crashed before current checkpoint")

    monkeypatch.setattr(cli, "load_runtime_profile", lambda *args, **kwargs: profile)
    monkeypatch.setattr(cli, "_build_provider", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "_resolve_workspace", lambda *args, **kwargs: FakeLease())
    monkeypatch.setattr(
        cli,
        "load_runtime",
        lambda *args, **kwargs: SimpleNamespace(runtime_hash="runtime.hash.effective"),
    )
    monkeypatch.setattr(cli, "RuntimeHost", FailingHost)

    with pytest.raises(RuntimeError, match="runtime crashed"):
        cli.solve_cmd(
            runtime_dir=str(runtime_dir),
            task_id=None,
            suite="demo",
            partition="train",
            prompt="hello",
            prompt_file=None,
            seed=0,
            provider="local",
            api_key_file=None,
            profile=None,
            workspace=str(workspace_dir),
            artifact_mode=ArtifactMode.NONE,
            runtime_backend="local",
            session=None,
            new_session=False,
        )

    store = RuntimeSessionStore(runtime_dir)
    identity = store.load_session(store.list_sessions()[0], runtime_hash="runtime.hash.effective")
    message = store.messages(identity.session_id)[0]
    assert message.lifecycle_state == "failed"
    assert message.checkpoint_ref is None


def test_solve_cmd_records_paused_checkpoint_when_host_fails_after_checkpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agintor import cli
    from agintor.storage.artifacts import ArtifactMode
    from agintor.storage.run_store import RunStore

    runtime_dir = _runtime_dir(tmp_path)
    workspace_dir = tmp_path / "workspace"
    profile = SimpleNamespace(marker="effective-profile")

    class FakeLease:
        path = workspace_dir

        def release(self, *, failed: bool) -> None:
            pass

    class PausedHost:
        def __init__(self, workspace, *args, **kwargs) -> None:
            self.run_store = RunStore(workspace)

        def solve(self, runtime_path, request, **kwargs):
            manifest = self.run_store.create_run(
                request_id=request.request_id,
                evaluation_unit_id=request.evaluation_unit_id,
                request_mode=request.mode,
                runtime_backend=request.runtime_backend,
                trace_context=request.trace_context.model_dump() if request.trace_context is not None else None,
            )
            checkpoint_ref = str(Path(manifest.run_root) / "checkpoints" / "checkpoint.json")
            Path(checkpoint_ref).write_text("{}", encoding="utf-8")
            self.run_store.finish_run(
                manifest,
                lifecycle_state="paused",
                latest_checkpoint_ref=checkpoint_ref,
                resumable=True,
                failure_kind="runtime_crash",
            )
            raise RuntimeError("runtime crashed after checkpoint")

    monkeypatch.setattr(cli, "load_runtime_profile", lambda *args, **kwargs: profile)
    monkeypatch.setattr(cli, "_build_provider", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "_resolve_workspace", lambda *args, **kwargs: FakeLease())
    monkeypatch.setattr(
        cli,
        "load_runtime",
        lambda *args, **kwargs: SimpleNamespace(runtime_hash="runtime.hash.effective"),
    )
    monkeypatch.setattr(cli, "RuntimeHost", PausedHost)

    with pytest.raises(RuntimeError, match="runtime crashed"):
        cli.solve_cmd(
            runtime_dir=str(runtime_dir),
            task_id=None,
            suite="demo",
            partition="train",
            prompt="hello",
            prompt_file=None,
            seed=0,
            provider="local",
            api_key_file=None,
            profile=None,
            workspace=str(workspace_dir),
            artifact_mode=ArtifactMode.NONE,
            runtime_backend="local",
            session=None,
            new_session=False,
        )

    store = RuntimeSessionStore(runtime_dir)
    identity = store.load_session(store.list_sessions()[0], runtime_hash="runtime.hash.effective")
    message = store.messages(identity.session_id)[0]
    assert message.lifecycle_state == "paused"
    assert message.checkpoint_ref
