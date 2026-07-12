from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..authority.public_tasks import assert_public_payload
from ..contracts.epochs import TaskEnvelope
from ..contracts.run_evidence import assert_no_resolved_credentials
from ..core.identity import canonical_identity_digest
from ..repositories.workspaces import repository_snapshot_digest
from .commands import (
    IsolatedCommandPolicy,
    IsolatedCommandRequest,
    IsolatedCommandResult,
    IsolatedCommandStatus,
)


ISOLATED_COMMAND_REPLAY_SCHEMA_VERSION = "repo-repair-isolated-command-replay-v1"
MAX_ISOLATED_COMMAND_REPLAY_BYTES = 64 * 1024 * 1024

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_SECRET_ENV_MARKERS = ("API_KEY", "AUTH", "CREDENTIAL", "PASSWORD", "SECRET", "TOKEN")
_SHELL_EXECUTABLES = frozenset(
    {
        "bash",
        "bash.exe",
        "cmd",
        "cmd.exe",
        "dash",
        "fish",
        "ksh",
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
        "sh",
        "sh.exe",
        "zsh",
    }
)


class IsolatedCommandReplayModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)


class IsolatedCommandReplayError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        sequence_no: int | None = None,
    ) -> None:
        self.code = code
        self.sequence_no = sequence_no
        super().__init__(message)


def _require_digest(value: str, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _DIGEST_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return normalized


def _require_identifier(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not _IDENTIFIER_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a portable identifier")
    return normalized


def _json_payload(value: Any) -> Any:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    return value


def _assert_public_replay_payload(
    value: Any,
    *,
    forbidden_markers: Sequence[str | bytes] = (),
) -> None:
    payload = _json_payload(value)
    assert_no_resolved_credentials(payload)
    assert_public_payload(payload, canary_values=tuple(forbidden_markers))


def _validate_command(command: tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(str(part) for part in command)
    if not normalized or any(not part or "\x00" in part for part in normalized):
        raise ValueError("replay command arguments must be nonempty and NUL-free")
    executable = normalized[0].replace("\\", "/").rsplit("/", 1)[-1].casefold()
    if executable in _SHELL_EXECUTABLES:
        raise ValueError("shell execution is forbidden in isolated command replay")
    return normalized


def _validate_environment(value: Mapping[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for raw_name, raw_value in value.items():
        name = str(raw_name or "").strip().upper()
        if not _ENV_NAME_RE.fullmatch(name):
            raise ValueError("replay environment names must be portable uppercase identifiers")
        if any(marker in name for marker in _SECRET_ENV_MARKERS):
            raise ValueError("secret-bearing environment names are forbidden in command replay")
        text = str(raw_value)
        if "\x00" in text:
            raise ValueError("replay environment values may not contain NUL")
        assert_no_resolved_credentials({name: text})
        assert_public_payload({name: text})
        normalized[name] = text
    return dict(sorted(normalized.items()))


class IsolatedCommandReplayBinding(IsolatedCommandReplayModel):
    """Immutable authority for one command transcript and public workspace."""

    release_digest: str
    epoch_id: str
    epoch_manifest_digest: str
    task_envelope_digest: str
    workspace_snapshot_id: str
    workspace_snapshot_digest: str
    command_policy_digest: str

    @field_validator(
        "release_digest",
        "epoch_manifest_digest",
        "task_envelope_digest",
        "workspace_snapshot_digest",
        "command_policy_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @field_validator("epoch_id", "workspace_snapshot_id")
    @classmethod
    def validate_identifier(cls, value: str, info: Any) -> str:
        return _require_identifier(value, info.field_name)

    @classmethod
    def from_runtime_inputs(
        cls,
        *,
        release_digest: str,
        task: TaskEnvelope,
        command_policy_digest: str,
    ) -> "IsolatedCommandReplayBinding":
        normalized_task = TaskEnvelope.model_validate(_json_payload(task))
        return cls(
            release_digest=release_digest,
            epoch_id=normalized_task.epoch_id,
            epoch_manifest_digest=normalized_task.epoch_manifest_digest,
            task_envelope_digest=normalized_task.task_manifest_digest,
            workspace_snapshot_id=normalized_task.workspace_snapshot.snapshot_id,
            workspace_snapshot_digest=normalized_task.workspace_snapshot.digest,
            command_policy_digest=command_policy_digest,
        )


class IsolatedCommandReplayRequest(IsolatedCommandReplayModel):
    """Host-path-free identity for one materialized command request."""

    command: tuple[str, ...]
    workspace_content_digest: str
    working_directory: str
    environment: dict[str, str] = Field(default_factory=dict)
    timeout_s: float | None = Field(default=None, gt=0.0, le=3600.0)
    request_digest: str = ""

    @field_validator("command")
    @classmethod
    def validate_command(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_command(value)

    @field_validator("workspace_content_digest")
    @classmethod
    def validate_workspace_digest(cls, value: str) -> str:
        return _require_digest(value, "workspace_content_digest")

    @field_validator("working_directory")
    @classmethod
    def validate_working_directory(cls, value: str) -> str:
        normalized = str(value or ".").strip().replace("\\", "/") or "."
        parts = tuple(part for part in normalized.split("/") if part not in {"", "."})
        if normalized.startswith("/") or ".." in parts or ":" in normalized:
            raise ValueError("working_directory must remain relative to the replay workspace")
        return "/".join(parts) if parts else "."

    @field_validator("environment")
    @classmethod
    def validate_environment(cls, value: dict[str, str]) -> dict[str, str]:
        return _validate_environment(value)

    @model_validator(mode="after")
    def bind_request(self) -> "IsolatedCommandReplayRequest":
        payload = self.model_dump(mode="json", exclude={"request_digest"})
        digest = canonical_identity_digest(payload, domain="isolated-command-replay-request-v1")
        if self.request_digest and self.request_digest != digest:
            raise ValueError("isolated command replay request digest mismatch")
        if not self.request_digest:
            object.__setattr__(self, "request_digest", digest)
        _assert_public_replay_payload(self.model_dump(mode="json"))
        return self

    @classmethod
    def from_request(
        cls,
        request: IsolatedCommandRequest | Mapping[str, Any],
    ) -> "IsolatedCommandReplayRequest":
        normalized = IsolatedCommandRequest.model_validate(_json_payload(request))
        return cls(
            command=normalized.command,
            workspace_content_digest=repository_snapshot_digest(normalized.workspace),
            working_directory=normalized.working_directory,
            environment=normalized.environment,
            timeout_s=normalized.timeout_s,
        )


def _request_shape(request: IsolatedCommandReplayRequest) -> dict[str, Any]:
    return request.model_dump(
        mode="json",
        exclude={"workspace_content_digest", "request_digest"},
    )


def _text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class IsolatedCommandReplayRow(IsolatedCommandReplayModel):
    sequence_no: int = Field(ge=0)
    request: IsolatedCommandReplayRequest
    workspace_content_digest_after: str
    result: IsolatedCommandResult
    row_digest: str = ""

    @field_validator("workspace_content_digest_after")
    @classmethod
    def validate_workspace_digest_after(cls, value: str) -> str:
        return _require_digest(value, "workspace_content_digest_after")

    @model_validator(mode="after")
    def bind_row(self) -> "IsolatedCommandReplayRow":
        if tuple(self.result.command) != self.request.command:
            raise ValueError("replay result command differs from its request")
        if self.result.stdout_digest != _text_digest(self.result.stdout):
            raise ValueError("replay stdout digest differs from exact UTF-8 output")
        if self.result.stderr_digest != _text_digest(self.result.stderr):
            raise ValueError("replay stderr digest differs from exact UTF-8 output")
        status = IsolatedCommandStatus(self.result.status)
        if status is IsolatedCommandStatus.COMPLETED and self.result.exit_code is None:
            raise ValueError("completed replay command requires an exit code")
        if status in {IsolatedCommandStatus.TIMED_OUT, IsolatedCommandStatus.LAUNCH_FAILED}:
            if self.result.exit_code is not None:
                raise ValueError("timed-out or launch-failed replay cannot claim an exit code")
        if status is IsolatedCommandStatus.OUTPUT_LIMIT and not self.result.output_truncated:
            raise ValueError("output-limit replay must record truncated output")
        timeout_s = self.request.timeout_s
        if timeout_s is not None:
            if status is IsolatedCommandStatus.TIMED_OUT:
                if self.result.duration_s < timeout_s:
                    raise ValueError("timed-out replay duration precedes its request deadline")
            elif self.result.duration_s > timeout_s:
                raise ValueError("replay result duration exceeds the request deadline")
        payload = self.model_dump(mode="json", exclude={"row_digest"})
        digest = canonical_identity_digest(payload, domain="isolated-command-replay-row-v1")
        if self.row_digest and self.row_digest != digest:
            raise ValueError("isolated command replay row digest mismatch")
        if not self.row_digest:
            object.__setattr__(self, "row_digest", digest)
        _assert_public_replay_payload(self.model_dump(mode="json"))
        return self


class IsolatedCommandReplayManifest(IsolatedCommandReplayModel):
    schema_version: str = ISOLATED_COMMAND_REPLAY_SCHEMA_VERSION
    binding: IsolatedCommandReplayBinding
    rows: tuple[IsolatedCommandReplayRow, ...] = Field(min_length=1)
    manifest_digest: str = ""

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: str) -> str:
        if value != ISOLATED_COMMAND_REPLAY_SCHEMA_VERSION:
            raise ValueError(f"unsupported isolated command replay schema {value!r}")
        return value

    @model_validator(mode="after")
    def bind_manifest(self) -> "IsolatedCommandReplayManifest":
        sequence_numbers = tuple(row.sequence_no for row in self.rows)
        if sequence_numbers != tuple(range(len(self.rows))):
            raise ValueError("command replay rows must have contiguous ordered sequence numbers")
        for previous, current in zip(self.rows, self.rows[1:], strict=False):
            if (
                previous.workspace_content_digest_after
                != current.request.workspace_content_digest
            ):
                raise ValueError(
                    "command replay workspace transition differs from the next request"
                )
        payload = self.model_dump(mode="json", exclude={"manifest_digest"})
        digest = canonical_identity_digest(payload, domain="isolated-command-replay-manifest-v1")
        if self.manifest_digest and self.manifest_digest != digest:
            raise ValueError("isolated command replay manifest digest mismatch")
        if not self.manifest_digest:
            object.__setattr__(self, "manifest_digest", digest)
        _assert_public_replay_payload(self.model_dump(mode="json"))
        return self


class IsolatedCommandReplayReconciliation(IsolatedCommandReplayModel):
    manifest_digest: str
    row_count: int = Field(ge=0)
    consumed_count: int = Field(ge=0)
    remaining_row_digests: tuple[str, ...]
    complete: bool
    reconciliation_digest: str = ""

    @field_validator("manifest_digest")
    @classmethod
    def validate_manifest_digest(cls, value: str) -> str:
        return _require_digest(value, "manifest_digest")

    @field_validator("remaining_row_digests")
    @classmethod
    def validate_row_digests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_require_digest(item, "row digest") for item in value)

    @model_validator(mode="after")
    def bind_reconciliation(self) -> "IsolatedCommandReplayReconciliation":
        if self.consumed_count > self.row_count:
            raise ValueError("consumed command replay rows exceed the manifest")
        if len(self.remaining_row_digests) != self.row_count - self.consumed_count:
            raise ValueError("remaining command replay rows do not reconcile")
        if self.complete != (self.consumed_count == self.row_count):
            raise ValueError("command replay complete flag does not reconcile")
        payload = self.model_dump(mode="json", exclude={"reconciliation_digest"})
        digest = canonical_identity_digest(
            payload,
            domain="isolated-command-replay-reconciliation-v1",
        )
        if self.reconciliation_digest and self.reconciliation_digest != digest:
            raise ValueError("command replay reconciliation digest mismatch")
        if not self.reconciliation_digest:
            object.__setattr__(self, "reconciliation_digest", digest)
        return self


class IsolatedCommandReplayRecordingSink(Protocol):
    def capture_request(
        self,
        request: IsolatedCommandRequest,
    ) -> IsolatedCommandReplayRequest:
        ...

    def record(
        self,
        *,
        request: IsolatedCommandRequest | IsolatedCommandReplayRequest,
        result: IsolatedCommandResult,
        workspace_after: Path | None = None,
    ) -> IsolatedCommandReplayRow:
        ...


class IsolatedCommandReplayRecorder:
    def __init__(self, binding: IsolatedCommandReplayBinding) -> None:
        self.binding = IsolatedCommandReplayBinding.model_validate(_json_payload(binding))
        self._rows: list[IsolatedCommandReplayRow] = []
        self._lock = threading.RLock()
        self._finalized = False

    def capture_request(
        self,
        request: IsolatedCommandRequest,
    ) -> IsolatedCommandReplayRequest:
        """Freeze mutable workspace identity before dispatching a command."""

        captured = IsolatedCommandReplayRequest.from_request(request)
        with self._lock:
            if self._finalized:
                raise IsolatedCommandReplayError(
                    "recorder_finalized",
                    "isolated command replay recorder is already finalized",
                )
            if self._rows:
                previous = self._rows[-1]
                if (
                    previous.workspace_content_digest_after
                    != captured.workspace_content_digest
                ):
                    self._rows[-1] = IsolatedCommandReplayRow(
                        sequence_no=previous.sequence_no,
                        request=previous.request,
                        workspace_content_digest_after=(
                            captured.workspace_content_digest
                        ),
                        result=previous.result,
                    )
        return captured

    def record(
        self,
        *,
        request: IsolatedCommandRequest | IsolatedCommandReplayRequest,
        result: IsolatedCommandResult,
        workspace_after: Path | None = None,
    ) -> IsolatedCommandReplayRow:
        replay_request = (
            IsolatedCommandReplayRequest.model_validate(_json_payload(request))
            if isinstance(request, IsolatedCommandReplayRequest)
            else self.capture_request(request)
        )
        normalized_result = IsolatedCommandResult.model_validate(_json_payload(result))
        with self._lock:
            if self._finalized:
                raise IsolatedCommandReplayError(
                    "recorder_finalized",
                    "isolated command replay recorder is already finalized",
                )
            row = IsolatedCommandReplayRow(
                sequence_no=len(self._rows),
                request=replay_request,
                workspace_content_digest_after=(
                    repository_snapshot_digest(workspace_after)
                    if workspace_after is not None
                    else replay_request.workspace_content_digest
                ),
                result=normalized_result,
            )
            self._rows.append(row)
            return row

    def snapshot(self) -> IsolatedCommandReplayManifest:
        with self._lock:
            return IsolatedCommandReplayManifest(binding=self.binding, rows=tuple(self._rows))

    def finalize(self) -> IsolatedCommandReplayManifest:
        with self._lock:
            manifest = IsolatedCommandReplayManifest(binding=self.binding, rows=tuple(self._rows))
            self._finalized = True
            return manifest


class IsolatedCommandReplayBackend:
    """Ordered, single-use, host-path-independent isolated command replay."""

    def __init__(
        self,
        manifest: IsolatedCommandReplayManifest | Mapping[str, Any],
        *,
        expected_binding: IsolatedCommandReplayBinding | Mapping[str, Any],
        policy: IsolatedCommandPolicy | Mapping[str, Any],
    ) -> None:
        self.manifest = IsolatedCommandReplayManifest.model_validate(_json_payload(manifest))
        expected = IsolatedCommandReplayBinding.model_validate(_json_payload(expected_binding))
        if self.manifest.binding != expected:
            raise IsolatedCommandReplayError(
                "identity_mismatch",
                "command replay manifest differs from its runtime authority binding",
            )
        self.binding = expected
        self.policy = IsolatedCommandPolicy.model_validate(_json_payload(policy))
        self._cursor = 0
        self._cancelled = False
        self._physical_workspace_digest: str | None = None
        self._virtual_workspace_digest: str | None = None
        self._last_workspace_transition: dict[str, str] | None = None
        self._lock = threading.RLock()

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True

    def run(self, request: IsolatedCommandRequest) -> IsolatedCommandResult:
        try:
            replay_request = IsolatedCommandReplayRequest.from_request(request)
        except Exception as exc:
            raise IsolatedCommandReplayError(
                "invalid_request",
                "isolated command replay request failed validation",
                sequence_no=self._cursor,
            ) from exc
        with self._lock:
            if self._cancelled:
                raise IsolatedCommandReplayError(
                    "cancelled",
                    "isolated command replay was cancelled before dispatch",
                    sequence_no=self._cursor,
                )
            if self._cursor >= len(self.manifest.rows):
                raise IsolatedCommandReplayError(
                    "manifest_reused",
                    "isolated command replay manifest was already fully consumed",
                    sequence_no=self._cursor,
                )
            row = self.manifest.rows[self._cursor]
            if self._virtual_workspace_digest is None:
                self._virtual_workspace_digest = row.request.workspace_content_digest
                self._physical_workspace_digest = replay_request.workspace_content_digest
            assert self._physical_workspace_digest is not None
            physical_digest = replay_request.workspace_content_digest
            expected_workspace_digest = row.request.workspace_content_digest
            if self._cursor == 0 and physical_digest != expected_workspace_digest:
                raise IsolatedCommandReplayError(
                    "workspace_state_mismatch",
                    "isolated command replay initial workspace differs from its recording",
                    sequence_no=row.sequence_no,
                )
            if physical_digest == expected_workspace_digest:
                # A side effect outside this replay backend was reproduced in
                # the real scratch tree between recorded command requests.
                self._physical_workspace_digest = physical_digest
                self._virtual_workspace_digest = expected_workspace_digest
            elif not (
                physical_digest == self._physical_workspace_digest
                and expected_workspace_digest == self._virtual_workspace_digest
            ):
                raise IsolatedCommandReplayError(
                    "workspace_state_mismatch",
                    "isolated command replay workspace crossed recorded or virtual state",
                    sequence_no=row.sequence_no,
                )
            if (
                _request_shape(row.request) != _request_shape(replay_request)
            ):
                raise IsolatedCommandReplayError(
                    "request_mismatch",
                    "isolated command request differs from the next replay row",
                    sequence_no=row.sequence_no,
                )
            self._cursor += 1
            self._last_workspace_transition = {
                "source": "replay_manifest",
                "before": row.request.workspace_content_digest,
                "after": row.workspace_content_digest_after,
            }
            self._virtual_workspace_digest = row.workspace_content_digest_after
            return IsolatedCommandResult.model_validate(row.result.model_dump(mode="json"))

    @property
    def last_workspace_transition(self) -> Mapping[str, str] | None:
        if self._last_workspace_transition is None:
            return None
        return dict(self._last_workspace_transition)

    def reconciliation(self) -> IsolatedCommandReplayReconciliation:
        with self._lock:
            remaining = self.manifest.rows[self._cursor :]
            return IsolatedCommandReplayReconciliation(
                manifest_digest=self.manifest.manifest_digest,
                row_count=len(self.manifest.rows),
                consumed_count=self._cursor,
                remaining_row_digests=tuple(row.row_digest for row in remaining),
                complete=self._cursor == len(self.manifest.rows),
            )

    def assert_reconciled(self) -> IsolatedCommandReplayReconciliation:
        reconciliation = self.reconciliation()
        if not reconciliation.complete:
            raise IsolatedCommandReplayError(
                "extra_rows",
                "command replay completed with unconsumed manifest rows",
                sequence_no=reconciliation.consumed_count,
            )
        return reconciliation


def _json_object_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"isolated command replay JSON contains duplicate key {key!r}")
        value[key] = item
    return value


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _manifest_bytes(manifest: IsolatedCommandReplayManifest) -> bytes:
    return (
        json.dumps(
            manifest.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def load_isolated_command_replay_manifest(
    path: str | Path,
    *,
    forbidden_markers: Sequence[str | bytes] = (),
    max_bytes: int = MAX_ISOLATED_COMMAND_REPLAY_BYTES,
) -> IsolatedCommandReplayManifest:
    manifest_path = Path(path)
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if manifest_path.is_symlink():
        raise ValueError("isolated command replay manifest may not be a symbolic link")
    if not manifest_path.is_file():
        raise FileNotFoundError(f"isolated command replay manifest is missing: {manifest_path}")
    if manifest_path.stat().st_size > max_bytes:
        raise ValueError("isolated command replay manifest exceeds the configured byte limit")
    raw = manifest_path.read_bytes()
    if len(raw) > max_bytes:
        raise ValueError("isolated command replay manifest exceeds the configured byte limit")
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_json_object_no_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("isolated command replay manifest is not valid UTF-8 JSON") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("isolated command replay manifest root must be an object")
    _assert_public_replay_payload(payload, forbidden_markers=forbidden_markers)
    return IsolatedCommandReplayManifest.model_validate(payload)


def write_isolated_command_replay_manifest(
    path: str | Path,
    manifest: IsolatedCommandReplayManifest | Mapping[str, Any],
    *,
    forbidden_markers: Sequence[str | bytes] = (),
) -> Path:
    """Atomically create an immutable transcript, permitting idempotent writes only."""

    normalized = IsolatedCommandReplayManifest.model_validate(_json_payload(manifest))
    _assert_public_replay_payload(normalized, forbidden_markers=forbidden_markers)
    payload = _manifest_bytes(normalized)
    if len(payload) > MAX_ISOLATED_COMMAND_REPLAY_BYTES:
        raise ValueError("isolated command replay manifest exceeds the configured byte limit")
    manifest_path = Path(path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    if manifest_path.is_symlink():
        raise ValueError("isolated command replay manifest may not be a symbolic link")
    temporary = manifest_path.parent / f".{manifest_path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, manifest_path)
        except FileExistsError:
            existing = load_isolated_command_replay_manifest(
                manifest_path,
                forbidden_markers=forbidden_markers,
            )
            if existing.model_dump(mode="json") != normalized.model_dump(mode="json"):
                raise FileExistsError(
                    f"immutable isolated command replay manifest already exists: {manifest_path}"
                )
        _fsync_directory(manifest_path.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return manifest_path


__all__ = [
    "ISOLATED_COMMAND_REPLAY_SCHEMA_VERSION",
    "MAX_ISOLATED_COMMAND_REPLAY_BYTES",
    "IsolatedCommandReplayBackend",
    "IsolatedCommandReplayBinding",
    "IsolatedCommandReplayError",
    "IsolatedCommandReplayManifest",
    "IsolatedCommandReplayRecorder",
    "IsolatedCommandReplayRecordingSink",
    "IsolatedCommandReplayReconciliation",
    "IsolatedCommandReplayRequest",
    "IsolatedCommandReplayRow",
    "load_isolated_command_replay_manifest",
    "write_isolated_command_replay_manifest",
]
