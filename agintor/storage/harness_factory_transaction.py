from __future__ import annotations

import json
import os
import re
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..core.identity import evidence_digest
from ..core.versioning import RUNTIME_CONTRACT_VERSION
from ..factory.harness_release import (
    ACTIVE_RELEASE_FILE,
    advance_active_release,
    load_active_release_pointer,
    materialize_harness_release,
)
from ..factory.harness_release_contracts import (
    ActiveReleasePointer,
    HarnessReleaseRequest,
    MaterializedHarnessRelease,
)


HARNESS_FACTORY_CHAT_SCHEMA_VERSION = "harness-factory-chat-v1"
HARNESS_FACTORY_MESSAGE_SCHEMA_VERSION = "harness-factory-message-v1"
HARNESS_FACTORY_PREPARE_SCHEMA_VERSION = "harness-factory-transaction-prepare-v1"
HARNESS_FACTORY_COMMIT_INTENT_SCHEMA_VERSION = "harness-factory-transaction-commit-intent-v1"
HARNESS_FACTORY_COMMITTED_SCHEMA_VERSION = "harness-factory-transaction-committed-v1"
HARNESS_FACTORY_RUNTIME_KIND = "harness"
HARNESS_FACTORY_CHAT_DIR_NAME = ".factory_chat"

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SECRET_VALUE_PATTERNS = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
_FORBIDDEN_PROMPT_FRAGMENTS = (
    "api_key",
    "authorization",
    "bearer ",
    "credential",
    "evaluator",
    "gold_patch",
    "hidden",
    "password",
    "private_key",
    "raw_context",
    "sealed",
    "token",
)


class HarnessFactoryTransactionError(RuntimeError):
    """Base class for V1 harness factory transaction failures."""


class HarnessFactoryConcurrencyError(HarnessFactoryTransactionError):
    """Raised when another factory transaction owns the project writer lock."""


class HarnessFactoryStaleHeadError(HarnessFactoryTransactionError):
    """Raised when pointer or chat head does not match the requested parent."""


class HarnessFactoryValidationError(HarnessFactoryTransactionError):
    """Raised for invalid factory chat or transaction inputs."""


class HarnessFactoryInjectedFailure(RuntimeError):
    """Test hook exception raised at a named transaction boundary."""


class HarnessFactoryModel(BaseModel):
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


def _require_identifier(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if (
        not _IDENTIFIER_RE.fullmatch(normalized)
        or normalized.startswith(".")
        or "/" in normalized
        or "\\" in normalized
        or ".." in normalized
    ):
        raise ValueError(f"{field_name} must be a portable non-traversing identifier")
    return normalized


def _assert_project_child(root: Path, *parts: str) -> Path:
    child = root.joinpath(*parts).resolve()
    try:
        child.relative_to(root)
    except ValueError as exc:
        raise HarnessFactoryValidationError("factory transaction path escapes the project") from exc
    return child


def _assert_public_prompt(
    prompt: str,
) -> str:
    normalized = str(prompt or "").strip()
    if not normalized:
        raise HarnessFactoryValidationError("factory prompt may not be empty")
    lowered = normalized.casefold()
    compact = re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")
    if any(pattern.search(normalized) for pattern in _SECRET_VALUE_PATTERNS):
        raise HarnessFactoryValidationError("factory prompt contains resolved credential material")
    for fragment in _FORBIDDEN_PROMPT_FRAGMENTS:
        if fragment in compact or fragment in lowered:
            raise HarnessFactoryValidationError(f"factory prompt references non-public state: {fragment}")
    return normalized


def _sorted_unique_digests(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    normalized = tuple(_require_digest(value, field_name) for value in values)
    if normalized != tuple(sorted(set(normalized))):
        raise ValueError(f"{field_name} must be unique and sorted")
    return normalized


class HarnessFactoryChatManifest(HarnessFactoryModel):
    schema_version: Literal[HARNESS_FACTORY_CHAT_SCHEMA_VERSION] = HARNESS_FACTORY_CHAT_SCHEMA_VERSION
    runtime_contract_version: str = RUNTIME_CONTRACT_VERSION
    chat_id: str
    project_root: str
    runtime_kind: Literal["harness"] = HARNESS_FACTORY_RUNTIME_KIND
    epoch_manifest_digest: str
    active_release_digest: str
    active_manifest_digest: str
    active_protocol_digest: str
    created_at_ms: int = Field(ge=0)
    message_count: int = Field(default=0, ge=0)
    last_message_id: str | None = None
    chat_digest: str = ""

    @field_validator("runtime_contract_version")
    @classmethod
    def validate_contract_version(cls, value: str) -> str:
        if str(value) != RUNTIME_CONTRACT_VERSION:
            raise ValueError("harness factory chat runtime contract version mismatch")
        return str(value)

    @field_validator("chat_id")
    @classmethod
    def validate_chat_id(cls, value: str) -> str:
        return _require_identifier(value, "chat_id")

    @field_validator("epoch_manifest_digest", "active_release_digest", "active_manifest_digest", "active_protocol_digest")
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @field_validator("project_root")
    @classmethod
    def validate_project_root(cls, value: str) -> str:
        if not str(value or "").strip():
            raise ValueError("project_root may not be empty")
        return str(value)

    @field_validator("last_message_id")
    @classmethod
    def validate_optional_message_id(cls, value: str | None) -> str | None:
        return None if value is None else _require_identifier(value, "last_message_id")

    @model_validator(mode="after")
    def bind_digest(self) -> "HarnessFactoryChatManifest":
        if self.message_count == 0 and self.last_message_id is not None:
            raise ValueError("empty factory chat cannot have a last message")
        if self.message_count > 0 and self.last_message_id is None:
            raise ValueError("nonempty factory chat requires a last message")
        computed = harness_factory_chat_digest(self)
        if self.chat_digest and self.chat_digest != computed:
            raise ValueError("factory chat digest mismatch")
        if not self.chat_digest:
            object.__setattr__(self, "chat_digest", computed)
        return self


class HarnessFactoryMessage(HarnessFactoryModel):
    schema_version: Literal[HARNESS_FACTORY_MESSAGE_SCHEMA_VERSION] = HARNESS_FACTORY_MESSAGE_SCHEMA_VERSION
    runtime_contract_version: str = RUNTIME_CONTRACT_VERSION
    chat_id: str
    message_id: str
    message_index: int = Field(ge=0)
    parent_message_id: str | None = None
    prior_active_release_digest: str | None = None
    new_release_digest: str
    new_manifest_digest: str
    new_protocol_digest: str
    compiled_semantic_digest: str
    dependency_manifest_digest: str
    epoch_manifest_digest: str
    prompt_text: str
    prompt_digest: str = ""
    search_result_digest: str
    selection_evidence_digests: tuple[str, ...] = Field(min_length=1)
    transaction_id: str
    transaction_digest: str = ""
    created_at_ms: int = Field(ge=0)
    message_digest: str = ""

    @field_validator("runtime_contract_version")
    @classmethod
    def validate_contract_version(cls, value: str) -> str:
        if str(value) != RUNTIME_CONTRACT_VERSION:
            raise ValueError("harness factory message runtime contract version mismatch")
        return str(value)

    @field_validator("chat_id", "message_id", "transaction_id")
    @classmethod
    def validate_identifier(cls, value: str, info: Any) -> str:
        return _require_identifier(value, info.field_name)

    @field_validator("parent_message_id")
    @classmethod
    def validate_parent_id(cls, value: str | None) -> str | None:
        return None if value is None else _require_identifier(value, "parent_message_id")

    @field_validator(
        "new_release_digest",
        "new_manifest_digest",
        "new_protocol_digest",
        "compiled_semantic_digest",
        "dependency_manifest_digest",
        "epoch_manifest_digest",
        "search_result_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @field_validator("prior_active_release_digest")
    @classmethod
    def validate_optional_digest(cls, value: str | None) -> str | None:
        return None if value is None else _require_digest(value, "prior_active_release_digest")

    @field_validator("selection_evidence_digests")
    @classmethod
    def validate_selection_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique_digests(value, "selection_evidence_digests")

    @field_validator("prompt_text")
    @classmethod
    def validate_prompt_text(cls, value: str) -> str:
        if not str(value or "").strip():
            raise ValueError("prompt_text may not be empty")
        return str(value)

    @model_validator(mode="after")
    def bind_message(self) -> "HarnessFactoryMessage":
        if self.message_index == 0:
            if self.parent_message_id is not None or self.prior_active_release_digest is not None:
                raise ValueError("initial factory message may not name a prior release or parent")
        elif self.parent_message_id is None or self.prior_active_release_digest is None:
            raise ValueError("factory follow-up must name its parent message and prior active release")
        prompt_digest = evidence_digest({"kind": "harness-factory-prompt-v1", "text": self.prompt_text})
        if self.prompt_digest and self.prompt_digest != prompt_digest:
            raise ValueError("factory prompt digest mismatch")
        if not self.prompt_digest:
            object.__setattr__(self, "prompt_digest", prompt_digest)
        transaction_digest = evidence_digest(
            {
                "kind": "harness-factory-transaction-identity-v1",
                "chat_id": self.chat_id,
                "message_id": self.message_id,
                "message_index": self.message_index,
                "prior_active_release_digest": self.prior_active_release_digest,
                "new_release_digest": self.new_release_digest,
                "new_manifest_digest": self.new_manifest_digest,
            }
        )
        if self.transaction_digest and self.transaction_digest != transaction_digest:
            raise ValueError("factory transaction digest mismatch")
        if not self.transaction_digest:
            object.__setattr__(self, "transaction_digest", transaction_digest)
        computed = harness_factory_message_digest(self)
        if self.message_digest and self.message_digest != computed:
            raise ValueError("factory message digest mismatch")
        if not self.message_digest:
            object.__setattr__(self, "message_digest", computed)
        return self


class HarnessFactoryPrepare(HarnessFactoryModel):
    schema_version: Literal[HARNESS_FACTORY_PREPARE_SCHEMA_VERSION] = HARNESS_FACTORY_PREPARE_SCHEMA_VERSION
    state: Literal["prepared"] = "prepared"
    transaction_id: str
    chat_id: str
    prior_chat_digest: str | None = None
    prior_active_release_digest: str | None = None
    materialized: MaterializedHarnessRelease
    message: HarnessFactoryMessage
    next_chat: HarnessFactoryChatManifest
    prepare_digest: str = ""

    @field_validator("transaction_id", "chat_id")
    @classmethod
    def validate_identifier(cls, value: str, info: Any) -> str:
        return _require_identifier(value, info.field_name)

    @field_validator("prior_chat_digest", "prior_active_release_digest")
    @classmethod
    def validate_optional_digest(cls, value: str | None) -> str | None:
        return None if value is None else _require_digest(value, "digest")

    @model_validator(mode="after")
    def bind_prepare(self) -> "HarnessFactoryPrepare":
        if self.message.transaction_id != self.transaction_id:
            raise ValueError("prepared message crossed transaction identity")
        if self.message.chat_id != self.chat_id or self.next_chat.chat_id != self.chat_id:
            raise ValueError("prepared chat/message crossed chat identity")
        if self.materialized.manifest.release_digest != self.message.new_release_digest:
            raise ValueError("prepared message names a different release than materialized")
        if self.materialized.manifest.manifest_digest != self.message.new_manifest_digest:
            raise ValueError("prepared message names a different release manifest than materialized")
        if self.materialized.manifest.protocol_source_digest != self.message.new_protocol_digest:
            raise ValueError("prepared message names a different protocol than materialized")
        if self.next_chat.active_release_digest != self.message.new_release_digest:
            raise ValueError("next chat manifest must advance to the message release")
        if self.next_chat.last_message_id != self.message.message_id:
            raise ValueError("next chat manifest must point to the prepared message")
        computed = harness_factory_prepare_digest(self)
        if self.prepare_digest and self.prepare_digest != computed:
            raise ValueError("factory prepare digest mismatch")
        if not self.prepare_digest:
            object.__setattr__(self, "prepare_digest", computed)
        return self


class HarnessFactoryCommitIntent(HarnessFactoryModel):
    schema_version: Literal[HARNESS_FACTORY_COMMIT_INTENT_SCHEMA_VERSION] = HARNESS_FACTORY_COMMIT_INTENT_SCHEMA_VERSION
    state: Literal["commit_intent"] = "commit_intent"
    transaction_id: str
    chat_id: str
    prepare_digest: str
    message_digest: str
    new_release_digest: str
    new_manifest_digest: str
    intent_digest: str = ""

    @field_validator("transaction_id", "chat_id")
    @classmethod
    def validate_identifier(cls, value: str, info: Any) -> str:
        return _require_identifier(value, info.field_name)

    @field_validator("prepare_digest", "message_digest", "new_release_digest", "new_manifest_digest")
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @model_validator(mode="after")
    def bind_intent(self) -> "HarnessFactoryCommitIntent":
        computed = harness_factory_commit_intent_digest(self)
        if self.intent_digest and self.intent_digest != computed:
            raise ValueError("factory commit-intent digest mismatch")
        if not self.intent_digest:
            object.__setattr__(self, "intent_digest", computed)
        return self


class HarnessFactoryCommitted(HarnessFactoryModel):
    schema_version: Literal[HARNESS_FACTORY_COMMITTED_SCHEMA_VERSION] = HARNESS_FACTORY_COMMITTED_SCHEMA_VERSION
    state: Literal["committed"] = "committed"
    transaction_id: str
    chat_id: str
    intent_digest: str
    active_pointer: ActiveReleasePointer
    message_digest: str
    chat_digest: str
    committed_at_ms: int = Field(ge=0)
    committed_digest: str = ""

    @field_validator("transaction_id", "chat_id")
    @classmethod
    def validate_identifier(cls, value: str, info: Any) -> str:
        return _require_identifier(value, info.field_name)

    @field_validator("intent_digest", "message_digest", "chat_digest")
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @model_validator(mode="after")
    def bind_committed(self) -> "HarnessFactoryCommitted":
        computed = harness_factory_committed_digest(self)
        if self.committed_digest and self.committed_digest != computed:
            raise ValueError("factory committed marker digest mismatch")
        if not self.committed_digest:
            object.__setattr__(self, "committed_digest", computed)
        return self


def harness_factory_chat_digest(chat: HarnessFactoryChatManifest | Mapping[str, Any]) -> str:
    payload = chat.model_dump(mode="python", exclude_none=True) if isinstance(chat, HarnessFactoryChatManifest) else dict(chat)
    payload.pop("chat_digest", None)
    return evidence_digest({"kind": HARNESS_FACTORY_CHAT_SCHEMA_VERSION, "chat": payload})


def harness_factory_message_digest(message: HarnessFactoryMessage | Mapping[str, Any]) -> str:
    payload = message.model_dump(mode="python", exclude_none=True) if isinstance(message, HarnessFactoryMessage) else dict(message)
    payload.pop("message_digest", None)
    return evidence_digest({"kind": HARNESS_FACTORY_MESSAGE_SCHEMA_VERSION, "message": payload})


def harness_factory_prepare_digest(prepare: HarnessFactoryPrepare | Mapping[str, Any]) -> str:
    payload = prepare.model_dump(mode="python", exclude_none=True) if isinstance(prepare, HarnessFactoryPrepare) else dict(prepare)
    payload.pop("prepare_digest", None)
    return evidence_digest({"kind": HARNESS_FACTORY_PREPARE_SCHEMA_VERSION, "prepare": payload})


def harness_factory_commit_intent_digest(intent: HarnessFactoryCommitIntent | Mapping[str, Any]) -> str:
    payload = intent.model_dump(mode="python", exclude_none=True) if isinstance(intent, HarnessFactoryCommitIntent) else dict(intent)
    payload.pop("intent_digest", None)
    return evidence_digest({"kind": HARNESS_FACTORY_COMMIT_INTENT_SCHEMA_VERSION, "intent": payload})


def harness_factory_committed_digest(committed: HarnessFactoryCommitted | Mapping[str, Any]) -> str:
    payload = committed.model_dump(mode="python", exclude_none=True) if isinstance(committed, HarnessFactoryCommitted) else dict(committed)
    payload.pop("committed_digest", None)
    return evidence_digest({"kind": HARNESS_FACTORY_COMMITTED_SCHEMA_VERSION, "committed": payload})


class HarnessFactoryTransactionStore:
    def __init__(
        self,
        project_root: str | Path,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.root = self.project_root / HARNESS_FACTORY_CHAT_DIR_NAME
        self.messages_root = self.root / "messages"
        self.wal_root = self.root / "wal"
        self.staging_root = self.root / "message_staging"

    def create_initial_chat(
        self,
        *,
        request: HarnessReleaseRequest,
        user_prompt_text: str,
        chat_id: str | None = None,
        search_result_digest: str,
        selection_evidence_digests: Sequence[str],
        fail_at: str | None = None,
    ) -> HarnessFactoryMessage:
        return self._publish(
            request=request,
            user_prompt_text=user_prompt_text,
            chat_id=chat_id,
            expected_parent_message_id=None,
            expected_message_index=0,
            search_result_digest=search_result_digest,
            selection_evidence_digests=selection_evidence_digests,
            fail_at=fail_at,
        )

    def apply_followup(
        self,
        *,
        request: HarnessReleaseRequest,
        user_prompt_text: str,
        expected_parent_message_id: str,
        expected_message_index: int | None = None,
        search_result_digest: str,
        selection_evidence_digests: Sequence[str],
        fail_at: str | None = None,
    ) -> HarnessFactoryMessage:
        return self._publish(
            request=request,
            user_prompt_text=user_prompt_text,
            chat_id=None,
            expected_parent_message_id=expected_parent_message_id,
            expected_message_index=expected_message_index,
            search_result_digest=search_result_digest,
            selection_evidence_digests=selection_evidence_digests,
            fail_at=fail_at,
        )

    def recover(self) -> HarnessFactoryChatManifest | None:
        if not self.root.exists():
            return None
        with self._writer_lock():
            return self._recover_unlocked()

    def load_chat(self) -> HarnessFactoryChatManifest:
        return HarnessFactoryChatManifest.model_validate(
            json.loads(self._chat_path().read_text(encoding="utf-8"))
        )

    def messages(self) -> list[HarnessFactoryMessage]:
        if not self.messages_root.exists():
            return []
        messages: list[HarnessFactoryMessage] = []
        for message_path in sorted(self.messages_root.glob("*/message.json")):
            messages.append(
                HarnessFactoryMessage.model_validate(
                    json.loads(message_path.read_text(encoding="utf-8"))
                )
            )
        return messages

    def _publish(
        self,
        *,
        request: HarnessReleaseRequest,
        user_prompt_text: str,
        chat_id: str | None,
        expected_parent_message_id: str | None,
        expected_message_index: int | None,
        search_result_digest: str,
        selection_evidence_digests: Sequence[str],
        fail_at: str | None,
    ) -> HarnessFactoryMessage:
        prompt = _assert_public_prompt(user_prompt_text)
        search_digest = _require_digest(search_result_digest, "search_result_digest")
        selection_digests = _sorted_unique_digests(selection_evidence_digests, "selection_evidence_digests")
        self._assert_selection_evidence_matches_request(request, selection_digests)
        materialized = materialize_harness_release(project_root=self.project_root, request=request)
        self._fail(fail_at, "after_materialize")
        with self._writer_lock():
            self._recover_unlocked()
            prior_pointer = load_active_release_pointer(self.project_root)
            prior_chat = self._load_chat_or_none()
            is_initial = prior_chat is None
            if is_initial:
                if prior_pointer is not None:
                    raise HarnessFactoryStaleHeadError("initial harness factory chat requires no active release pointer")
                if expected_parent_message_id is not None:
                    raise HarnessFactoryStaleHeadError("initial harness factory chat cannot name a parent message")
                allocated_chat_id = _require_identifier(chat_id or self._allocate_chat_id(request.epoch.epoch_manifest_digest), "chat_id")
                message_index = 0
                prior_release_digest = None
            else:
                allocated_chat_id = prior_chat.chat_id
                if chat_id is not None and chat_id != allocated_chat_id:
                    raise HarnessFactoryStaleHeadError("follow-up chat_id does not match the existing factory chat")
                if prior_pointer is None:
                    raise HarnessFactoryStaleHeadError("factory follow-up requires an active release pointer")
                if prior_pointer.release_digest != prior_chat.active_release_digest:
                    raise HarnessFactoryStaleHeadError("factory chat head and active release pointer differ")
                if request.epoch.epoch_manifest_digest != prior_chat.epoch_manifest_digest:
                    raise HarnessFactoryStaleHeadError("factory follow-up changed the pinned epoch digest")
                if expected_parent_message_id != prior_chat.last_message_id:
                    raise HarnessFactoryStaleHeadError("factory follow-up parent message is stale")
                message_index = prior_chat.message_count
                prior_release_digest = prior_chat.active_release_digest
            if expected_message_index is not None and expected_message_index != message_index:
                raise HarnessFactoryStaleHeadError(
                    f"factory chat expected message_index {message_index}, got {expected_message_index}"
                )
            message = self._build_message(
                chat_id=allocated_chat_id,
                message_index=message_index,
                parent_message_id=None if prior_chat is None else prior_chat.last_message_id,
                prior_active_release_digest=prior_release_digest,
                materialized=materialized,
                prompt=prompt,
                search_result_digest=search_digest,
                selection_evidence_digests=selection_digests,
            )
            next_chat = self._next_chat(prior_chat, message, materialized)
            prepare = HarnessFactoryPrepare(
                transaction_id=message.transaction_id,
                chat_id=allocated_chat_id,
                prior_chat_digest=None if prior_chat is None else prior_chat.chat_digest,
                prior_active_release_digest=prior_release_digest,
                materialized=materialized,
                message=message,
                next_chat=next_chat,
            )
            self._write_prepare(prepare)
            self._stage_message(prepare)
            self._fail(fail_at, "after_prepare")
            self._fail(fail_at, "after_message_staged")
            intent = HarnessFactoryCommitIntent(
                transaction_id=prepare.transaction_id,
                chat_id=prepare.chat_id,
                prepare_digest=prepare.prepare_digest,
                message_digest=prepare.message.message_digest,
                new_release_digest=prepare.message.new_release_digest,
                new_manifest_digest=prepare.message.new_manifest_digest,
            )
            self._write_commit_intent(intent)
            self._fail(fail_at, "after_commit_intent")
            pointer = self._advance_pointer(prepare)
            self._fail(fail_at, "after_pointer")
            self._publish_message(prepare)
            self._fail(fail_at, "after_message")
            self._write_chat(prepare.next_chat)
            self._fail(fail_at, "after_chat_manifest")
            self._write_committed(prepare, intent, pointer)
            self._fail(fail_at, "after_committed_marker")
            return prepare.message

    def _recover_unlocked(self) -> HarnessFactoryChatManifest | None:
        self._ensure_roots()
        for prepare_path in sorted(self.wal_root.glob("prepare.*.json")):
            prepare = self._read_prepare(prepare_path)
            intent_path = self._intent_path(prepare.transaction_id)
            committed_path = self._committed_path(prepare.transaction_id)
            if not intent_path.exists():
                self._abort_prepare(prepare, prepare_path)
                continue
            intent = self._read_commit_intent(intent_path)
            self._assert_intent_matches_prepare(intent, prepare)
            pointer = self._advance_pointer(prepare)
            self._publish_message(prepare)
            self._write_chat(prepare.next_chat)
            if not committed_path.exists():
                self._write_committed(prepare, intent, pointer)
        return self._load_chat_or_none()

    def _ensure_roots(self) -> None:
        self.messages_root.mkdir(parents=True, exist_ok=True)
        self.wal_root.mkdir(parents=True, exist_ok=True)
        self.staging_root.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _writer_lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / ".transaction.lock"
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise HarnessFactoryConcurrencyError("factory project already has an active transaction writer") from exc
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

    def _chat_path(self) -> Path:
        return _assert_project_child(self.project_root, HARNESS_FACTORY_CHAT_DIR_NAME, "chat.json")

    def _load_chat_or_none(self) -> HarnessFactoryChatManifest | None:
        path = self._chat_path()
        if not path.exists():
            return None
        return HarnessFactoryChatManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def _allocate_chat_id(self, epoch_digest: str) -> str:
        candidate = "hchat." + evidence_digest(
            {
                "kind": "harness-factory-chat-identity-v1",
                "epoch_manifest_digest": epoch_digest,
            }
        )[:20]
        if not _IDENTIFIER_RE.fullmatch(candidate):
            raise HarnessFactoryValidationError(
                "unable to derive a valid harness factory chat identity"
            )
        return candidate

    def _build_message(
        self,
        *,
        chat_id: str,
        message_index: int,
        parent_message_id: str | None,
        prior_active_release_digest: str | None,
        materialized: MaterializedHarnessRelease,
        prompt: str,
        search_result_digest: str,
        selection_evidence_digests: tuple[str, ...],
    ) -> HarnessFactoryMessage:
        manifest = materialized.manifest
        message_seed = evidence_digest(
            {
                "kind": "harness-factory-message-identity-v1",
                "chat_id": chat_id,
                "message_index": message_index,
                "parent_message_id": parent_message_id,
                "prior_active_release_digest": prior_active_release_digest,
                "new_release_digest": manifest.release_digest,
                "new_manifest_digest": manifest.manifest_digest,
                "new_protocol_digest": manifest.protocol_source_digest,
                "compiled_semantic_digest": manifest.compiled_semantic_digest,
                "dependency_manifest_digest": manifest.dependency_manifest_digest,
                "epoch_manifest_digest": manifest.epoch_manifest_digest,
                "prompt_text": prompt,
                "search_result_digest": search_result_digest,
                "selection_evidence_digests": list(selection_evidence_digests),
            }
        )
        transaction_id = f"ftxn.{message_index:04d}.{message_seed[:20]}"
        return HarnessFactoryMessage(
            chat_id=chat_id,
            message_id=f"fmsg.{message_seed[:20]}",
            message_index=message_index,
            parent_message_id=parent_message_id,
            prior_active_release_digest=prior_active_release_digest,
            new_release_digest=manifest.release_digest,
            new_manifest_digest=manifest.manifest_digest,
            new_protocol_digest=manifest.protocol_source_digest,
            compiled_semantic_digest=manifest.compiled_semantic_digest,
            dependency_manifest_digest=manifest.dependency_manifest_digest,
            epoch_manifest_digest=manifest.epoch_manifest_digest,
            prompt_text=prompt,
            search_result_digest=search_result_digest,
            selection_evidence_digests=selection_evidence_digests,
            transaction_id=transaction_id,
            created_at_ms=_now_ms(),
        )

    def _next_chat(
        self,
        prior_chat: HarnessFactoryChatManifest | None,
        message: HarnessFactoryMessage,
        materialized: MaterializedHarnessRelease,
    ) -> HarnessFactoryChatManifest:
        manifest = materialized.manifest
        created_at = prior_chat.created_at_ms if prior_chat is not None else _now_ms()
        return HarnessFactoryChatManifest(
            chat_id=message.chat_id,
            project_root=str(self.project_root),
            epoch_manifest_digest=manifest.epoch_manifest_digest,
            active_release_digest=manifest.release_digest,
            active_manifest_digest=manifest.manifest_digest,
            active_protocol_digest=manifest.protocol_source_digest,
            created_at_ms=created_at,
            message_count=message.message_index + 1,
            last_message_id=message.message_id,
        )

    def _assert_selection_evidence_matches_request(
        self,
        request: HarnessReleaseRequest,
        selection_evidence_digests: tuple[str, ...],
    ) -> None:
        request_digests = tuple(
            sorted(
                {
                    digest
                    for decision in request.selection_decisions
                    for digest in decision.evidence_digests
                }
            )
        )
        if request_digests and request_digests != selection_evidence_digests:
            raise HarnessFactoryValidationError("selection evidence digests crossed release request decisions")

    def _transaction_stem(self, transaction_id: str) -> str:
        return _require_identifier(transaction_id, "transaction_id")

    def _prepare_path(self, transaction_id: str) -> Path:
        return _assert_project_child(self.project_root, HARNESS_FACTORY_CHAT_DIR_NAME, "wal", f"prepare.{self._transaction_stem(transaction_id)}.json")

    def _intent_path(self, transaction_id: str) -> Path:
        return _assert_project_child(self.project_root, HARNESS_FACTORY_CHAT_DIR_NAME, "wal", f"commit_intent.{self._transaction_stem(transaction_id)}.json")

    def _committed_path(self, transaction_id: str) -> Path:
        return _assert_project_child(self.project_root, HARNESS_FACTORY_CHAT_DIR_NAME, "wal", f"committed.{self._transaction_stem(transaction_id)}.json")

    def _message_dir(self, message: HarnessFactoryMessage) -> Path:
        return _assert_project_child(
            self.project_root,
            HARNESS_FACTORY_CHAT_DIR_NAME,
            "messages",
            f"{message.message_index:04d}_{message.message_id}",
        )

    def _staging_dir(self, prepare: HarnessFactoryPrepare) -> Path:
        return _assert_project_child(
            self.project_root,
            HARNESS_FACTORY_CHAT_DIR_NAME,
            "message_staging",
            prepare.transaction_id,
        )

    @staticmethod
    def _write_once(path: Path, payload: Mapping[str, Any] | bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = payload if isinstance(payload, bytes) else _canonical_bytes(payload)
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if path.read_bytes() != data:
                raise HarnessFactoryValidationError(f"transaction path already contains different bytes: {path}")
            return
        try:
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise HarnessFactoryValidationError(f"failed to write transaction path: {path}")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _write_replace(path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        try:
            with open(temp, "xb") as handle:
                handle.write(_canonical_bytes(payload))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    def _write_prepare(self, prepare: HarnessFactoryPrepare) -> None:
        self._write_once(self._prepare_path(prepare.transaction_id), prepare.model_dump(mode="json", exclude_none=True))

    def _stage_message(self, prepare: HarnessFactoryPrepare) -> None:
        staging = self._staging_dir(prepare)
        staging.mkdir(parents=True, exist_ok=True)
        self._write_once(staging / "message.json", prepare.message.model_dump(mode="json", exclude_none=True))
        self._write_once(staging / "prompt.txt", (prepare.message.prompt_text + "\n").encode("utf-8"))

    def _write_commit_intent(self, intent: HarnessFactoryCommitIntent) -> None:
        self._write_once(self._intent_path(intent.transaction_id), intent.model_dump(mode="json", exclude_none=True))

    def _write_committed(
        self,
        prepare: HarnessFactoryPrepare,
        intent: HarnessFactoryCommitIntent,
        pointer: ActiveReleasePointer,
    ) -> None:
        committed = HarnessFactoryCommitted(
            transaction_id=prepare.transaction_id,
            chat_id=prepare.chat_id,
            intent_digest=intent.intent_digest,
            active_pointer=pointer,
            message_digest=prepare.message.message_digest,
            chat_digest=prepare.next_chat.chat_digest,
            committed_at_ms=_now_ms(),
        )
        self._write_once(self._committed_path(prepare.transaction_id), committed.model_dump(mode="json", exclude_none=True))

    def _write_chat(self, chat: HarnessFactoryChatManifest) -> None:
        self._write_replace(self._chat_path(), chat.model_dump(mode="json", exclude_none=True))

    def _advance_pointer(self, prepare: HarnessFactoryPrepare) -> ActiveReleasePointer:
        pointer = advance_active_release(
            project_root=self.project_root,
            materialized=prepare.materialized,
        )
        if pointer.release_digest != prepare.message.new_release_digest:
            raise HarnessFactoryValidationError("active pointer advanced to the wrong release")
        return pointer

    def _publish_message(self, prepare: HarnessFactoryPrepare) -> None:
        destination = self._message_dir(prepare.message)
        staged = self._staging_dir(prepare)
        if destination.exists():
            existing = HarnessFactoryMessage.model_validate(
                json.loads((destination / "message.json").read_text(encoding="utf-8"))
            )
            if existing.message_digest != prepare.message.message_digest:
                raise HarnessFactoryValidationError("visible factory message crossed transaction identity")
            return
        if not (staged / "message.json").exists():
            self._stage_message(prepare)
        temp = destination.with_name(f".{destination.name}.tmp.{secrets.token_hex(8)}")
        temp.mkdir(parents=True, exist_ok=False)
        try:
            self._write_once(temp / "message.json", (staged / "message.json").read_bytes())
            self._write_once(temp / "prompt.txt", (staged / "prompt.txt").read_bytes())
            temp.replace(destination)
        except Exception:
            if temp.exists():
                for child in temp.iterdir():
                    child.unlink()
                temp.rmdir()
            raise

    def _abort_prepare(self, prepare: HarnessFactoryPrepare, prepare_path: Path) -> None:
        staging = self._staging_dir(prepare)
        if staging.exists():
            for child in sorted(staging.iterdir()):
                if child.is_file():
                    child.unlink()
            staging.rmdir()
        prepare_path.unlink()

    def _read_prepare(self, path: Path) -> HarnessFactoryPrepare:
        return HarnessFactoryPrepare.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def _read_commit_intent(self, path: Path) -> HarnessFactoryCommitIntent:
        return HarnessFactoryCommitIntent.model_validate(json.loads(path.read_text(encoding="utf-8")))

    @staticmethod
    def _assert_intent_matches_prepare(intent: HarnessFactoryCommitIntent, prepare: HarnessFactoryPrepare) -> None:
        if (
            intent.transaction_id != prepare.transaction_id
            or intent.chat_id != prepare.chat_id
            or intent.prepare_digest != prepare.prepare_digest
            or intent.message_digest != prepare.message.message_digest
            or intent.new_release_digest != prepare.message.new_release_digest
            or intent.new_manifest_digest != prepare.message.new_manifest_digest
        ):
            raise HarnessFactoryValidationError("factory commit intent crossed prepared transaction")

    @staticmethod
    def _fail(fail_at: str | None, boundary: str) -> None:
        if fail_at == boundary:
            raise HarnessFactoryInjectedFailure(f"injected failure at {boundary}")


__all__ = [
    "HARNESS_FACTORY_CHAT_DIR_NAME",
    "HARNESS_FACTORY_RUNTIME_KIND",
    "HarnessFactoryChatManifest",
    "HarnessFactoryCommitted",
    "HarnessFactoryCommitIntent",
    "HarnessFactoryConcurrencyError",
    "HarnessFactoryInjectedFailure",
    "HarnessFactoryMessage",
    "HarnessFactoryPrepare",
    "HarnessFactoryStaleHeadError",
    "HarnessFactoryTransactionError",
    "HarnessFactoryTransactionStore",
    "HarnessFactoryValidationError",
    "harness_factory_chat_digest",
    "harness_factory_commit_intent_digest",
    "harness_factory_committed_digest",
    "harness_factory_message_digest",
    "harness_factory_prepare_digest",
]
