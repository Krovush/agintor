from __future__ import annotations

import hashlib
import os
import re
import subprocess
import threading
import time
import uuid
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_PINNED_IMAGE_RE = re.compile(r"^[a-zA-Z0-9._:/-]+@sha256:[0-9a-fA-F]{64}$")
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_SECRET_ENV_MARKERS = ("API_KEY", "AUTH", "CREDENTIAL", "PASSWORD", "SECRET", "TOKEN")
_DEFAULT_ENV_ALLOWLIST = frozenset({"LANG", "LC_ALL", "PYTHONHASHSEED", "TZ"})


class IsolatedCommandStatus(str, Enum):
    COMPLETED = "completed"
    TIMED_OUT = "timed_out"
    OUTPUT_LIMIT = "output_limit"
    LAUNCH_FAILED = "launch_failed"


class IsolatedCommandPolicy(BaseModel):
    """Immutable hard limits for one digest-pinned command-container policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    image: str
    user: str = "65532:65532"
    timeout_s: float = Field(default=30.0, gt=0.0, le=3600.0)
    memory_bytes: int = Field(default=512 * 1024 * 1024, ge=32 * 1024 * 1024)
    cpu_count: float = Field(default=1.0, gt=0.0, le=64.0)
    pids_limit: int = Field(default=128, ge=8, le=4096)
    output_bytes: int = Field(default=1_000_000, ge=1024, le=64_000_000)
    tmpfs_bytes: int = Field(default=64 * 1024 * 1024, ge=1024 * 1024)
    nofile_limit: int = Field(default=256, ge=32, le=65536)
    environment_allowlist: frozenset[str] = Field(default_factory=lambda: _DEFAULT_ENV_ALLOWLIST)

    @field_validator("image")
    @classmethod
    def _require_digest_pinned_image(cls, value: str) -> str:
        image = value.strip()
        if not _PINNED_IMAGE_RE.fullmatch(image):
            raise ValueError("isolated command image must be pinned as name@sha256:<64 hex chars>")
        return image

    @field_validator("user")
    @classmethod
    def _require_numeric_nonroot_user(cls, value: str) -> str:
        raw = value.strip()
        match = re.fullmatch(r"([0-9]+):([0-9]+)", raw)
        if match is None or int(match.group(1)) == 0 or int(match.group(2)) == 0:
            raise ValueError("isolated command user must be a numeric non-root uid:gid")
        return raw

    @field_validator("environment_allowlist")
    @classmethod
    def _validate_environment_allowlist(cls, value: frozenset[str]) -> frozenset[str]:
        normalized = frozenset(str(item).strip().upper() for item in value)
        for name in normalized:
            _validate_environment_name(name)
            _reject_secret_environment_name(name)
        return normalized


class IsolatedCommandRequest(BaseModel):
    """A shell-free command against one already-materialized scratch workspace."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command: tuple[str, ...]
    workspace: Path
    working_directory: str = "."
    environment: dict[str, str] = Field(default_factory=dict)
    timeout_s: float | None = Field(default=None, gt=0.0, le=3600.0)

    @field_validator("command")
    @classmethod
    def _validate_command(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("isolated command cannot be empty")
        normalized = tuple(str(part) for part in value)
        if any(not part or "\x00" in part for part in normalized):
            raise ValueError("isolated command arguments must be non-empty and NUL-free")
        return normalized

    @field_validator("working_directory")
    @classmethod
    def _validate_working_directory(cls, value: str) -> str:
        raw = value.replace("\\", "/").strip() or "."
        path = PurePosixPath(raw)
        if path.is_absolute() or any(part == ".." for part in path.parts):
            raise ValueError("working_directory must stay within the mounted workspace")
        return path.as_posix()

    @field_validator("environment")
    @classmethod
    def _validate_environment(cls, value: Mapping[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for key, item in value.items():
            name = str(key).strip().upper()
            _validate_environment_name(name)
            _reject_secret_environment_name(name)
            text = str(item)
            if "\x00" in text:
                raise ValueError(f"environment value for {name!r} contains NUL")
            normalized[name] = text
        return dict(sorted(normalized.items()))

    @model_validator(mode="after")
    def _validate_workspace(self) -> "IsolatedCommandRequest":
        workspace = self.workspace.expanduser().resolve()
        if not workspace.is_dir():
            raise ValueError(f"isolated command workspace is not a directory: {workspace}")
        if "," in str(workspace) or "\n" in str(workspace) or "\r" in str(workspace):
            raise ValueError("workspace path cannot contain comma or a line break")
        object.__setattr__(self, "workspace", workspace)
        return self


class IsolatedCommandResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: IsolatedCommandStatus
    command: tuple[str, ...]
    container_name: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    stdout_digest: str
    stderr_digest: str
    duration_s: float = Field(ge=0.0)
    output_truncated: bool = False
    failure_detail: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == IsolatedCommandStatus.COMPLETED and self.exit_code == 0


class IsolatedCommandBackend(Protocol):
    """Shared containment-only command boundary for runtime and evaluator use."""

    def run(self, request: IsolatedCommandRequest) -> IsolatedCommandResult:
        ...


class _BoundedBytes:
    def __init__(self, limit: int, overflow: threading.Event) -> None:
        self._limit = limit
        self._overflow = overflow
        self._chunks: list[bytes] = []
        self._size = 0
        self._lock = threading.Lock()

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        with self._lock:
            remaining = self._limit - self._size
            if remaining > 0:
                accepted = chunk[:remaining]
                self._chunks.append(accepted)
                self._size += len(accepted)
            if len(chunk) > max(remaining, 0):
                self._overflow.set()

    def value(self) -> bytes:
        with self._lock:
            return b"".join(self._chunks)


ProcessFactory = Callable[..., subprocess.Popen[bytes]]
CommandRunner = Callable[..., subprocess.CompletedProcess[bytes]]


class DockerCommandBackend:
    """Runs fixed argv commands in a no-network, resource-bounded Docker container.

    This class owns containment mechanics only. It does not know about runtime
    plans, tool authorization, public versus sealed fixtures, or evaluation.
    """

    def __init__(
        self,
        policy: IsolatedCommandPolicy,
        *,
        docker_executable: str = "docker",
        process_factory: ProcessFactory = subprocess.Popen,
        command_runner: CommandRunner = subprocess.run,
    ) -> None:
        self.policy = policy
        self.docker_executable = docker_executable
        self._process_factory = process_factory
        self._command_runner = command_runner

    def build_run_arguments(self, request: IsolatedCommandRequest, *, container_name: str) -> list[str]:
        environment = self._validated_environment(request.environment)
        timeout_s = min(request.timeout_s or self.policy.timeout_s, self.policy.timeout_s)
        del timeout_s  # Enforced by the supervising process, never delegated to candidate code.
        mount = f"type=bind,source={request.workspace},target=/workspace"
        working_directory = "/workspace"
        if request.working_directory != ".":
            working_directory = f"/workspace/{request.working_directory}"
        args = [
            self.docker_executable,
            "run",
            "--rm",
            "--init",
            "--pull",
            "never",
            "--name",
            container_name,
            "--stop-timeout",
            "1",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(self.policy.pids_limit),
            "--memory",
            str(self.policy.memory_bytes),
            "--memory-swap",
            str(self.policy.memory_bytes),
            "--cpus",
            str(self.policy.cpu_count),
            "--user",
            self.policy.user,
            "--ulimit",
            f"nofile={self.policy.nofile_limit}:{self.policy.nofile_limit}",
            "--mount",
            mount,
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,nodev,size={self.policy.tmpfs_bytes}",
            "--workdir",
            working_directory,
        ]
        for name, value in sorted(environment.items()):
            args.extend(["--env", f"{name}={value}"])
        args.extend([self.policy.image, *request.command])
        return args

    def run(self, request: IsolatedCommandRequest) -> IsolatedCommandResult:
        container_name = f"agintor-cmd-{uuid.uuid4().hex}"
        args = self.build_run_arguments(request, container_name=container_name)
        timeout_s = min(request.timeout_s or self.policy.timeout_s, self.policy.timeout_s)
        started = time.perf_counter()
        overflow = threading.Event()
        stdout_buffer = _BoundedBytes(self.policy.output_bytes, overflow)
        stderr_buffer = _BoundedBytes(self.policy.output_bytes, overflow)
        try:
            process = self._process_factory(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                creationflags=_hidden_process_flags(),
            )
        except Exception as exc:
            return self._result(
                request=request,
                container_name=container_name,
                status=IsolatedCommandStatus.LAUNCH_FAILED,
                exit_code=None,
                stdout=b"",
                stderr=b"",
                started=started,
                failure_detail=str(exc),
            )

        threads = [
            threading.Thread(target=_drain_stream, args=(process.stdout, stdout_buffer), daemon=True),
            threading.Thread(target=_drain_stream, args=(process.stderr, stderr_buffer), daemon=True),
        ]
        for thread in threads:
            thread.start()

        status = IsolatedCommandStatus.COMPLETED
        failure_detail: str | None = None
        deadline = started + timeout_s
        while process.poll() is None:
            if overflow.is_set():
                status = IsolatedCommandStatus.OUTPUT_LIMIT
                failure_detail = f"combined stream exceeded per-stream limit of {self.policy.output_bytes} bytes"
                self._force_remove(container_name)
                break
            if time.perf_counter() >= deadline:
                status = IsolatedCommandStatus.TIMED_OUT
                failure_detail = f"command exceeded {timeout_s:.3f}s deadline"
                self._force_remove(container_name)
                break
            time.sleep(0.01)

        if process.poll() is None:
            try:
                process.kill()
            except Exception:
                pass
        try:
            process.wait(timeout=5.0)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
        for thread in threads:
            thread.join(timeout=1.0)

        exit_code = process.returncode
        if status != IsolatedCommandStatus.COMPLETED and exit_code == 0:
            exit_code = None
        return self._result(
            request=request,
            container_name=container_name,
            status=status,
            exit_code=exit_code,
            stdout=stdout_buffer.value(),
            stderr=stderr_buffer.value(),
            started=started,
            output_truncated=overflow.is_set(),
            failure_detail=failure_detail,
        )

    def _validated_environment(self, environment: Mapping[str, str]) -> dict[str, str]:
        allowed = self.policy.environment_allowlist
        unexpected = sorted(set(environment) - set(allowed))
        if unexpected:
            raise ValueError(f"isolated command environment keys are not allowed: {unexpected}")
        return dict(environment)

    def _force_remove(self, container_name: str) -> None:
        try:
            self._command_runner(
                [self.docker_executable, "rm", "--force", container_name],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                check=False,
                timeout=10.0,
                creationflags=_hidden_process_flags(),
            )
        except Exception:
            # The supervised docker process is still killed below. A later Docker
            # daemon cleanup can remove a container only if the daemon ignored rm.
            pass

    @staticmethod
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


def _drain_stream(stream: BinaryIO | None, destination: _BoundedBytes) -> None:
    if stream is None:
        return
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                return
            destination.append(chunk)
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _validate_environment_name(name: str) -> None:
    if not _ENV_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid isolated environment variable name: {name!r}")


def _reject_secret_environment_name(name: str) -> None:
    if any(marker in name for marker in _SECRET_ENV_MARKERS):
        raise ValueError(f"secret-bearing environment variable is forbidden in command container: {name!r}")


def _hidden_process_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0


__all__ = [
    "DockerCommandBackend",
    "IsolatedCommandBackend",
    "IsolatedCommandPolicy",
    "IsolatedCommandRequest",
    "IsolatedCommandResult",
    "IsolatedCommandStatus",
]
