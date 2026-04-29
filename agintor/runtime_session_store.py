"""Persistence for runtime chat sessions.

A runtime chat session is a sequence of user messages exchanged with one built
runtime. The runtime's long-term memory and predictor state carry across
messages within a session; in-flight execution state (open handles, side-effect
ledger, plan frontier) does not.

Each runtime under ``<runtime_dir>`` may host many independent sessions. A
session is pinned to the runtime hash that was current when the session was
created; if the runtime is rebuilt and its hash changes, sessions created
against the prior hash are no longer continuable.
"""

from __future__ import annotations

import json
import secrets
import shutil
from pathlib import Path
from typing import Any

from .exceptions import AgintorError, RuntimeLoadError
from .schemas import (
    LongTermGraphSnapshot,
    PredictorSnapshot,
    RuntimeSessionIdentity,
    RuntimeSessionMessage,
    RuntimeSessionSeed,
    RuntimeSolveResponse,
    SolveResult,
)
from .utils import ensure_directory, now_ts, stable_hash


SESSIONS_DIR_NAME = ".runtime_sessions"
SESSION_MANIFEST_NAME = "manifest.json"
MESSAGES_DIR_NAME = "messages"
MESSAGE_STAGING_DIR_NAME = ".message_staging"
MESSAGE_FILE_NAMES = {
    "request": "request.json",
    "response": "response.json",
    "result": "result.json",
    "boundary_state": "boundary_state.json",
    "long_term_graph": "long_term_graph.json",
    "predictor_snapshot": "predictor_snapshot.json",
    "metadata": "metadata.json",
    "prompt": "prompt.txt",
}


class RuntimeSessionMismatchError(AgintorError):
    """Raised when a session's pinned runtime hash does not match the runtime."""


class RuntimeSessionStore:
    def __init__(self, runtime_dir: str | Path) -> None:
        self.runtime_dir = Path(runtime_dir).resolve()

    @property
    def root(self) -> Path:
        return self.runtime_dir / SESSIONS_DIR_NAME

    def list_sessions(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(
            entry.name
            for entry in self.root.iterdir()
            if entry.is_dir() and (entry / SESSION_MANIFEST_NAME).exists()
        )

    def session_dir(self, session_id: str) -> Path:
        if not self._is_safe_session_id(session_id):
            raise RuntimeLoadError(f"invalid runtime session id {session_id!r}")
        return self.root / session_id

    def has_session(self, session_id: str) -> bool:
        if not self._is_safe_session_id(session_id):
            return False
        return (self.session_dir(session_id) / SESSION_MANIFEST_NAME).exists()

    def create_session(
        self,
        *,
        runtime_hash: str,
        runtime_backend: str | None = None,
        session_id: str | None = None,
    ) -> RuntimeSessionIdentity:
        runtime_hash_text = str(runtime_hash or "").strip()
        if not runtime_hash_text:
            raise RuntimeLoadError("cannot create a runtime session without a runtime hash")
        runtime_backend_text = str(runtime_backend or "local").strip().lower()
        if not runtime_backend_text:
            raise RuntimeLoadError("cannot create a runtime session without a runtime backend")
        if session_id is None:
            allocated = self._allocate_session_id(runtime_hash_text)
        else:
            allocated = str(session_id).strip()
            if not allocated:
                raise RuntimeLoadError("session_id must not be empty")
            if self.has_session(allocated):
                raise RuntimeLoadError(f"runtime session {allocated!r} already exists")
        session_path = ensure_directory(self.session_dir(allocated))
        ensure_directory(session_path / MESSAGES_DIR_NAME)
        identity = RuntimeSessionIdentity(
            session_id=allocated,
            runtime_dir=str(self.runtime_dir),
            runtime_hash=runtime_hash_text,
            runtime_backend=runtime_backend_text,
            created_at=now_ts(),
            message_count=0,
            last_message_id=None,
        )
        self._write_manifest(identity)
        return identity

    def load_session(
        self,
        session_id: str,
        *,
        runtime_hash: str,
        runtime_backend: str | None = None,
    ) -> RuntimeSessionIdentity:
        if not self.has_session(session_id):
            raise RuntimeLoadError(f"runtime session {session_id!r} not found under {self.runtime_dir}")
        identity = self._read_manifest(session_id)
        runtime_hash_text = str(runtime_hash or "").strip()
        identity_runtime_hash = str(identity.runtime_hash or "").strip()
        if not runtime_hash_text or not identity_runtime_hash:
            raise RuntimeSessionMismatchError(
                f"runtime session {session_id!r} has incomplete runtime hash pinning; "
                "start a new session against the current runtime"
            )
        if runtime_hash_text != identity_runtime_hash:
            raise RuntimeSessionMismatchError(
                f"runtime session {session_id!r} was created against runtime hash "
                f"{identity.runtime_hash!r} but the runtime now reports {runtime_hash_text!r}; "
                "start a new session against the rebuilt runtime"
            )
        runtime_backend_text = str(runtime_backend or "").strip().lower()
        identity_backend = str(identity.runtime_backend or "").strip().lower()
        if runtime_backend_text:
            if not identity_backend:
                raise RuntimeSessionMismatchError(
                    f"runtime session {session_id!r} has incomplete runtime backend pinning; "
                    "start a new session against the current runtime"
                )
            if runtime_backend_text != identity_backend:
                raise RuntimeSessionMismatchError(
                    f"runtime session {session_id!r} was created against runtime backend "
                    f"{identity_backend!r} but the runtime now uses {runtime_backend_text!r}; "
                    "start a new session for the selected backend"
                )
        return identity

    def latest_message(self, session_id: str) -> RuntimeSessionMessage | None:
        directory = self.session_dir(session_id) / MESSAGES_DIR_NAME
        if not directory.exists():
            return None
        message_dirs = sorted(
            (entry for entry in directory.iterdir() if entry.is_dir()),
            key=lambda path: path.name,
        )
        for entry in reversed(message_dirs):
            message = self._read_message_metadata(entry)
            if message is not None:
                return message
        return None

    def latest_completed_message(self, session_id: str) -> RuntimeSessionMessage | None:
        directory = self.session_dir(session_id) / MESSAGES_DIR_NAME
        if not directory.exists():
            return None
        message_dirs = sorted(
            (entry for entry in directory.iterdir() if entry.is_dir()),
            key=lambda path: path.name,
            reverse=True,
        )
        for entry in message_dirs:
            message = self._read_message_metadata(entry)
            if message is None:
                continue
            if message.lifecycle_state != "completed":
                continue
            if not (
                message.boundary_state_path
                or message.long_term_graph_path
                or message.predictor_snapshot_path
            ):
                raise RuntimeLoadError(
                    f"completed runtime session message {message.message_id!r} "
                    "has no persisted carryover artifacts"
                )
            else:
                return message
        return None

    def messages(self, session_id: str) -> list[RuntimeSessionMessage]:
        directory = self.session_dir(session_id) / MESSAGES_DIR_NAME
        if not directory.exists():
            return []
        ordered: list[RuntimeSessionMessage] = []
        for entry in sorted(directory.iterdir(), key=lambda path: path.name):
            if not entry.is_dir():
                continue
            message = self._read_message_metadata(entry)
            if message is not None:
                ordered.append(message)
        return ordered

    def next_message_index(self, session_id: str) -> int:
        latest = self.latest_message(session_id)
        return 0 if latest is None else latest.message_index + 1

    def seed_for_next_message(self, session_id: str) -> RuntimeSessionSeed | None:
        latest = self.latest_completed_message(session_id)
        if latest is None:
            return None
        long_term_graph = self._load_long_term_graph(latest.long_term_graph_path)
        predictor_snapshot = self._load_predictor_snapshot(latest.predictor_snapshot_path)
        carryover = self._load_short_term_carryover(latest)
        return RuntimeSessionSeed(
            session_id=session_id,
            message_index=self.next_message_index(session_id),
            parent_message_id=latest.message_id,
            long_term_graph=long_term_graph or LongTermGraphSnapshot(),
            predictor_snapshot=predictor_snapshot,
            short_term_carryover=carryover,
        )

    def allocate_message_id(self, session_id: str, *, message_index: int, prompt: str) -> str:
        seed = stable_hash(session_id, message_index, prompt, secrets.token_hex(4))
        return f"msg.{seed[:12]}"

    def record_message(
        self,
        session_id: str,
        message: RuntimeSessionMessage,
        *,
        prompt_text: str,
        request_payload: dict[str, Any] | None,
        response: RuntimeSolveResponse | None,
        result: SolveResult | None,
    ) -> RuntimeSessionMessage:
        identity = self._read_manifest(session_id)
        if message.session_id != identity.session_id:
            raise RuntimeLoadError(
                f"runtime session message {message.message_id!r} belongs to {message.session_id!r}, "
                f"not {identity.session_id!r}"
            )
        expected_index = self.next_message_index(session_id)
        if message.message_index != expected_index:
            raise RuntimeLoadError(
                f"runtime session {session_id!r} expected message_index {expected_index}, "
                f"got {message.message_index}"
            )
        result_has_carryover_artifacts = bool(
            result is not None
            and (
                result.post_message_short_term_export
                or result.post_message_long_term_graph is not None
                or result.post_message_predictor_snapshot is not None
            )
        )
        if message.lifecycle_state == "completed" and not (
            result_has_carryover_artifacts
            or message.boundary_state_path
            or message.long_term_graph_path
            or message.predictor_snapshot_path
        ):
            raise RuntimeLoadError(
                f"completed runtime session message {message.message_id!r} "
                "has no persisted carryover artifacts"
            )
        messages_dir = ensure_directory(self.session_dir(session_id) / MESSAGES_DIR_NAME)
        message_dir_name = self._format_message_dir(message)
        message_dir = messages_dir / message_dir_name
        if message_dir.exists():
            raise RuntimeLoadError(f"runtime session message {message.message_id!r} is already recorded")

        staging_root = ensure_directory(self.session_dir(session_id) / MESSAGE_STAGING_DIR_NAME)
        staging_dir = staging_root / f"{message_dir_name}.{stable_hash(message.message_id, now_ts())[:12]}"
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        ensure_directory(staging_dir)

        recorded = message.model_copy(deep=True)
        try:
            if prompt_text:
                (staging_dir / MESSAGE_FILE_NAMES["prompt"]).write_text(prompt_text, encoding="utf-8")
            if request_payload is not None:
                self._write_json(staging_dir / MESSAGE_FILE_NAMES["request"], request_payload)
            if response is not None:
                self._write_json(staging_dir / MESSAGE_FILE_NAMES["response"], (response).model_dump())
                recorded.response_path = str((message_dir / MESSAGE_FILE_NAMES["response"]).resolve())
            if result is not None:
                self._write_json(staging_dir / MESSAGE_FILE_NAMES["result"], (result).model_dump())
                recorded.result_path = str((message_dir / MESSAGE_FILE_NAMES["result"]).resolve())
                persist_carryover = recorded.lifecycle_state == "completed"
                if persist_carryover and result.post_message_long_term_graph is not None:
                    long_term_path = staging_dir / MESSAGE_FILE_NAMES["long_term_graph"]
                    self._write_json(long_term_path, (result.post_message_long_term_graph).model_dump())
                    recorded.long_term_graph_path = str(
                        (message_dir / MESSAGE_FILE_NAMES["long_term_graph"]).resolve()
                    )
                if persist_carryover and result.post_message_predictor_snapshot is not None:
                    predictor_path = staging_dir / MESSAGE_FILE_NAMES["predictor_snapshot"]
                    self._write_json(predictor_path, (result.post_message_predictor_snapshot).model_dump())
                    recorded.predictor_snapshot_path = str(
                        (message_dir / MESSAGE_FILE_NAMES["predictor_snapshot"]).resolve()
                    )
                if persist_carryover and (
                    result.post_message_short_term_export
                    or result.post_message_long_term_graph is not None
                    or result.post_message_predictor_snapshot is not None
                ):
                    boundary_path = staging_dir / MESSAGE_FILE_NAMES["boundary_state"]
                    self._write_json(
                        boundary_path,
                        {"short_term_carryover": list(result.post_message_short_term_export)},
                    )
                    recorded.boundary_state_path = str(
                        (message_dir / MESSAGE_FILE_NAMES["boundary_state"]).resolve()
                    )
            self._write_json(staging_dir / MESSAGE_FILE_NAMES["metadata"], (recorded).model_dump())
            staging_dir.rename(message_dir)
        except Exception:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise
        finally:
            try:
                if staging_root.exists() and not any(staging_root.iterdir()):
                    staging_root.rmdir()
            except OSError:
                pass

        identity.message_count = max(identity.message_count, recorded.message_index + 1)
        identity.last_message_id = recorded.message_id
        self._write_manifest(identity)
        return recorded

    def _allocate_session_id(self, runtime_hash: str) -> str:
        for _ in range(64):
            candidate = f"sess.{stable_hash(runtime_hash, secrets.token_hex(8), now_ts())[:12]}"
            if not self.has_session(candidate):
                return candidate
        raise RuntimeLoadError("unable to allocate a unique runtime session id")

    @staticmethod
    def _is_safe_session_id(session_id: str) -> bool:
        text = str(session_id or "").strip()
        if not text:
            return False
        if text in {".", ".."}:
            return False
        return all(ch.isalnum() or ch in {".", "-", "_"} for ch in text)

    def _read_manifest(self, session_id: str) -> RuntimeSessionIdentity:
        manifest_path = self.session_dir(session_id) / SESSION_MANIFEST_NAME
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RuntimeLoadError(f"runtime session manifest not found at {manifest_path}") from exc
        return (RuntimeSessionIdentity).model_validate(payload)

    def _write_manifest(self, identity: RuntimeSessionIdentity) -> None:
        manifest_path = self.session_dir(identity.session_id) / SESSION_MANIFEST_NAME
        self._write_json(manifest_path, (identity).model_dump())

    @staticmethod
    def _format_message_dir(message: RuntimeSessionMessage) -> str:
        return f"{message.message_index:04d}_{message.message_id}"

    def _read_message_metadata(self, message_dir: Path) -> RuntimeSessionMessage | None:
        metadata_path = message_dir / MESSAGE_FILE_NAMES["metadata"]
        if not metadata_path.exists():
            raise RuntimeLoadError(f"runtime session message metadata is missing at {metadata_path}")
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeLoadError(f"runtime session message metadata is corrupt at {metadata_path}: {exc}") from exc
        try:
            return (RuntimeSessionMessage).model_validate(payload)
        except Exception as exc:
            raise RuntimeLoadError(f"runtime session message metadata is invalid at {metadata_path}: {exc}") from exc

    @staticmethod
    def _load_long_term_graph(path_text: str | None) -> LongTermGraphSnapshot | None:
        if not path_text:
            return None
        path = Path(path_text)
        if not path.exists():
            raise RuntimeLoadError(f"runtime session long-term graph artifact is missing at {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return (LongTermGraphSnapshot).model_validate(payload)
        except Exception as exc:
            raise RuntimeLoadError(f"runtime session long-term graph artifact is corrupt at {path}: {exc}") from exc

    @staticmethod
    def _load_predictor_snapshot(path_text: str | None) -> PredictorSnapshot | None:
        if not path_text:
            return None
        path = Path(path_text)
        if not path.exists():
            raise RuntimeLoadError(f"runtime session predictor artifact is missing at {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return (PredictorSnapshot).model_validate(payload)
        except Exception as exc:
            raise RuntimeLoadError(f"runtime session predictor artifact is corrupt at {path}: {exc}") from exc

    @staticmethod
    def _load_short_term_carryover(message: RuntimeSessionMessage) -> list[dict[str, Any]]:
        if not message.boundary_state_path:
            return []
        path = Path(message.boundary_state_path)
        if not path.exists():
            raise RuntimeLoadError(f"runtime session boundary artifact is missing at {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeLoadError(f"runtime session boundary artifact is corrupt at {path}: {exc}") from exc
        rows = payload.get("short_term_carryover")
        if not isinstance(rows, list):
            raise RuntimeLoadError(f"runtime session boundary artifact has invalid carryover rows at {path}")
        invalid_index = next((index for index, row in enumerate(rows) if not isinstance(row, dict)), None)
        if invalid_index is not None:
            raise RuntimeLoadError(
                f"runtime session boundary artifact has invalid carryover row {invalid_index} at {path}"
            )
        return [dict(row) for row in rows]

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        ensure_directory(path.parent)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
