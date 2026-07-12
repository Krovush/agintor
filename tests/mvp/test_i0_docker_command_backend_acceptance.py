from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from agintor.isolation.commands import (
    DockerCommandBackend,
    IsolatedCommandPolicy,
    IsolatedCommandRequest,
    IsolatedCommandStatus,
)


pytestmark = pytest.mark.docker

ACCEPTANCE_IMAGE_ENV = "AGINTOR_DOCKER_COMMAND_ACCEPTANCE_IMAGE"


def _docker() -> str:
    executable = shutil.which("docker")
    if executable is None:
        pytest.skip("Docker executable is unavailable")
    return executable


def _policy(**overrides: object) -> IsolatedCommandPolicy:
    image = os.environ.get(ACCEPTANCE_IMAGE_ENV, "").strip()
    if not image:
        pytest.skip(
            f"set {ACCEPTANCE_IMAGE_ENV} to an already-pulled digest-pinned Python image"
        )
    try:
        policy = IsolatedCommandPolicy(image=image, **overrides)
    except ValidationError as exc:
        pytest.fail(f"{ACCEPTANCE_IMAGE_ENV} must be digest-pinned: {exc}")
    inspected = subprocess.run(
        [_docker(), "image", "inspect", policy.image],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        check=False,
        timeout=10.0,
    )
    if inspected.returncode != 0:
        pytest.skip(
            f"{ACCEPTANCE_IMAGE_ENV} image is not preloaded locally; builds and pulls are not allowed"
        )
    return policy


def _backend(policy: IsolatedCommandPolicy) -> DockerCommandBackend:
    return DockerCommandBackend(policy, docker_executable=_docker())


def _request(
    tmp_path: Path,
    script: str,
    *,
    timeout_s: float | None = None,
    environment: dict[str, str] | None = None,
) -> IsolatedCommandRequest:
    return IsolatedCommandRequest(
        command=("python", "-c", script),
        workspace=tmp_path,
        environment=environment or {},
        timeout_s=timeout_s,
    )


def _assert_container_removed(container_name: str) -> None:
    inspected = subprocess.run(
        [_docker(), "ps", "-a", "--filter", f"name=^{container_name}$", "--format", "{{.Names}}"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        shell=False,
        check=False,
        timeout=10.0,
    )
    assert inspected.returncode == 0
    assert inspected.stdout.strip() == ""


def test_real_docker_backend_blocks_network_and_secret_environment(tmp_path: Path) -> None:
    policy = _policy(timeout_s=5.0)
    script = """
import socket
import sys

sock = socket.socket()
sock.settimeout(0.5)
try:
    sock.connect(("1.1.1.1", 53))
except OSError:
    print("network-blocked")
    sys.exit(0)
else:
    sys.exit(2)
finally:
    sock.close()
"""

    result = _backend(policy).run(_request(tmp_path, script))

    assert result.status is IsolatedCommandStatus.COMPLETED
    assert result.exit_code == 0
    assert "network-blocked" in result.stdout
    _assert_container_removed(result.container_name)
    with pytest.raises(ValidationError, match="secret-bearing"):
        IsolatedCommandRequest(
            command=("python", "-V"),
            workspace=tmp_path,
            environment={"OPENAI_API_KEY": "not-a-real-key"},
        )


def test_real_docker_backend_enforces_time_and_output_limits(tmp_path: Path) -> None:
    timeout_policy = _policy(timeout_s=0.2, output_bytes=4096)
    timeout_result = _backend(timeout_policy).run(
        _request(
            tmp_path,
            "import time; time.sleep(5)",
            timeout_s=0.2,
        )
    )

    assert timeout_result.status is IsolatedCommandStatus.TIMED_OUT
    assert timeout_result.exit_code is not None or timeout_result.failure_detail
    _assert_container_removed(timeout_result.container_name)

    output_policy = _policy(timeout_s=5.0, output_bytes=2048)
    output_result = _backend(output_policy).run(
        _request(tmp_path, "import sys; sys.stdout.write('x' * 100000)")
    )

    assert output_result.status is IsolatedCommandStatus.OUTPUT_LIMIT
    assert output_result.output_truncated is True
    assert len(output_result.stdout.encode("utf-8")) == output_policy.output_bytes
    _assert_container_removed(output_result.container_name)


def test_real_docker_backend_enforces_pids_memory_and_orphan_cleanup(tmp_path: Path) -> None:
    pids_policy = _policy(timeout_s=5.0, pids_limit=16)
    pids_script = """
import os
import signal
import sys
import time

children = []
limited = False
try:
    for _ in range(64):
        try:
            pid = os.fork()
        except OSError:
            limited = True
            break
        if pid == 0:
            time.sleep(10)
            os._exit(0)
        children.append(pid)
finally:
    for pid in children:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for pid in children:
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
if not limited:
    sys.exit(2)
print("pids-limited")
"""

    pids_result = _backend(pids_policy).run(_request(tmp_path, pids_script))

    assert pids_result.status is IsolatedCommandStatus.COMPLETED
    assert pids_result.exit_code == 0
    assert "pids-limited" in pids_result.stdout
    _assert_container_removed(pids_result.container_name)

    memory_policy = _policy(timeout_s=5.0, memory_bytes=96 * 1024 * 1024)
    memory_script = """
import sys

try:
    bytearray(512 * 1024 * 1024)
except MemoryError:
    print("memory-limited")
    sys.exit(0)
sys.exit(2)
"""

    memory_result = _backend(memory_policy).run(_request(tmp_path, memory_script))

    assert memory_result.status is IsolatedCommandStatus.COMPLETED
    assert memory_result.exit_code != 2
    _assert_container_removed(memory_result.container_name)
