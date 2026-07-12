from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from ...isolation.commands import (
    IsolatedCommandBackend,
    IsolatedCommandRequest,
    IsolatedCommandResult,
    IsolatedCommandStatus,
)
from ...repositories.workspaces import repository_snapshot_digest


class RepoPatchExecutionBackend(Protocol):
    """Evaluator adapter around a containment backend.

    The adapter owns only command-shape and environment identity. Patch meaning,
    protected paths, public/sealed phases, and scoring remain evaluator concerns.
    """

    backend_id: str
    is_isolated: bool
    python_argv: tuple[str, ...]
    git_argv: tuple[str, ...]

    @property
    def identity_payload(self) -> Mapping[str, Any]: ...

    def command_argv(self, command: str | Sequence[str]) -> tuple[str, ...]: ...

    def run(self, request: IsolatedCommandRequest) -> IsolatedCommandResult: ...

    @property
    def last_workspace_transition(self) -> Mapping[str, str] | None: ...


class IsolatedRepoPatchCommandBackend:
    """Strict V1 adapter for a real or recording isolated command backend."""

    backend_id = "repo_patch.isolated_command.v1"
    is_isolated = True

    def __init__(
        self,
        command_backend: IsolatedCommandBackend,
        *,
        environment_identity: Mapping[str, Any],
        python_argv: Sequence[str] = ("python",),
        git_argv: Sequence[str] = ("git",),
    ) -> None:
        if not environment_identity:
            raise ValueError("isolated repo-patch backend requires a frozen environment identity")
        self._command_backend = command_backend
        self._environment_identity = _normalized_identity(environment_identity)
        self.python_argv = _fixed_argv(python_argv, name="python_argv")
        self.git_argv = _fixed_argv(git_argv, name="git_argv")
        self._last_workspace_transition: dict[str, str] | None = None

    @property
    def identity_payload(self) -> Mapping[str, Any]:
        return {
            "adapter": self.backend_id,
            "environment": dict(self._environment_identity),
            "python_argv": list(self.python_argv),
            "git_argv": list(self.git_argv),
        }

    def command_argv(self, command: str | Sequence[str]) -> tuple[str, ...]:
        if isinstance(command, str):
            raise ValueError("isolated repo-patch commands must be explicit argv; shell strings are forbidden")
        return _fixed_argv(command, name="command")

    def run(self, request: IsolatedCommandRequest) -> IsolatedCommandResult:
        self._last_workspace_transition = None
        physical_before = repository_snapshot_digest(request.workspace)
        result = self._command_backend.run(request)
        physical_after = repository_snapshot_digest(request.workspace)
        backend_transition = _last_backend_workspace_transition(self._command_backend)
        self._last_workspace_transition = {
            **(
                backend_transition
                if backend_transition is not None
                else {
                    "source": "physical_workspace",
                    "before": physical_before,
                    "after": physical_after,
                }
            ),
            "physical_before": physical_before,
            "physical_after": physical_after,
        }
        return result

    @property
    def last_workspace_transition(self) -> Mapping[str, str] | None:
        if self._last_workspace_transition is None:
            return None
        return dict(self._last_workspace_transition)


class TrustedLocalRepoPatchCommandBackend:
    """Explicitly unsafe compatibility backend for deterministic local tests.

    This is not a containment backend and must never be selected implicitly by
    the V1 evaluator path. Shell-form legacy commands are converted to an
    explicit shell executable argv while subprocess itself still uses
    ``shell=False``.
    """

    backend_id = "repo_patch.trusted_local.v1"
    is_isolated = False

    def __init__(
        self,
        *,
        python_argv: Sequence[str] | None = None,
        git_argv: Sequence[str] = ("git",),
        output_bytes: int = 1_000_000,
    ) -> None:
        if output_bytes < 1024:
            raise ValueError("trusted-local output limit must be at least 1024 bytes")
        self.python_argv = _fixed_argv(python_argv or (sys.executable,), name="python_argv")
        self.git_argv = _fixed_argv(git_argv, name="git_argv")
        self.output_bytes = int(output_bytes)
        self._last_workspace_transition: dict[str, str] | None = None

    @property
    def identity_payload(self) -> Mapping[str, Any]:
        return {
            "adapter": self.backend_id,
            "python": sys.version,
            "platform": platform.platform(),
            "python_argv": list(self.python_argv),
            "git_argv": list(self.git_argv),
        }

    def command_argv(self, command: str | Sequence[str]) -> tuple[str, ...]:
        if not isinstance(command, str):
            return _fixed_argv(command, name="command")
        if os.name == "nt":
            shell = os.environ.get("COMSPEC", "cmd.exe")
            return (shell, "/d", "/s", "/c", command)
        return ("/bin/sh", "-c", command)

    def run(self, request: IsolatedCommandRequest) -> IsolatedCommandResult:
        self._last_workspace_transition = None
        workspace_before = repository_snapshot_digest(request.workspace)
        working_directory = request.workspace
        if request.working_directory != ".":
            working_directory = request.workspace / request.working_directory
        container_name = f"agintor-trusted-local-{uuid.uuid4().hex}"
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                list(request.command),
                cwd=str(working_directory),
                env=dict(request.environment),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                shell=False,
                timeout=request.timeout_s,
                check=False,
                creationflags=_hidden_process_flags(),
            )
        except subprocess.TimeoutExpired as exc:
            stdout = _timeout_bytes(exc.stdout)
            stderr = _timeout_bytes(exc.stderr)
            result = _result(
                request=request,
                container_name=container_name,
                status=IsolatedCommandStatus.TIMED_OUT,
                exit_code=None,
                stdout=stdout,
                stderr=stderr,
                started=started,
                failure_detail=f"command exceeded {request.timeout_s}s deadline",
            )
            self._last_workspace_transition = _physical_workspace_transition(
                workspace_before,
                request.workspace,
            )
            return result
        except Exception as exc:
            result = _result(
                request=request,
                container_name=container_name,
                status=IsolatedCommandStatus.LAUNCH_FAILED,
                exit_code=None,
                stdout=b"",
                stderr=b"",
                started=started,
                failure_detail=str(exc),
            )
            self._last_workspace_transition = _physical_workspace_transition(
                workspace_before,
                request.workspace,
            )
            return result

        stdout = completed.stdout or b""
        stderr = completed.stderr or b""
        truncated = len(stdout) > self.output_bytes or len(stderr) > self.output_bytes
        status = IsolatedCommandStatus.OUTPUT_LIMIT if truncated else IsolatedCommandStatus.COMPLETED
        result = _result(
            request=request,
            container_name=container_name,
            status=status,
            exit_code=None if truncated else int(completed.returncode),
            stdout=stdout[: self.output_bytes],
            stderr=stderr[: self.output_bytes],
            started=started,
            output_truncated=truncated,
            failure_detail="trusted-local command exceeded output limit" if truncated else None,
        )
        self._last_workspace_transition = {
            **_physical_workspace_transition(workspace_before, request.workspace),
            "physical_before": workspace_before,
            "physical_after": repository_snapshot_digest(request.workspace),
        }
        return result

    @property
    def last_workspace_transition(self) -> Mapping[str, str] | None:
        if self._last_workspace_transition is None:
            return None
        return dict(self._last_workspace_transition)


def _fixed_argv(value: Sequence[str], *, name: str) -> tuple[str, ...]:
    argv = tuple(str(part) for part in value)
    if not argv or any(not part or "\x00" in part for part in argv):
        raise ValueError(f"{name} must be non-empty argv with NUL-free arguments")
    return argv


def _normalized_identity(value: Mapping[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, item in sorted(value.items(), key=lambda row: str(row[0])):
        if isinstance(item, Mapping):
            normalized[str(key)] = _normalized_identity(item)
        elif isinstance(item, (list, tuple, set, frozenset)):
            normalized[str(key)] = [str(part) for part in item]
        elif isinstance(item, (str, int, float, bool)) or item is None:
            normalized[str(key)] = item
        else:
            normalized[str(key)] = str(item)
    return normalized


def _last_backend_workspace_transition(backend: Any) -> dict[str, str] | None:
    raw = getattr(backend, "last_workspace_transition", None)
    if callable(raw):
        raw = raw()
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        return None
    before = str(raw.get("before", "") or "")
    after = str(raw.get("after", "") or "")
    source = str(raw.get("source", "") or "")
    if not before or not after or not source:
        return None
    return {"source": source, "before": before, "after": after}


def _physical_workspace_transition(before: str, workspace: Any) -> dict[str, str]:
    return {
        "source": "physical_workspace",
        "before": before,
        "after": repository_snapshot_digest(workspace),
    }


def _result(
    *,
    request: IsolatedCommandRequest,
    container_name: str,
    status: IsolatedCommandStatus,
    exit_code: int | None,
    stdout: bytes,
    stderr: bytes,
    started: float,
    output_truncated: bool = False,
    failure_detail: str | None = None,
) -> IsolatedCommandResult:
    return IsolatedCommandResult(
        status=status,
        command=request.command,
        container_name=container_name,
        exit_code=exit_code,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
        stdout_digest=hashlib.sha256(stdout).hexdigest(),
        stderr_digest=hashlib.sha256(stderr).hexdigest(),
        duration_s=max(time.perf_counter() - started, 0.0),
        output_truncated=output_truncated,
        failure_detail=failure_detail,
    )


def _timeout_bytes(value: str | bytes | None) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    return value.encode("utf-8", errors="replace")


def _hidden_process_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0


__all__ = [
    "IsolatedCommandBackend",
    "IsolatedRepoPatchCommandBackend",
    "RepoPatchExecutionBackend",
    "TrustedLocalRepoPatchCommandBackend",
]
