from __future__ import annotations

import json
import os
import re
import secrets
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..contracts.harness import (
    HarnessPublicCarryoverRef,
    HarnessPublicSessionContext,
    HarnessPublicSessionLimits,
)
from ..core.identity import evidence_digest
from ..core.versioning import RUNTIME_CONTRACT_VERSION


HARNESS_SESSION_SCHEMA_VERSION = "harness-runtime-session-v1"
HARNESS_SESSION_MESSAGE_SCHEMA_VERSION = "harness-runtime-session-message-v1"
HARNESS_SESSION_PREPARE_SCHEMA_VERSION = "harness-runtime-session-prepare-v1"
HARNESS_SESSION_COMMIT_SCHEMA_VERSION = "harness-runtime-session-commit-v1"
HARNESS_SESSION_CONTEXT_SCHEMA_VERSION = "harness-runtime-session-next-context-v1"
HARNESS_RUNTIME_KIND = "harness"
HARNESS_SESSIONS_DIR_NAME = ".runtime_sessions"

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_SECRET_VALUE_PATTERNS = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
_FORBIDDEN_PUBLIC_TEXT_FRAGMENTS = (
    "api_key",
    "authorization",
    "bearer ",
    "credential",
    "evaluator",
    "full_context",
    "hidden",
    "long_term",
    "password",
    "predictor",
    "private_key",
    "raw_patch",
    "repository_snapshot",
    "sealed",
    "source_uri",
    "token",
    "workspace_snapshot",
)
_FORBIDDEN_PUBLIC_TEXT_PHRASES = (
    "full context",
    "pre-call context",
    "raw patch",
    "repository snapshot",
    "workspace snapshot",
)


class HarnessSessionError(RuntimeError):
    """Base class for V1 harness runtime-session failures."""


class HarnessSessionReleaseMismatchError(HarnessSessionError):
    """Raised when a session is opened through a different active release."""


class HarnessSessionConcurrencyError(HarnessSessionError):
    """Raised when another writer owns the session lock."""


class HarnessSessionVersionError(HarnessSessionError):
    """Raised when optimistic append sequencing does not match the manifest."""


class HarnessSessionValidationError(HarnessSessionError):
    """Raised when a public-safe session boundary is invalid."""


class HarnessSessionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _require_digest(value: str, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _DIGEST_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return normalized


def _require_nonempty(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} may not be empty")
    return normalized


def _assert_safe_session_id(session_id: str) -> str:
    normalized = str(session_id or "").strip()
    if (
        not _SESSION_ID_RE.fullmatch(normalized)
        or normalized.startswith(".")
        or ".." in normalized
        or "/" in normalized
        or "\\" in normalized
    ):
        raise HarnessSessionValidationError(f"invalid harness session id {session_id!r}")
    return normalized


def _safe_child(root: Path, *parts: str) -> Path:
    candidate = root.joinpath(*parts).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise HarnessSessionValidationError("session path escapes .runtime_sessions") from exc
    return candidate


def _safe_artifact_ref(value: str) -> str:
    normalized = str(value or "").strip().replace("\\", "/")
    if not normalized:
        raise ValueError("artifact_ref may not be empty")
    if "://" in normalized or normalized.startswith(("/", ".")):
        raise ValueError("artifact_ref may not traverse or reference absolute/hidden filesystem state")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or any(part.startswith(".") for part in path.parts):
        raise ValueError("artifact_ref may not traverse or hide filesystem state")
    return normalized


def _assert_public_safe_text(
    value: str,
    *,
    field_name: str,
) -> None:
    normalized = str(value)
    lowered = normalized.casefold()
    compact = re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")
    if any(pattern.search(normalized) for pattern in _SECRET_VALUE_PATTERNS):
        raise HarnessSessionValidationError(f"{field_name} contains resolved credential material")
    for fragment in _FORBIDDEN_PUBLIC_TEXT_FRAGMENTS:
        if fragment in compact or fragment in lowered:
            raise HarnessSessionValidationError(f"{field_name} references non-public session state: {fragment}")
    for phrase in _FORBIDDEN_PUBLIC_TEXT_PHRASES:
        if phrase in lowered:
            raise HarnessSessionValidationError(f"{field_name} references non-public session state: {phrase}")


class HarnessSessionLimits(HarnessSessionModel):
    max_entries: int = Field(default=8, ge=0, le=64)
    max_total_bytes: int = Field(default=4096, ge=0, le=262_144)
    max_summary_bytes: int = Field(default=512, ge=0, le=16_384)


class HarnessCarryoverRef(HarnessSessionModel):
    artifact_ref: str
    artifact_digest: str
    summary: str
    public_safe: Literal[True] = True
    carryover_digest: str = ""

    @field_validator("artifact_ref")
    @classmethod
    def validate_artifact_ref(cls, value: str) -> str:
        return _safe_artifact_ref(value)

    @field_validator("artifact_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _require_digest(value, "artifact_digest")

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        return _require_nonempty(value, "summary")

    @model_validator(mode="after")
    def bind_digest(self) -> "HarnessCarryoverRef":
        computed = harness_carryover_ref_digest(self)
        if self.carryover_digest and self.carryover_digest != computed:
            raise ValueError("carryover digest does not match the public artifact reference")
        if not self.carryover_digest:
            object.__setattr__(self, "carryover_digest", computed)
        return self


class HarnessSessionManifest(HarnessSessionModel):
    schema_version: Literal[HARNESS_SESSION_SCHEMA_VERSION] = HARNESS_SESSION_SCHEMA_VERSION
    runtime_contract_version: str = RUNTIME_CONTRACT_VERSION
    session_id: str
    runtime_kind: Literal["harness"] = HARNESS_RUNTIME_KIND
    project_dir: str
    active_release_digest: str
    limits: HarnessSessionLimits = Field(default_factory=HarnessSessionLimits)
    created_at_ms: int = Field(ge=0)
    version: int = Field(default=0, ge=0)
    message_count: int = Field(default=0, ge=0)
    next_sequence: int = Field(default=0, ge=0)
    last_message_id: str | None = None
    carryover: tuple[HarnessCarryoverRef, ...] = ()
    manifest_digest: str = ""

    @field_validator("runtime_contract_version")
    @classmethod
    def validate_contract_version(cls, value: str) -> str:
        if str(value) != RUNTIME_CONTRACT_VERSION:
            raise ValueError("harness session runtime contract version mismatch")
        return str(value)

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str) -> str:
        return _assert_safe_session_id(value)

    @field_validator("project_dir")
    @classmethod
    def validate_project_dir(cls, value: str) -> str:
        return _require_nonempty(value, "project_dir")

    @field_validator("active_release_digest")
    @classmethod
    def validate_release_digest(cls, value: str) -> str:
        return _require_digest(value, "active_release_digest")

    @field_validator("last_message_id")
    @classmethod
    def validate_optional_message_id(cls, value: str | None) -> str | None:
        return None if value is None else _require_nonempty(value, "last_message_id")

    @model_validator(mode="after")
    def validate_manifest(self) -> "HarnessSessionManifest":
        if self.version != self.message_count or self.next_sequence != self.message_count:
            raise ValueError("session version, message_count, and next_sequence must move together")
        if self.message_count == 0 and self.last_message_id is not None:
            raise ValueError("new sessions may not carry a prior message id")
        if self.message_count > 0 and self.last_message_id is None:
            raise ValueError("nonempty sessions require a last_message_id")
        computed = harness_session_manifest_digest(self)
        if self.manifest_digest and self.manifest_digest != computed:
            raise ValueError("harness session manifest digest mismatch")
        if not self.manifest_digest:
            object.__setattr__(self, "manifest_digest", computed)
        return self


class HarnessSessionMessage(HarnessSessionModel):
    schema_version: Literal[HARNESS_SESSION_MESSAGE_SCHEMA_VERSION] = HARNESS_SESSION_MESSAGE_SCHEMA_VERSION
    runtime_contract_version: str = RUNTIME_CONTRACT_VERSION
    session_id: str
    message_id: str
    parent_message_id: str | None = None
    sequence: int = Field(ge=0)
    base_version: int = Field(ge=0)
    active_release_digest: str
    runtime_kind: Literal["harness"] = HARNESS_RUNTIME_KIND
    message_summary: str
    carryover: tuple[HarnessCarryoverRef, ...] = ()
    created_at_ms: int = Field(ge=0)
    message_digest: str = ""

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str) -> str:
        return _assert_safe_session_id(value)

    @field_validator("message_id", "message_summary")
    @classmethod
    def validate_nonempty(cls, value: str, info: Any) -> str:
        return _require_nonempty(value, info.field_name)

    @field_validator("parent_message_id")
    @classmethod
    def validate_optional_message_id(cls, value: str | None) -> str | None:
        return None if value is None else _require_nonempty(value, "parent_message_id")

    @field_validator("active_release_digest")
    @classmethod
    def validate_release_digest(cls, value: str) -> str:
        return _require_digest(value, "active_release_digest")

    @model_validator(mode="after")
    def bind_digest(self) -> "HarnessSessionMessage":
        computed = harness_session_message_digest(self)
        if self.message_digest and self.message_digest != computed:
            raise ValueError("harness session message digest mismatch")
        if not self.message_digest:
            object.__setattr__(self, "message_digest", computed)
        return self


class HarnessSessionPrepare(HarnessSessionModel):
    schema_version: Literal[HARNESS_SESSION_PREPARE_SCHEMA_VERSION] = HARNESS_SESSION_PREPARE_SCHEMA_VERSION
    prepare_id: str
    session_id: str
    base_manifest_digest: str
    base_version: int = Field(ge=0)
    message: HarnessSessionMessage
    next_manifest: HarnessSessionManifest
    prepare_digest: str = ""

    @field_validator("prepare_id", "session_id")
    @classmethod
    def validate_nonempty(cls, value: str, info: Any) -> str:
        return _require_nonempty(value, info.field_name)

    @field_validator("base_manifest_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _require_digest(value, "base_manifest_digest")

    @model_validator(mode="after")
    def validate_prepare(self) -> "HarnessSessionPrepare":
        if self.message.session_id != self.session_id or self.next_manifest.session_id != self.session_id:
            raise ValueError("prepared session message crossed session identity")
        if self.message.base_version != self.base_version:
            raise ValueError("prepared message crossed base version")
        if self.next_manifest.version != self.base_version + 1:
            raise ValueError("prepared next manifest must advance exactly one version")
        if self.next_manifest.last_message_id != self.message.message_id:
            raise ValueError("prepared manifest must point at the prepared message")
        if self.next_manifest.carryover != self.message.carryover:
            raise ValueError("prepared manifest carryover must equal message boundary carryover")
        computed = harness_session_prepare_digest(self)
        if self.prepare_digest and self.prepare_digest != computed:
            raise ValueError("harness session prepare digest mismatch")
        if not self.prepare_digest:
            object.__setattr__(self, "prepare_digest", computed)
        return self


class HarnessSessionCommit(HarnessSessionModel):
    schema_version: Literal[HARNESS_SESSION_COMMIT_SCHEMA_VERSION] = HARNESS_SESSION_COMMIT_SCHEMA_VERSION
    commit_id: str
    session_id: str
    prepare_digest: str
    message_digest: str
    next_manifest_digest: str
    committed_at_ms: int = Field(ge=0)
    commit_digest: str = ""

    @field_validator("commit_id", "session_id")
    @classmethod
    def validate_nonempty(cls, value: str, info: Any) -> str:
        return _require_nonempty(value, info.field_name)

    @field_validator("prepare_digest", "message_digest", "next_manifest_digest")
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @model_validator(mode="after")
    def bind_digest(self) -> "HarnessSessionCommit":
        computed = harness_session_commit_digest(self)
        if self.commit_digest and self.commit_digest != computed:
            raise ValueError("harness session commit digest mismatch")
        if not self.commit_digest:
            object.__setattr__(self, "commit_digest", computed)
        return self


class HarnessNextContext(HarnessSessionModel):
    schema_version: Literal[HARNESS_SESSION_CONTEXT_SCHEMA_VERSION] = HARNESS_SESSION_CONTEXT_SCHEMA_VERSION
    runtime_contract_version: str = RUNTIME_CONTRACT_VERSION
    session_id: str
    active_release_digest: str
    session_manifest_digest: str
    runtime_kind: Literal["harness"] = HARNESS_RUNTIME_KIND
    parent_message_id: str | None
    next_sequence: int = Field(ge=0)
    limits: HarnessSessionLimits = Field(default_factory=HarnessSessionLimits)
    carryover: tuple[HarnessCarryoverRef, ...] = ()
    context_digest: str = ""

    @field_validator("session_manifest_digest")
    @classmethod
    def validate_session_manifest_digest(cls, value: str) -> str:
        return _require_digest(value, "session_manifest_digest")

    @model_validator(mode="after")
    def bind_digest(self) -> "HarnessNextContext":
        computed = evidence_digest(
            {
                "kind": HARNESS_SESSION_CONTEXT_SCHEMA_VERSION,
                "context": self.model_dump(mode="python", exclude={"context_digest"}),
            }
        )
        if self.context_digest and self.context_digest != computed:
            raise ValueError("harness next-context digest mismatch")
        if not self.context_digest:
            object.__setattr__(self, "context_digest", computed)
        return self

    def to_public_runtime_context(self) -> HarnessPublicSessionContext:
        return HarnessPublicSessionContext(
            session_id=self.session_id,
            active_release_digest=self.active_release_digest,
            session_manifest_digest=self.session_manifest_digest,
            parent_message_id=self.parent_message_id,
            next_sequence=self.next_sequence,
            limits=HarnessPublicSessionLimits(
                max_entries=self.limits.max_entries,
                max_total_bytes=self.limits.max_total_bytes,
                max_summary_bytes=self.limits.max_summary_bytes,
            ),
            carryover=tuple(
                HarnessPublicCarryoverRef(
                    artifact_ref=entry.artifact_ref,
                    artifact_digest=entry.artifact_digest,
                    summary=entry.summary,
                )
                for entry in self.carryover
            ),
        )


def harness_carryover_ref_digest(ref: HarnessCarryoverRef | Mapping[str, Any]) -> str:
    payload = ref.model_dump(mode="python", exclude={"carryover_digest"}) if isinstance(ref, HarnessCarryoverRef) else dict(ref)
    payload.pop("carryover_digest", None)
    return evidence_digest({"kind": "harness-carryover-ref", "ref": payload})


def harness_session_manifest_digest(manifest: HarnessSessionManifest | Mapping[str, Any]) -> str:
    payload = manifest.model_dump(mode="python", exclude_none=True) if isinstance(manifest, HarnessSessionManifest) else dict(manifest)
    payload.pop("manifest_digest", None)
    return evidence_digest({"kind": HARNESS_SESSION_SCHEMA_VERSION, "manifest": payload})


def harness_session_message_digest(message: HarnessSessionMessage | Mapping[str, Any]) -> str:
    payload = message.model_dump(mode="python", exclude_none=True) if isinstance(message, HarnessSessionMessage) else dict(message)
    payload.pop("message_digest", None)
    return evidence_digest({"kind": HARNESS_SESSION_MESSAGE_SCHEMA_VERSION, "message": payload})


def harness_session_prepare_digest(prepare: HarnessSessionPrepare | Mapping[str, Any]) -> str:
    payload = prepare.model_dump(mode="python", exclude_none=True) if isinstance(prepare, HarnessSessionPrepare) else dict(prepare)
    payload.pop("prepare_digest", None)
    return evidence_digest({"kind": HARNESS_SESSION_PREPARE_SCHEMA_VERSION, "prepare": payload})


def harness_session_commit_digest(commit: HarnessSessionCommit | Mapping[str, Any]) -> str:
    payload = commit.model_dump(mode="python", exclude_none=True) if isinstance(commit, HarnessSessionCommit) else dict(commit)
    payload.pop("commit_digest", None)
    return evidence_digest({"kind": HARNESS_SESSION_COMMIT_SCHEMA_VERSION, "commit": payload})


class HarnessSessionStore:
    def __init__(
        self,
        project_dir: str | Path,
        *,
        limits: HarnessSessionLimits | None = None,
    ) -> None:
        self.project_dir = Path(project_dir).resolve()
        self.root = self.project_dir / HARNESS_SESSIONS_DIR_NAME
        self.default_limits = limits or HarnessSessionLimits()

    def create_session(
        self,
        *,
        active_release_digest: str,
        session_id: str | None = None,
        limits: HarnessSessionLimits | None = None,
    ) -> HarnessSessionManifest:
        release_digest = _require_digest(active_release_digest, "active_release_digest")
        allocated = _assert_safe_session_id(session_id) if session_id else self._allocate_session_id(release_digest)
        session_dir = self.session_dir(allocated)
        try:
            session_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise HarnessSessionValidationError(f"harness session {allocated!r} already exists") from exc
        (session_dir / "messages").mkdir()
        (session_dir / "wal").mkdir()
        manifest = HarnessSessionManifest(
            session_id=allocated,
            project_dir=str(self.project_dir),
            active_release_digest=release_digest,
            limits=limits or self.default_limits,
            created_at_ms=_now_ms(),
        )
        self._write_json_once(self._manifest_path(allocated), manifest.model_dump(mode="json", exclude_none=True))
        return manifest

    def load_for_continuation(
        self,
        session_id: str,
        *,
        active_release_digest: str,
    ) -> HarnessSessionManifest:
        manifest = self.recover(session_id)
        self._assert_release(manifest, active_release_digest)
        return manifest

    def append_message(
        self,
        session_id: str,
        *,
        active_release_digest: str,
        expected_version: int,
        message_summary: str,
        carryover: Sequence[HarnessCarryoverRef | Mapping[str, Any]] = (),
    ) -> HarnessSessionMessage:
        session_id = _assert_safe_session_id(session_id)
        with self._writer_lock(session_id):
            manifest = self._recover_unlocked(session_id)
            self._assert_release(manifest, active_release_digest)
            if expected_version != manifest.version:
                raise HarnessSessionVersionError(
                    f"harness session {session_id!r} expected version {expected_version}, "
                    f"but current version is {manifest.version}"
                )
            _assert_public_safe_text(
                message_summary,
                field_name="message_summary",
            )
            summary_size = len(message_summary.encode("utf-8"))
            if summary_size > manifest.limits.max_summary_bytes:
                raise HarnessSessionValidationError("message_summary exceeds the configured summary byte limit")
            normalized = self._normalize_carryover(carryover, manifest.limits)
            message_id = self._message_id(manifest, message_summary, normalized)
            message = HarnessSessionMessage(
                session_id=session_id,
                message_id=message_id,
                parent_message_id=manifest.last_message_id,
                sequence=manifest.next_sequence,
                base_version=manifest.version,
                active_release_digest=manifest.active_release_digest,
                message_summary=message_summary,
                carryover=normalized,
                created_at_ms=_now_ms(),
            )
            next_manifest = self._next_manifest(manifest, message)
            prepare = HarnessSessionPrepare(
                prepare_id=self._prepare_id(message),
                session_id=session_id,
                base_manifest_digest=manifest.manifest_digest,
                base_version=manifest.version,
                message=message,
                next_manifest=next_manifest,
            )
            self._write_prepare(prepare)
            self._write_message_file(message)
            commit = HarnessSessionCommit(
                commit_id=self._commit_id(message),
                session_id=session_id,
                prepare_digest=prepare.prepare_digest,
                message_digest=message.message_digest,
                next_manifest_digest=next_manifest.manifest_digest,
                committed_at_ms=_now_ms(),
            )
            self._write_commit(commit)
            self._write_manifest(next_manifest)
            return message

    def context_for_next(
        self,
        session_id: str,
        *,
        active_release_digest: str,
    ) -> HarnessNextContext:
        manifest = self.load_for_continuation(
            session_id,
            active_release_digest=active_release_digest,
        )
        return HarnessNextContext(
            session_id=manifest.session_id,
            active_release_digest=manifest.active_release_digest,
            session_manifest_digest=manifest.manifest_digest,
            parent_message_id=manifest.last_message_id,
            next_sequence=manifest.next_sequence,
            limits=manifest.limits,
            carryover=manifest.carryover,
        )

    def append_solve_result(
        self,
        session_id: str,
        *,
        active_release_digest: str,
        expected_version: int,
        task: Any,
        solve_result: Any,
    ) -> HarnessSessionMessage:
        from ..contracts.epochs import TaskEnvelope
        from ..runtime.sdk.harness_executor import HarnessSolveResult

        try:
            normalized_task = TaskEnvelope.model_validate(
                task.model_dump(mode="python") if isinstance(task, TaskEnvelope) else task
            )
            normalized_result = HarnessSolveResult.model_validate(
                solve_result.model_dump(mode="python")
                if isinstance(solve_result, HarnessSolveResult)
                else solve_result
            )
        except Exception as exc:
            raise HarnessSessionValidationError(
                "solve result session append requires typed public task and result contracts"
            ) from exc
        release_digest = _require_digest(active_release_digest, "active_release_digest")
        if normalized_result.release.release_digest != release_digest:
            raise HarnessSessionReleaseMismatchError(
                "solve result belongs to a different immutable release; start a new session"
            )
        if normalized_result.task.task_envelope_digest != normalized_task.task_manifest_digest:
            raise HarnessSessionValidationError("solve result crossed task identity")
        verification_status = normalized_result.public_verification.status
        message_summary = (
            f"Public solve {normalized_result.run_id} ended {normalized_result.status}; "
            f"public verification {verification_status}."
        )
        carryover_summary = (
            f"Public solve result {normalized_result.run_id}: "
            f"status {normalized_result.status}, verification {verification_status}."
        )
        return self.append_message(
            session_id,
            active_release_digest=release_digest,
            expected_version=expected_version,
            message_summary=message_summary,
            carryover=(
                HarnessCarryoverRef(
                    artifact_ref=f"runs/{normalized_result.run_id}/harness_solve_result.json",
                    artifact_digest=normalized_result.result_digest,
                    summary=carryover_summary,
                ),
            ),
        )

    def recover(
        self,
        session_id: str,
        *,
        active_release_digest: str | None = None,
    ) -> HarnessSessionManifest:
        session_id = _assert_safe_session_id(session_id)
        with self._writer_lock(session_id):
            manifest = self._recover_unlocked(session_id)
        if active_release_digest is not None:
            self._assert_release(manifest, active_release_digest)
        return manifest

    def session_dir(self, session_id: str) -> Path:
        return _safe_child(self.root.resolve(), _assert_safe_session_id(session_id))

    def _assert_release(self, manifest: HarnessSessionManifest, active_release_digest: str) -> None:
        current = _require_digest(active_release_digest, "active_release_digest")
        if manifest.runtime_kind != HARNESS_RUNTIME_KIND:
            raise HarnessSessionReleaseMismatchError(
                f"harness session {manifest.session_id!r} has runtime kind {manifest.runtime_kind!r}"
            )
        if current != manifest.active_release_digest:
            raise HarnessSessionReleaseMismatchError(
                f"harness session {manifest.session_id!r} is pinned to immutable release "
                f"{manifest.active_release_digest}, but the active release is {current}; "
                "start a new session instead of migrating this one"
            )

    def _allocate_session_id(self, active_release_digest: str) -> str:
        self.root.mkdir(parents=True, exist_ok=True)
        for _ in range(100):
            suffix = evidence_digest(
                {
                    "release": active_release_digest,
                    "nonce": secrets.token_hex(16),
                    "time": _now_ms(),
                }
            )[:16]
            session_id = f"hsess.{suffix}"
            if not self.session_dir(session_id).exists():
                return session_id
        raise HarnessSessionValidationError("unable to allocate a unique harness session id")

    @contextmanager
    def _writer_lock(self, session_id: str) -> Iterator[None]:
        session_dir = self.session_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        lock_path = session_dir / ".writer.lock"
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise HarnessSessionConcurrencyError(
                f"harness session {session_id!r} already has an active writer"
            ) from exc
        try:
            os.write(descriptor, str(os.getpid()).encode("ascii"))
            os.fsync(descriptor)
            yield
        finally:
            os.close(descriptor)
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass

    def _manifest_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "session.json"

    def _message_path(self, message: HarnessSessionMessage) -> Path:
        return self.session_dir(message.session_id) / "messages" / f"{message.sequence:06d}.{message.message_id}.json"

    def _prepare_path(self, prepare: HarnessSessionPrepare) -> Path:
        return self.session_dir(prepare.session_id) / "wal" / f"{prepare.prepare_id}.json"

    def _commit_path_for_message(self, message: HarnessSessionMessage) -> Path:
        return self.session_dir(message.session_id) / "wal" / f"{self._commit_id(message)}.json"

    @staticmethod
    def _prepare_id(message: HarnessSessionMessage) -> str:
        return f"prepare.{message.sequence:06d}.{message.message_id}"

    @staticmethod
    def _commit_id(message: HarnessSessionMessage) -> str:
        return f"commit.{message.sequence:06d}.{message.message_id}"

    def _message_id(
        self,
        manifest: HarnessSessionManifest,
        message_summary: str,
        carryover: tuple[HarnessCarryoverRef, ...],
    ) -> str:
        digest = evidence_digest(
            {
                "session": manifest.session_id,
                "release": manifest.active_release_digest,
                "base_version": manifest.version,
                "sequence": manifest.next_sequence,
                "message_summary": message_summary,
                "carryover": [entry.carryover_digest for entry in carryover],
                "nonce": secrets.token_hex(8),
            }
        )
        return f"hmsg.{digest[:20]}"

    def _normalize_carryover(
        self,
        entries: Sequence[HarnessCarryoverRef | Mapping[str, Any]],
        limits: HarnessSessionLimits,
    ) -> tuple[HarnessCarryoverRef, ...]:
        if len(entries) > limits.max_entries:
            raise HarnessSessionValidationError("carryover exceeds the configured entry limit")
        normalized: list[HarnessCarryoverRef] = []
        seen_refs: set[str] = set()
        for raw in entries:
            try:
                entry = raw if isinstance(raw, HarnessCarryoverRef) else HarnessCarryoverRef.model_validate(raw)
            except ValueError as exc:
                raise HarnessSessionValidationError(str(exc)) from exc
            if entry.artifact_ref in seen_refs:
                raise HarnessSessionValidationError(f"duplicate carryover artifact_ref {entry.artifact_ref!r}")
            seen_refs.add(entry.artifact_ref)
            for field_name, text in (
                ("artifact_ref", entry.artifact_ref),
                ("summary", entry.summary),
            ):
                _assert_public_safe_text(
                    text,
                    field_name=field_name,
                )
            if len(entry.summary.encode("utf-8")) > limits.max_summary_bytes:
                raise HarnessSessionValidationError("carryover summary exceeds the configured summary byte limit")
            normalized.append(entry)
        total_bytes = len(_canonical_bytes([entry.model_dump(mode="json") for entry in normalized]))
        if total_bytes > limits.max_total_bytes:
            raise HarnessSessionValidationError("carryover exceeds the configured total byte limit")
        return tuple(normalized)

    @staticmethod
    def _next_manifest(
        manifest: HarnessSessionManifest,
        message: HarnessSessionMessage,
    ) -> HarnessSessionManifest:
        return HarnessSessionManifest(
            session_id=manifest.session_id,
            project_dir=manifest.project_dir,
            active_release_digest=manifest.active_release_digest,
            limits=manifest.limits,
            created_at_ms=manifest.created_at_ms,
            version=manifest.version + 1,
            message_count=manifest.message_count + 1,
            next_sequence=manifest.next_sequence + 1,
            last_message_id=message.message_id,
            carryover=message.carryover,
        )

    def _read_manifest(self, session_id: str) -> HarnessSessionManifest:
        path = self._manifest_path(session_id)
        try:
            return HarnessSessionManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError as exc:
            raise HarnessSessionValidationError(f"harness session manifest not found for {session_id!r}") from exc
        except json.JSONDecodeError as exc:
            raise HarnessSessionValidationError(f"harness session manifest is corrupt at {path}") from exc

    def _read_prepare(self, path: Path) -> HarnessSessionPrepare:
        try:
            return HarnessSessionPrepare.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError as exc:
            raise HarnessSessionValidationError(f"harness session prepare record is corrupt at {path}") from exc

    def _read_commit(self, path: Path) -> HarnessSessionCommit:
        try:
            return HarnessSessionCommit.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError as exc:
            raise HarnessSessionValidationError(f"harness session commit record is corrupt at {path}") from exc

    @staticmethod
    def _write_json_once(path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = _canonical_bytes(payload)
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if path.read_bytes() != data:
                raise HarnessSessionValidationError(f"immutable session path already contains different bytes: {path}")
            return
        try:
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise HarnessSessionValidationError(f"failed to write session path: {path}")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _write_json_replace(path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = _canonical_bytes(payload)
        temp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        try:
            with open(temp, "xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    def _write_prepare(self, prepare: HarnessSessionPrepare) -> None:
        self._write_json_once(self._prepare_path(prepare), prepare.model_dump(mode="json", exclude_none=True))

    def _write_message_file(self, message: HarnessSessionMessage) -> None:
        self._write_json_once(self._message_path(message), message.model_dump(mode="json", exclude_none=True))

    def _write_commit(self, commit: HarnessSessionCommit) -> None:
        path = self.session_dir(commit.session_id) / "wal" / f"{commit.commit_id}.json"
        self._write_json_once(path, commit.model_dump(mode="json", exclude_none=True))

    def _write_manifest(self, manifest: HarnessSessionManifest) -> None:
        self._write_json_replace(self._manifest_path(manifest.session_id), manifest.model_dump(mode="json", exclude_none=True))

    def _recover_unlocked(self, session_id: str) -> HarnessSessionManifest:
        manifest = self._read_manifest(session_id)
        wal_dir = self.session_dir(session_id) / "wal"
        if not wal_dir.exists():
            return manifest
        for prepare_path in sorted(wal_dir.glob("prepare.*.json")):
            prepare = self._read_prepare(prepare_path)
            if prepare.session_id != session_id:
                raise HarnessSessionValidationError("prepare record crossed session identity")
            message_path = self._message_path(prepare.message)
            commit_path = self._commit_path_for_message(prepare.message)
            if not commit_path.exists():
                if message_path.exists():
                    recorded = HarnessSessionMessage.model_validate(json.loads(message_path.read_text(encoding="utf-8")))
                    if recorded.message_digest == prepare.message.message_digest:
                        message_path.unlink()
                    else:
                        raise HarnessSessionValidationError("uncommitted message path contains unexpected history")
                prepare_path.unlink()
                continue
            commit = self._read_commit(commit_path)
            if (
                commit.session_id != session_id
                or commit.prepare_digest != prepare.prepare_digest
                or commit.message_digest != prepare.message.message_digest
                or commit.next_manifest_digest != prepare.next_manifest.manifest_digest
            ):
                raise HarnessSessionValidationError("commit record crossed prepare/message/manifest identity")
            if message_path.exists():
                recorded = HarnessSessionMessage.model_validate(json.loads(message_path.read_text(encoding="utf-8")))
                if recorded.message_digest != prepare.message.message_digest:
                    raise HarnessSessionValidationError("committed message path contains unexpected history")
            else:
                self._write_message_file(prepare.message)
            current = self._read_manifest(session_id)
            if current.version == prepare.base_version:
                self._write_manifest(prepare.next_manifest)
                manifest = prepare.next_manifest
            elif current.version > prepare.next_manifest.version or (
                current.version == prepare.next_manifest.version
                and current.last_message_id == prepare.next_manifest.last_message_id
            ):
                manifest = current
            else:
                raise HarnessSessionValidationError("session manifest cannot be recovered without inventing history")
        return self._read_manifest(session_id)


HarnessRuntimeSessionStore = HarnessSessionStore


__all__ = [
    "HARNESS_RUNTIME_KIND",
    "HARNESS_SESSIONS_DIR_NAME",
    "HarnessCarryoverRef",
    "HarnessNextContext",
    "HarnessRuntimeSessionStore",
    "HarnessSessionCommit",
    "HarnessSessionConcurrencyError",
    "HarnessSessionError",
    "HarnessSessionLimits",
    "HarnessSessionManifest",
    "HarnessSessionMessage",
    "HarnessSessionPrepare",
    "HarnessSessionReleaseMismatchError",
    "HarnessSessionStore",
    "HarnessSessionValidationError",
    "HarnessSessionVersionError",
    "harness_carryover_ref_digest",
    "harness_session_commit_digest",
    "harness_session_manifest_digest",
    "harness_session_message_digest",
    "harness_session_prepare_digest",
]
