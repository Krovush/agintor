"""Persistence for factory build/evolution chat projects.

A factory chat is a long-running conversation that produces and evolves one
runtime under a single project directory. The first message produces the
initial runtime; later messages are follow-up instructions that amend the
goal/success/benchmark contracts and rebuild the runtime in place.

Each project directory hosts at most one chat. Per-message planning artifacts
and the leader runtime hash are preserved under ``.factory_chat/messages/`` so
the conversation history remains auditable across rebuilds.
"""

from __future__ import annotations

import json
import secrets
import shutil
from pathlib import Path
from typing import Any

from .exceptions import AgintorError
from .schemas import FactoryChatIdentity, FactoryMessage
from .utils import ensure_directory, now_ts, stable_hash


CHAT_DIR_NAME = ".factory_chat"
CHAT_MANIFEST_NAME = "manifest.json"
MESSAGES_DIR_NAME = "messages"
MESSAGE_METADATA_FILE = "metadata.json"
MESSAGE_PROMPT_FILE = "prompt.txt"
PLANNING_ARTIFACT_FILES: tuple[str, ...] = (
    "goal_spec.json",
    "success_criteria.json",
    "benchmark_plan.json",
    "verifier_bundle.json",
    "runtime_plan.json",
    "deployment_contract.json",
    "export_summary.json",
    "build_summary.json",
)


class FactoryChatError(AgintorError):
    """Raised for invalid factory chat operations."""


class FactoryChatStore:
    def __init__(self, project_dir: str | Path) -> None:
        self.project_dir = Path(project_dir).resolve()

    @property
    def root(self) -> Path:
        return self.project_dir / CHAT_DIR_NAME

    @property
    def manifest_path(self) -> Path:
        return self.root / CHAT_MANIFEST_NAME

    def has_chat(self) -> bool:
        return self.manifest_path.exists()

    def load_chat(self) -> FactoryChatIdentity:
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise FactoryChatError(
                f"factory chat manifest not found at {self.manifest_path}; "
                "this project has not been initialized yet"
            ) from exc
        return (FactoryChatIdentity).model_validate(payload)

    def create_chat(
        self,
        *,
        goal_id: str,
        runtime_provider: str,
        agintor_provider: str,
        runtime_backend: str,
        runtime_profile_hash: str = "",
        chat_id: str | None = None,
    ) -> FactoryChatIdentity:
        if self.has_chat():
            raise FactoryChatError(
                f"factory chat already exists at {self.manifest_path}; "
                "use load_chat() to continue or pick a fresh project_dir"
            )
        allocated = self._allocate_chat_id(goal_id) if chat_id is None else str(chat_id).strip()
        if not allocated:
            raise FactoryChatError("chat_id must not be empty")
        ensure_directory(self.root / MESSAGES_DIR_NAME)
        identity = FactoryChatIdentity(
            chat_id=allocated,
            project_dir=str(self.project_dir),
            goal_id=str(goal_id or "").strip(),
            runtime_provider=str(runtime_provider or "").strip(),
            agintor_provider=str(agintor_provider or "").strip(),
            runtime_backend=str(runtime_backend or "local").strip(),
            runtime_profile_hash=str(runtime_profile_hash or "").strip(),
            created_at=now_ts(),
            message_count=0,
            last_message_id=None,
        )
        self._write_manifest(identity)
        return identity

    def latest_message(self) -> FactoryMessage | None:
        directory = self.root / MESSAGES_DIR_NAME
        if not directory.exists():
            return None
        message_dirs = sorted(
            (entry for entry in directory.iterdir() if entry.is_dir()),
            key=lambda path: path.name,
            reverse=True,
        )
        for entry in message_dirs:
            message = self._read_message_metadata(entry)
            if message is not None:
                return message
        return None

    def messages(self) -> list[FactoryMessage]:
        directory = self.root / MESSAGES_DIR_NAME
        if not directory.exists():
            return []
        ordered: list[FactoryMessage] = []
        for entry in sorted(directory.iterdir(), key=lambda path: path.name):
            if not entry.is_dir():
                continue
            message = self._read_message_metadata(entry)
            if message is not None:
                ordered.append(message)
        return ordered

    def next_message_index(self) -> int:
        latest = self.latest_message()
        return 0 if latest is None else latest.message_index + 1

    def allocate_chat_id(self, *, goal_id: str) -> str:
        return self._allocate_chat_id(goal_id)

    def allocate_message_id(self, *, message_index: int, prompt: str) -> str:
        seed = stable_hash(self.project_dir.name, message_index, prompt, secrets.token_hex(4))
        return f"fmsg.{seed[:12]}"

    def record_message(
        self,
        message: FactoryMessage,
        *,
        prompt_text: str,
        planning_artifacts: dict[str, str | Path] | None = None,
    ) -> FactoryMessage:
        identity = self.load_chat()
        if message.chat_id != identity.chat_id:
            raise FactoryChatError(
                f"factory message chat_id {message.chat_id!r} does not match chat {identity.chat_id!r}"
            )
        expected_index = self.next_message_index()
        if message.message_index != expected_index:
            raise FactoryChatError(
                f"factory chat {identity.chat_id!r} expected message_index {expected_index}, "
                f"got {message.message_index}"
            )
        artifact_sources = self._validate_planning_artifacts(planning_artifacts or {})
        messages_dir = ensure_directory(self.root / MESSAGES_DIR_NAME)
        message_dir = messages_dir / self._format_message_dir(message)
        if message_dir.exists():
            raise FactoryChatError(
                f"factory message {message.message_id!r} is already recorded or partially recorded"
            )
        temp_dir = messages_dir / f".{message_dir.name}.tmp.{secrets.token_hex(8)}"
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        try:
            ensure_directory(temp_dir)
            if prompt_text:
                (temp_dir / MESSAGE_PROMPT_FILE).write_text(prompt_text, encoding="utf-8")
            copied_paths = self._copy_planning_artifacts(temp_dir, artifact_sources)
            for field_name, copied_path in copied_paths.items():
                setattr(message, field_name, str((message_dir / copied_path.name).resolve()))
            self._write_json(temp_dir / MESSAGE_METADATA_FILE, (message).model_dump())
            temp_dir.rename(message_dir)
        except Exception:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise
        identity.message_count = max(identity.message_count, message.message_index + 1)
        identity.last_message_id = message.message_id
        self._write_manifest(identity)
        return message

    def _validate_planning_artifacts(
        self,
        artifacts: dict[str, str | Path],
    ) -> dict[str, Path]:
        validated: dict[str, Path] = {}
        for field_name, source in artifacts.items():
            if not source:
                continue
            source_path = Path(source)
            if not source_path.exists() or not source_path.is_file():
                raise FactoryChatError(
                    f"factory planning artifact {field_name!r} is missing at {source_path}"
                )
            validated[field_name] = source_path
        return validated

    def _copy_planning_artifacts(
        self,
        message_dir: Path,
        artifacts: dict[str, Path],
    ) -> dict[str, Path]:
        copied: dict[str, Path] = {}
        for field_name, source in artifacts.items():
            destination = message_dir / source.name
            shutil.copy2(source, destination)
            copied[field_name] = destination
        return copied

    def _allocate_chat_id(self, goal_id: str) -> str:
        for _ in range(64):
            candidate = f"chat.{stable_hash(goal_id, secrets.token_hex(8), now_ts())[:12]}"
            if not (self.root / candidate).exists():
                return candidate
        raise FactoryChatError("unable to allocate a unique factory chat id")

    @staticmethod
    def _format_message_dir(message: FactoryMessage) -> str:
        return f"{message.message_index:04d}_{message.message_id}"

    def _read_message_metadata(self, message_dir: Path) -> FactoryMessage | None:
        metadata_path = message_dir / MESSAGE_METADATA_FILE
        if not metadata_path.exists():
            raise FactoryChatError(f"factory message metadata is missing at {metadata_path}")
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise FactoryChatError(f"factory message metadata is corrupt at {metadata_path}: {exc}") from exc
        try:
            return (FactoryMessage).model_validate(payload)
        except Exception as exc:
            raise FactoryChatError(f"factory message metadata is invalid at {metadata_path}: {exc}") from exc

    def _write_manifest(self, identity: FactoryChatIdentity) -> None:
        self._write_json(self.manifest_path, (identity).model_dump())

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        ensure_directory(path.parent)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
