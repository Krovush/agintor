from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from agintor.isolation.commands import DockerCommandBackend, IsolatedCommandPolicy, IsolatedCommandRequest


PINNED_IMAGE = f"python@sha256:{'a' * 64}"


def _policy(**overrides) -> IsolatedCommandPolicy:
    return IsolatedCommandPolicy(image=PINNED_IMAGE, **overrides)


def test_policy_rejects_unpinned_image_and_root_user() -> None:
    with pytest.raises(ValidationError, match="pinned"):
        IsolatedCommandPolicy(image="python:3.12")
    with pytest.raises(ValidationError, match="non-root"):
        IsolatedCommandPolicy(image=PINNED_IMAGE, user="0:0")


def test_request_rejects_traversal_empty_commands_and_secret_environment(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="cannot be empty"):
        IsolatedCommandRequest(command=(), workspace=tmp_path)
    with pytest.raises(ValidationError, match="stay within"):
        IsolatedCommandRequest(command=("python",), workspace=tmp_path, working_directory="../escape")
    with pytest.raises(ValidationError, match="secret-bearing"):
        IsolatedCommandRequest(command=("python",), workspace=tmp_path, environment={"OPENAI_API_KEY": "secret"})


def test_docker_arguments_enforce_the_full_containment_policy(tmp_path: Path) -> None:
    request = IsolatedCommandRequest(
        command=("python", "-c", "print('ok')"),
        workspace=tmp_path,
        working_directory="src",
        environment={"PYTHONHASHSEED": "0"},
    )
    backend = DockerCommandBackend(_policy())

    args = backend.build_run_arguments(request, container_name="agintor-cmd-test")
    joined = " ".join(args)

    assert args[:4] == ["docker", "run", "--rm", "--init"]
    assert "--pull never" in joined
    assert "--stop-timeout 1" in joined
    assert "--network none" in joined
    assert "--read-only" in args
    assert "--cap-drop ALL" in joined
    assert "--security-opt no-new-privileges" in joined
    assert "--pids-limit 128" in joined
    assert "--memory-swap 536870912" in joined
    assert "--user 65532:65532" in joined
    assert "--workdir /workspace/src" in joined
    assert "--tmpfs /tmp:rw,noexec,nosuid,nodev,size=67108864" in joined
    assert any(item.startswith("type=bind,source=") and item.endswith(",target=/workspace") for item in args)
    assert "PYTHONHASHSEED=0" in args
    assert args[-4:] == [PINNED_IMAGE, "python", "-c", "print('ok')"]
    assert "build" not in args


def test_backend_rejects_environment_not_allowed_by_policy(tmp_path: Path) -> None:
    request = IsolatedCommandRequest(command=("python", "-V"), workspace=tmp_path, environment={"HOME": "/tmp"})
    backend = DockerCommandBackend(_policy())

    with pytest.raises(ValueError, match="not allowed"):
        backend.build_run_arguments(request, container_name="agintor-cmd-test")


def test_policy_rejects_secret_names_even_when_explicitly_allowlisted() -> None:
    with pytest.raises(ValidationError, match="secret-bearing"):
        IsolatedCommandPolicy(image=PINNED_IMAGE, environment_allowlist=frozenset({"API_TOKEN"}))


class _FakeProcess:
    def __init__(self, *, stdout: bytes = b"", stderr: bytes = b"", running: bool = False, returncode: int = 0) -> None:
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode = None if running else returncode

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        del timeout
        if self.returncode is None:
            self.returncode = -9
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


def test_backend_records_completed_output_without_using_a_shell(tmp_path: Path) -> None:
    launched: dict[str, object] = {}

    def process_factory(args, **kwargs):
        launched["args"] = args
        launched["kwargs"] = kwargs
        return _FakeProcess(stdout=b"ok\n")

    request = IsolatedCommandRequest(command=("python", "-V"), workspace=tmp_path)
    result = DockerCommandBackend(_policy(), process_factory=process_factory).run(request)

    assert result.succeeded is True
    assert result.stdout == "ok\n"
    assert result.output_truncated is False
    assert launched["kwargs"]["shell"] is False
    assert launched["kwargs"]["stdin"] == subprocess.DEVNULL


def test_backend_force_removes_timed_out_container(tmp_path: Path) -> None:
    removed: list[list[str]] = []
    process = _FakeProcess(running=True)

    def command_runner(args, **kwargs):
        del kwargs
        removed.append(list(args))
        return subprocess.CompletedProcess(args, 0)

    request = IsolatedCommandRequest(command=("python", "-V"), workspace=tmp_path, timeout_s=0.01)
    result = DockerCommandBackend(
        _policy(timeout_s=0.01),
        process_factory=lambda *_args, **_kwargs: process,
        command_runner=command_runner,
    ).run(request)

    assert result.status.value == "timed_out"
    assert result.succeeded is False
    assert removed and removed[0][:3] == ["docker", "rm", "--force"]


def test_backend_force_removes_container_on_output_limit(tmp_path: Path) -> None:
    removed: list[list[str]] = []
    process = _FakeProcess(stdout=b"x" * 4096, running=True)

    def command_runner(args, **kwargs):
        del kwargs
        removed.append(list(args))
        return subprocess.CompletedProcess(args, 0)

    request = IsolatedCommandRequest(command=("python", "-V"), workspace=tmp_path)
    result = DockerCommandBackend(
        _policy(output_bytes=1024),
        process_factory=lambda *_args, **_kwargs: process,
        command_runner=command_runner,
    ).run(request)

    assert result.status.value == "output_limit"
    assert result.output_truncated is True
    assert len(result.stdout.encode("utf-8")) == 1024
    assert removed and removed[0][:3] == ["docker", "rm", "--force"]
