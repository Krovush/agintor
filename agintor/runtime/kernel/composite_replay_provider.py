from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ...authority.public_tasks import assert_public_payload
from ...contracts.epochs import DeploymentIdentity, TaskEnvelope
from ...contracts.harness import CompositeRunPlan, HarnessPublicSessionContext, public_session_context_digest
from ...contracts.run_evidence import assert_no_resolved_credentials
from ...core.identity import canonical_identity_digest
from .composite_budget import CostStatus, ProviderUsageReport, UsageStatus
from .composite_provider import (
    CredentialReference,
    ProviderCallControl,
    ProviderExecutionProvenance,
    ProviderInvocation,
    ProviderInvocationError,
)
from .composite_runtime import ActorCallRequest, ActorTerminalTurn, ActorToolRequest


COMPOSITE_REPLAY_SCHEMA_VERSION = "repo-repair-composite-replay-v1"
MAX_COMPOSITE_REPLAY_BYTES = 16 * 1024 * 1024

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")


class CompositeReplayModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _require_digest(value: str, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _DIGEST_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return normalized


def _require_identifier(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not _IDENTIFIER_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a nonempty portable identifier")
    return normalized


def _json_payload(value: Any) -> Any:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    return value


def _assert_replay_payload_is_public(
    value: Any,
    *,
    forbidden_markers: Sequence[str | bytes] = (),
) -> None:
    payload = _json_payload(value)
    assert_no_resolved_credentials(payload)
    assert_public_payload(payload, canary_values=tuple(forbidden_markers))


def _request_public_session_context_digest(request: ActorCallRequest) -> str:
    session_reads = tuple(
        read
        for read in request.context.reads
        if read.source_kind == "session" and read.source_ref == "public_carryover"
    )
    if not session_reads:
        return public_session_context_digest(None)
    if len(session_reads) != 1:
        raise CompositeReplayMismatchError(
            "session_context_mismatch",
            "replay requests may carry one declared public session context read",
        )
    value = session_reads[0].value
    if not isinstance(value, Mapping):
        raise CompositeReplayMismatchError(
            "session_context_mismatch",
            "public session context read is not a stable JSON object",
        )
    digest = str(value.get("context_digest") or "").strip().lower()
    _require_digest(digest, "public_session_context_digest")
    return digest


class CompositeReplayBinding(CompositeReplayModel):
    """Every immutable identity needed to authorize one replay transcript."""

    release_digest: str
    epoch_id: str
    epoch_manifest_digest: str
    deployment: DeploymentIdentity
    source_protocol_digest: str
    task_envelope_digest: str
    compiled_semantic_digest: str
    public_session_context_digest: str

    @field_validator(
        "release_digest",
        "epoch_manifest_digest",
        "source_protocol_digest",
        "task_envelope_digest",
        "compiled_semantic_digest",
        "public_session_context_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @field_validator("epoch_id")
    @classmethod
    def validate_epoch_id(cls, value: str) -> str:
        return _require_identifier(value, "epoch_id")

    @classmethod
    def from_runtime_inputs(
        cls,
        *,
        release_digest: str,
        task: TaskEnvelope,
        deployment: DeploymentIdentity,
        plan: CompositeRunPlan,
        public_session_context: HarnessPublicSessionContext | Mapping[str, Any] | None = None,
    ) -> "CompositeReplayBinding":
        normalized_task = TaskEnvelope.model_validate(_json_payload(task))
        normalized_plan = CompositeRunPlan.model_validate(_json_payload(plan))
        normalized_deployment = DeploymentIdentity.model_validate(_json_payload(deployment))
        if normalized_plan.task_envelope_digest != normalized_task.task_manifest_digest:
            raise ValueError("replay plan is bound to another task envelope")
        return cls(
            release_digest=release_digest,
            epoch_id=normalized_task.epoch_id,
            epoch_manifest_digest=normalized_task.epoch_manifest_digest,
            deployment=normalized_deployment,
            source_protocol_digest=normalized_plan.source_protocol_digest,
            task_envelope_digest=normalized_task.task_manifest_digest,
            compiled_semantic_digest=normalized_plan.compiled_semantic_digest,
            public_session_context_digest=public_session_context_digest(public_session_context),
        )


CompositeReplayTurn = Annotated[
    ActorToolRequest | ActorTerminalTurn,
    Field(discriminator="turn_kind"),
]


class CompositeReplayRow(CompositeReplayModel):
    """One ordered, single-use request/response/accounting exchange."""

    sequence_no: int = Field(ge=0)
    request_digest: str
    turn: CompositeReplayTurn
    usage: ProviderUsageReport
    row_digest: str = ""

    @field_validator("request_digest")
    @classmethod
    def validate_request_digest(cls, value: str) -> str:
        return _require_digest(value, "request_digest")

    @model_validator(mode="after")
    def bind_row(self) -> "CompositeReplayRow":
        if self.usage.usage_status is UsageStatus.UNKNOWN:
            raise ValueError("replay rows require exact known or estimated token usage")
        if self.usage.cost_status is CostStatus.UNKNOWN:
            raise ValueError("replay rows require exact known or estimated cost")
        response_id = str(self.usage.response_id or "").strip()
        if not response_id or any(character.isspace() for character in response_id):
            raise ValueError("replay usage requires a nonempty whitespace-free response_id")
        if len(response_id) > 256:
            raise ValueError("replay response_id may not exceed 256 characters")
        # JSON-mode values keep the transcript identity invariant when the same
        # contracts are loaded from the source package or the bundled runtime
        # namespace (notably for usage-status enums).
        payload = self.model_dump(mode="json", exclude={"row_digest"})
        computed = canonical_identity_digest(payload, domain="composite-replay-row-v1")
        if self.row_digest and self.row_digest != computed:
            raise ValueError("composite replay row digest mismatch")
        if not self.row_digest:
            object.__setattr__(self, "row_digest", computed)
        _assert_replay_payload_is_public(self.model_dump(mode="json"))
        return self


class CompositeReplayManifest(CompositeReplayModel):
    """Content-addressed transcript; consumption state is deliberately external."""

    schema_version: str = COMPOSITE_REPLAY_SCHEMA_VERSION
    binding: CompositeReplayBinding
    rows: tuple[CompositeReplayRow, ...] = Field(min_length=1)
    manifest_digest: str = ""

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: str) -> str:
        if value != COMPOSITE_REPLAY_SCHEMA_VERSION:
            raise ValueError(f"unsupported composite replay schema version {value!r}")
        return value

    @model_validator(mode="after")
    def bind_manifest(self) -> "CompositeReplayManifest":
        sequence_numbers = tuple(row.sequence_no for row in self.rows)
        if sequence_numbers != tuple(range(len(self.rows))):
            raise ValueError("composite replay rows must have contiguous ordered sequence numbers")
        request_digests = tuple(row.request_digest for row in self.rows)
        if len(request_digests) != len(set(request_digests)):
            raise ValueError("composite replay request digests must be unique")
        response_ids = tuple(str(row.usage.response_id) for row in self.rows)
        if len(response_ids) != len(set(response_ids)):
            raise ValueError("composite replay response_id values must be unique")
        payload = self.model_dump(mode="json", exclude={"manifest_digest"})
        computed = canonical_identity_digest(payload, domain="composite-replay-manifest-v1")
        if self.manifest_digest and self.manifest_digest != computed:
            raise ValueError("composite replay manifest digest mismatch")
        if not self.manifest_digest:
            object.__setattr__(self, "manifest_digest", computed)
        _assert_replay_payload_is_public(self.model_dump(mode="json"))
        return self


class CompositeReplayReconciliation(CompositeReplayModel):
    manifest_digest: str
    row_count: int = Field(ge=0)
    consumed_count: int = Field(ge=0)
    consumed_response_ids: tuple[str, ...]
    remaining_request_digests: tuple[str, ...]
    complete: bool
    reconciliation_digest: str = ""

    @field_validator("manifest_digest")
    @classmethod
    def validate_manifest_digest(cls, value: str) -> str:
        return _require_digest(value, "manifest_digest")

    @model_validator(mode="after")
    def validate_reconciliation(self) -> "CompositeReplayReconciliation":
        if self.consumed_count > self.row_count:
            raise ValueError("replay consumption cannot exceed the manifest row count")
        if len(self.consumed_response_ids) != self.consumed_count:
            raise ValueError("consumed response IDs do not reconcile with consumed_count")
        if len(self.remaining_request_digests) != self.row_count - self.consumed_count:
            raise ValueError("remaining request digests do not reconcile with row counts")
        expected_complete = self.consumed_count == self.row_count
        if self.complete is not expected_complete:
            raise ValueError("replay complete flag does not reconcile with row counts")
        payload = self.model_dump(mode="json", exclude={"reconciliation_digest"})
        computed = canonical_identity_digest(payload, domain="composite-replay-reconciliation-v1")
        if self.reconciliation_digest and self.reconciliation_digest != computed:
            raise ValueError("composite replay reconciliation digest mismatch")
        if not self.reconciliation_digest:
            object.__setattr__(self, "reconciliation_digest", computed)
        return self


class CompositeReplayRecordingSink(Protocol):
    """Narrow sink a future live adapter can populate after one completed call."""

    def record(
        self,
        *,
        request: ActorCallRequest,
        turn: ActorToolRequest | ActorTerminalTurn,
        usage: ProviderUsageReport,
    ) -> CompositeReplayRow:
        ...


class CompositeReplayRecorder:
    """Thread-safe in-memory implementation of the provider recording boundary."""

    def __init__(self, binding: CompositeReplayBinding) -> None:
        self.binding = CompositeReplayBinding.model_validate(_json_payload(binding))
        self._rows: list[CompositeReplayRow] = []
        self._lock = threading.RLock()
        self._finalized = False

    def record(
        self,
        *,
        request: ActorCallRequest,
        turn: ActorToolRequest | ActorTerminalTurn,
        usage: ProviderUsageReport,
    ) -> CompositeReplayRow:
        normalized_request = ActorCallRequest.model_validate(_json_payload(request))
        _assert_replay_payload_is_public(normalized_request)
        if normalized_request.compiled_semantic_digest != self.binding.compiled_semantic_digest:
            raise CompositeReplayMismatchError(
                "identity_mismatch",
                "recorded request is bound to another compiled plan",
            )
        if (
            _request_public_session_context_digest(normalized_request)
            != self.binding.public_session_context_digest
        ):
            raise CompositeReplayMismatchError(
                "session_context_mismatch",
                "recorded request is bound to another public session context",
            )
        normalized_turn = _normalize_turn(turn)
        normalized_usage = ProviderUsageReport.model_validate(_json_payload(usage))
        with self._lock:
            if self._finalized:
                raise CompositeReplayMismatchError(
                    "recorder_finalized",
                    "composite replay recorder is already finalized",
                )
            row = CompositeReplayRow(
                sequence_no=len(self._rows),
                request_digest=normalized_request.request_digest,
                turn=normalized_turn,
                usage=normalized_usage,
            )
            if any(existing.request_digest == row.request_digest for existing in self._rows):
                raise CompositeReplayMismatchError(
                    "row_reuse",
                    "a request digest may be recorded exactly once",
                    sequence_no=row.sequence_no,
                )
            if any(existing.usage.response_id == row.usage.response_id for existing in self._rows):
                raise CompositeReplayMismatchError(
                    "response_reuse",
                    "a provider response_id may be recorded exactly once",
                    sequence_no=row.sequence_no,
                )
            self._rows.append(row)
            return row

    def record_invocation(
        self,
        *,
        request: ActorCallRequest,
        invocation: ProviderInvocation,
    ) -> CompositeReplayRow:
        normalized = ProviderInvocation.model_validate(_json_payload(invocation))
        return self.record(
            request=request,
            turn=_normalize_turn(normalized.response),
            usage=normalized.usage,
        )

    def snapshot(self) -> CompositeReplayManifest:
        with self._lock:
            return CompositeReplayManifest(binding=self.binding, rows=tuple(self._rows))

    def finalize(self) -> CompositeReplayManifest:
        with self._lock:
            manifest = CompositeReplayManifest(binding=self.binding, rows=tuple(self._rows))
            self._finalized = True
            return manifest


class CompositeReplayMismatchError(RuntimeError):
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


class CompositeReplayInvocationError(ProviderInvocationError):
    """A deterministic replay refusal that is always known to be pre-send."""

    def __init__(
        self,
        code: str,
        *,
        sequence_no: int | None = None,
        cancelled: bool = False,
        deadline_exceeded: bool = False,
    ) -> None:
        self.code = code
        self.sequence_no = sequence_no
        super().__init__(
            request_sent=False,
            cancelled=cancelled,
            deadline_exceeded=deadline_exceeded,
        )
        self.args = (f"composite replay refused before send: {code}",)


def _normalize_turn(value: Any) -> ActorToolRequest | ActorTerminalTurn:
    payload = _json_payload(value)
    if not isinstance(payload, Mapping):
        raise CompositeReplayMismatchError(
            "invalid_turn",
            "replay turns must be typed tool-request or terminal mappings",
        )
    kind = payload.get("turn_kind")
    if kind == "tool_request":
        return ActorToolRequest.model_validate(payload)
    if kind == "terminal":
        return ActorTerminalTurn.model_validate(payload)
    raise CompositeReplayMismatchError(
        "invalid_turn",
        "replay turns must be typed ActorToolRequest or ActorTerminalTurn values",
    )


def _normalize_manifest(value: CompositeReplayManifest | Mapping[str, Any]) -> CompositeReplayManifest:
    return CompositeReplayManifest.model_validate(_json_payload(value))


def _normalize_binding(value: CompositeReplayBinding | Mapping[str, Any]) -> CompositeReplayBinding:
    return CompositeReplayBinding.model_validate(_json_payload(value))


class CompositeReplayProvider:
    """Controlled provider that consumes an authorized transcript exactly once."""

    execution_provenance = ProviderExecutionProvenance(
        execution_mode="deterministic_replay",
        live_inference_status="not_run",
        real_inference_requests_sent=0,
    )

    def __init__(
        self,
        manifest: CompositeReplayManifest | Mapping[str, Any],
        *,
        expected_binding: CompositeReplayBinding | Mapping[str, Any],
    ) -> None:
        self.manifest = _normalize_manifest(manifest)
        expected = _normalize_binding(expected_binding)
        if self.manifest.binding != expected:
            raise CompositeReplayMismatchError(
                "identity_mismatch",
                "composite replay manifest does not match the expected runtime identity binding",
            )
        self.binding = expected
        # Harness execution binds every provider to the immutable deployment
        # declared by the active release. Replay carries that identity in its
        # manifest and never derives it from ambient provider configuration.
        self.deployment_identity = expected.deployment
        self._cursor = 0
        self._consumed_request_digests: set[str] = set()
        self._lock = threading.RLock()

    @staticmethod
    def _check_control(control: ProviderCallControl) -> None:
        if control.cancelled:
            raise CompositeReplayInvocationError("cancelled", cancelled=True)
        if control.remaining_ms() <= 0 or time.monotonic() >= control.deadline_monotonic:
            raise CompositeReplayInvocationError(
                "deadline_exceeded",
                deadline_exceeded=True,
            )

    def invoke(
        self,
        request: Any,
        *,
        control: ProviderCallControl,
        credential_reference: CredentialReference | None,
    ) -> ProviderInvocation:
        self._check_control(control)
        if credential_reference is not None:
            raise CompositeReplayInvocationError("credential_not_allowed")
        try:
            normalized_request = ActorCallRequest.model_validate(_json_payload(request))
        except Exception as exc:
            raise CompositeReplayInvocationError("invalid_request") from exc
        try:
            _assert_replay_payload_is_public(normalized_request)
        except Exception as exc:
            raise CompositeReplayInvocationError("non_public_request") from exc
        if normalized_request.compiled_semantic_digest != self.binding.compiled_semantic_digest:
            raise CompositeReplayInvocationError("identity_mismatch")
        try:
            if (
                _request_public_session_context_digest(normalized_request)
                != self.binding.public_session_context_digest
            ):
                raise CompositeReplayInvocationError("identity_mismatch")
        except CompositeReplayMismatchError as exc:
            raise CompositeReplayInvocationError("identity_mismatch") from exc

        with self._lock:
            self._check_control(control)
            if normalized_request.request_digest in self._consumed_request_digests:
                raise CompositeReplayInvocationError(
                    "row_reuse",
                    sequence_no=self._cursor,
                )
            if self._cursor >= len(self.manifest.rows):
                raise CompositeReplayInvocationError(
                    "missing_row",
                    sequence_no=self._cursor,
                )
            row = self.manifest.rows[self._cursor]
            if row.request_digest != normalized_request.request_digest:
                raise CompositeReplayInvocationError(
                    "request_mismatch",
                    sequence_no=row.sequence_no,
                )
            self._check_control(control)
            self._cursor += 1
            self._consumed_request_digests.add(row.request_digest)
            return ProviderInvocation(response=row.turn, usage=row.usage)

    def reconciliation(self) -> CompositeReplayReconciliation:
        with self._lock:
            consumed = self.manifest.rows[: self._cursor]
            remaining = self.manifest.rows[self._cursor :]
            return CompositeReplayReconciliation(
                manifest_digest=self.manifest.manifest_digest,
                row_count=len(self.manifest.rows),
                consumed_count=self._cursor,
                consumed_response_ids=tuple(str(row.usage.response_id) for row in consumed),
                remaining_request_digests=tuple(row.request_digest for row in remaining),
                complete=self._cursor == len(self.manifest.rows),
            )

    def assert_reconciled(self) -> CompositeReplayReconciliation:
        reconciliation = self.reconciliation()
        if not reconciliation.complete:
            raise CompositeReplayMismatchError(
                "extra_rows",
                "composite replay completed with unconsumed manifest rows",
                sequence_no=reconciliation.consumed_count,
            )
        return reconciliation


def _json_object_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"composite replay JSON contains duplicate key {key!r}")
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


def _canonical_manifest_bytes(manifest: CompositeReplayManifest) -> bytes:
    return (
        json.dumps(
            manifest.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def load_composite_replay_manifest(
    path: str | Path,
    *,
    forbidden_markers: Sequence[str | bytes] = (),
    max_bytes: int = MAX_COMPOSITE_REPLAY_BYTES,
) -> CompositeReplayManifest:
    manifest_path = Path(path)
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if manifest_path.is_symlink():
        raise ValueError("composite replay manifest may not be a symbolic link")
    if not manifest_path.is_file():
        raise FileNotFoundError(f"composite replay manifest is missing: {manifest_path}")
    size = manifest_path.stat().st_size
    if size > max_bytes:
        raise ValueError("composite replay manifest exceeds the configured byte limit")
    raw = manifest_path.read_bytes()
    if len(raw) > max_bytes:
        raise ValueError("composite replay manifest exceeds the configured byte limit")
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_json_object_no_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("composite replay manifest is not valid UTF-8 JSON") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("composite replay manifest root must be a JSON object")
    _assert_replay_payload_is_public(payload, forbidden_markers=forbidden_markers)
    return CompositeReplayManifest.model_validate(payload)


def write_composite_replay_manifest(
    path: str | Path,
    manifest: CompositeReplayManifest | Mapping[str, Any],
    *,
    forbidden_markers: Sequence[str | bytes] = (),
) -> Path:
    """Atomically create an immutable replay file, allowing only idempotent rewrites."""

    normalized = _normalize_manifest(manifest)
    _assert_replay_payload_is_public(normalized, forbidden_markers=forbidden_markers)
    payload = _canonical_manifest_bytes(normalized)
    if len(payload) > MAX_COMPOSITE_REPLAY_BYTES:
        raise ValueError("composite replay manifest exceeds the configured byte limit")

    manifest_path = Path(path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    if manifest_path.is_symlink():
        raise ValueError("composite replay manifest may not be a symbolic link")
    temp_path = manifest_path.parent / f".{manifest_path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temp_path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp_path, manifest_path)
        except FileExistsError:
            existing = load_composite_replay_manifest(
                manifest_path,
                forbidden_markers=forbidden_markers,
            )
            if existing.model_dump(mode="json") != normalized.model_dump(mode="json"):
                raise FileExistsError(
                    f"immutable composite replay manifest already exists: {manifest_path}"
                )
        _fsync_directory(manifest_path.parent)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return manifest_path


__all__ = [
    "COMPOSITE_REPLAY_SCHEMA_VERSION",
    "MAX_COMPOSITE_REPLAY_BYTES",
    "CompositeReplayBinding",
    "CompositeReplayInvocationError",
    "CompositeReplayManifest",
    "CompositeReplayMismatchError",
    "CompositeReplayProvider",
    "CompositeReplayRecorder",
    "CompositeReplayRecordingSink",
    "CompositeReplayReconciliation",
    "CompositeReplayRow",
    "CompositeReplayTurn",
    "load_composite_replay_manifest",
    "write_composite_replay_manifest",
]
