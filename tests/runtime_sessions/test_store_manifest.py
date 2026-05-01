from __future__ import annotations

import json
from pathlib import Path

import pytest

from agintor.core.exceptions import RuntimeLoadError
from agintor.storage.runtime_session_store import RuntimeSessionMismatchError, RuntimeSessionStore

from ._support import _runtime_dir


def test_runtime_session_store_create_and_load(tmp_path: Path) -> None:
    runtime_dir = _runtime_dir(tmp_path)
    store = RuntimeSessionStore(runtime_dir)
    assert store.list_sessions() == []

    identity = store.create_session(runtime_hash="runtime.hash.alpha")
    assert identity.session_id.startswith("sess.")
    assert identity.runtime_hash == "runtime.hash.alpha"
    assert identity.runtime_dir == str(runtime_dir.resolve())
    assert identity.message_count == 0
    assert identity.last_message_id is None
    assert store.list_sessions() == [identity.session_id]

    loaded = store.load_session(identity.session_id, runtime_hash="runtime.hash.alpha")
    assert loaded.session_id == identity.session_id


def test_runtime_session_store_pinning_to_runtime_hash(tmp_path: Path) -> None:
    store = RuntimeSessionStore(_runtime_dir(tmp_path))
    identity = store.create_session(runtime_hash="runtime.hash.alpha")
    with pytest.raises(RuntimeSessionMismatchError):
        store.load_session(identity.session_id, runtime_hash="runtime.hash.beta")


def test_runtime_session_store_pinning_to_runtime_backend(tmp_path: Path) -> None:
    store = RuntimeSessionStore(_runtime_dir(tmp_path))
    identity = store.create_session(runtime_hash="runtime.hash.alpha", runtime_backend="local")
    with pytest.raises(RuntimeSessionMismatchError, match="runtime backend"):
        store.load_session(
            identity.session_id,
            runtime_hash="runtime.hash.alpha",
            runtime_backend="docker",
        )


def test_runtime_session_store_rejects_partial_manifest_hash_pin(tmp_path: Path) -> None:
    store = RuntimeSessionStore(_runtime_dir(tmp_path))
    identity = store.create_session(runtime_hash="runtime.hash.alpha")
    manifest_path = store.session_dir(identity.session_id) / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["runtime_hash"] = ""
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    with pytest.raises(RuntimeSessionMismatchError, match="incomplete runtime hash"):
        store.load_session(identity.session_id, runtime_hash="runtime.hash.alpha")

    with pytest.raises(RuntimeSessionMismatchError, match="incomplete runtime hash"):
        store.load_session(identity.session_id, runtime_hash="")


def test_runtime_session_store_rejects_partial_manifest_backend_pin(tmp_path: Path) -> None:
    store = RuntimeSessionStore(_runtime_dir(tmp_path))
    identity = store.create_session(runtime_hash="runtime.hash.alpha", runtime_backend="local")
    manifest_path = store.session_dir(identity.session_id) / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload.pop("runtime_backend", None)
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    with pytest.raises(RuntimeSessionMismatchError, match="incomplete runtime backend"):
        store.load_session(
            identity.session_id,
            runtime_hash="runtime.hash.alpha",
            runtime_backend="local",
        )


def test_runtime_session_store_rejects_unknown_session(tmp_path: Path) -> None:
    store = RuntimeSessionStore(_runtime_dir(tmp_path))
    with pytest.raises(RuntimeLoadError):
        store.load_session("sess.missing", runtime_hash="runtime.hash.alpha")


@pytest.mark.parametrize("session_id", [".", ".."])
def test_runtime_session_store_rejects_dot_only_session_ids(tmp_path: Path, session_id: str) -> None:
    runtime_dir = _runtime_dir(tmp_path)
    store = RuntimeSessionStore(runtime_dir)

    with pytest.raises(RuntimeLoadError, match="invalid runtime session id"):
        store.create_session(runtime_hash="runtime.hash.alpha", session_id=session_id)

    assert not (runtime_dir / "manifest.json").exists()
    assert not (runtime_dir / ".runtime_sessions" / "manifest.json").exists()
