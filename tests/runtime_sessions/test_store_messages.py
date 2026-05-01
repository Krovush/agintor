from __future__ import annotations

import json
from pathlib import Path

import pytest

from agintor.contracts import LongTermGraphSnapshot, PredictorSnapshot, RuntimeSessionMessage
from agintor.core.exceptions import RuntimeLoadError
from agintor.storage.runtime_session_store import RuntimeSessionStore

from ._support import _runtime_dir, _solve_result_with_state


def test_runtime_session_store_records_message_state(tmp_path: Path) -> None:
    store = RuntimeSessionStore(_runtime_dir(tmp_path))
    identity = store.create_session(runtime_hash="runtime.hash.alpha")

    long_term = LongTermGraphSnapshot()
    predictor = PredictorSnapshot()
    short_term = [{"event": "model_response", "summary": "hello"}]
    result = _solve_result_with_state(
        long_term_graph=long_term,
        predictor_snapshot=predictor,
        short_term_export=short_term,
    )

    message_id = store.allocate_message_id(identity.session_id, message_index=0, prompt="hello world")
    message = RuntimeSessionMessage(
        message_id=message_id,
        message_index=0,
        parent_message_id=None,
        session_id=identity.session_id,
        request_id="req.1",
        prompt="hello world",
        lifecycle_state="completed",
    )
    recorded = store.record_message(
        identity.session_id,
        message,
        prompt_text="hello world",
        request_payload={"mode": "user_request"},
        response=None,
        result=result,
    )

    assert Path(recorded.boundary_state_path).exists()
    assert Path(recorded.long_term_graph_path).exists()
    assert Path(recorded.predictor_snapshot_path).exists()
    assert Path(recorded.result_path).exists()
    refreshed = store.load_session(identity.session_id, runtime_hash="runtime.hash.alpha")
    assert refreshed.message_count == 1
    assert refreshed.last_message_id == message_id

    assert store.next_message_index(identity.session_id) == 1
    assert store.latest_message(identity.session_id) is not None


def test_runtime_session_store_stages_message_until_metadata_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RuntimeSessionStore(_runtime_dir(tmp_path))
    identity = store.create_session(runtime_hash="runtime.hash.alpha")
    result = _solve_result_with_state(long_term_graph=LongTermGraphSnapshot())
    message_id = store.allocate_message_id(identity.session_id, message_index=0, prompt="hello")
    message = RuntimeSessionMessage(
        message_id=message_id,
        message_index=0,
        parent_message_id=None,
        session_id=identity.session_id,
        request_id="req.1",
        prompt="hello",
        lifecycle_state="completed",
    )

    original_write_json = store._write_json

    def fail_before_metadata(path: Path, payload: object) -> None:
        if path.name == "result.json":
            raise OSError("disk full")
        original_write_json(path, payload)

    monkeypatch.setattr(store, "_write_json", fail_before_metadata)

    with pytest.raises(OSError, match="disk full"):
        store.record_message(
            identity.session_id,
            message,
            prompt_text="hello",
            request_payload={"mode": "user_request"},
            response=None,
            result=result,
        )

    messages_dir = store.session_dir(identity.session_id) / "messages"
    assert list(messages_dir.iterdir()) == []
    assert not (store.session_dir(identity.session_id) / ".message_staging").exists()
    assert store.latest_message(identity.session_id) is None
    assert store.next_message_index(identity.session_id) == 0


def test_runtime_session_store_seeds_next_message_with_completed_state(tmp_path: Path) -> None:
    store = RuntimeSessionStore(_runtime_dir(tmp_path))
    identity = store.create_session(runtime_hash="runtime.hash.alpha")
    long_term = LongTermGraphSnapshot()
    predictor = PredictorSnapshot()
    short_term = [{"event": "model_response", "summary": "first turn"}]

    message_id = store.allocate_message_id(identity.session_id, message_index=0, prompt="first")
    message = RuntimeSessionMessage(
        message_id=message_id,
        message_index=0,
        parent_message_id=None,
        session_id=identity.session_id,
        request_id="req.first",
        prompt="first",
        lifecycle_state="completed",
    )
    store.record_message(
        identity.session_id,
        message,
        prompt_text="first",
        request_payload={"mode": "user_request"},
        response=None,
        result=_solve_result_with_state(
            long_term_graph=long_term,
            predictor_snapshot=predictor,
            short_term_export=short_term,
        ),
    )

    seed = store.seed_for_next_message(identity.session_id)
    assert seed is not None
    assert seed.session_id == identity.session_id
    assert seed.message_index == 1
    assert seed.parent_message_id == message_id
    assert seed.short_term_carryover == short_term


def test_runtime_session_store_skips_failed_messages_for_carryover(tmp_path: Path) -> None:
    store = RuntimeSessionStore(_runtime_dir(tmp_path))
    identity = store.create_session(runtime_hash="runtime.hash.alpha")
    short_term_first = [{"event": "model_response", "summary": "first turn"}]

    first_id = store.allocate_message_id(identity.session_id, message_index=0, prompt="first")
    store.record_message(
        identity.session_id,
        RuntimeSessionMessage(
            message_id=first_id,
            message_index=0,
            parent_message_id=None,
            session_id=identity.session_id,
            request_id="req.first",
            prompt="first",
            lifecycle_state="completed",
        ),
        prompt_text="first",
        request_payload={"mode": "user_request"},
        response=None,
        result=_solve_result_with_state(
            long_term_graph=LongTermGraphSnapshot(),
            predictor_snapshot=PredictorSnapshot(),
            short_term_export=short_term_first,
        ),
    )

    failed_id = store.allocate_message_id(identity.session_id, message_index=1, prompt="failed")
    store.record_message(
        identity.session_id,
        RuntimeSessionMessage(
            message_id=failed_id,
            message_index=1,
            parent_message_id=first_id,
            session_id=identity.session_id,
            request_id="req.failed",
            prompt="failed",
            lifecycle_state="failed",
        ),
        prompt_text="failed",
        request_payload={"mode": "user_request"},
        response=None,
        result=_solve_result_with_state(),
    )

    seed = store.seed_for_next_message(identity.session_id)
    assert seed is not None
    assert seed.message_index == 2
    assert seed.parent_message_id == first_id
    assert seed.short_term_carryover == short_term_first


def test_runtime_session_store_uses_completed_state_even_with_empty_recap(tmp_path: Path) -> None:
    store = RuntimeSessionStore(_runtime_dir(tmp_path))
    identity = store.create_session(runtime_hash="runtime.hash.alpha")
    message_id = store.allocate_message_id(identity.session_id, message_index=0, prompt="first")
    recorded = store.record_message(
        identity.session_id,
        RuntimeSessionMessage(
            message_id=message_id,
            message_index=0,
            parent_message_id=None,
            session_id=identity.session_id,
            request_id="req.first",
            prompt="first",
            lifecycle_state="completed",
        ),
        prompt_text="first",
        request_payload={"mode": "user_request"},
        response=None,
        result=_solve_result_with_state(
            long_term_graph=LongTermGraphSnapshot(),
            predictor_snapshot=PredictorSnapshot(),
            short_term_export=[],
        ),
    )

    assert recorded.boundary_state_path is not None
    seed = store.seed_for_next_message(identity.session_id)
    assert seed is not None
    assert seed.message_index == 1
    assert seed.short_term_carryover == []


def test_runtime_session_store_rejects_completed_message_without_carryover_artifacts(tmp_path: Path) -> None:
    store = RuntimeSessionStore(_runtime_dir(tmp_path))
    identity = store.create_session(runtime_hash="runtime.hash.alpha")
    message_id = store.allocate_message_id(identity.session_id, message_index=0, prompt="first")

    with pytest.raises(RuntimeLoadError, match="no persisted carryover artifacts"):
        store.record_message(
            identity.session_id,
            RuntimeSessionMessage(
                message_id=message_id,
                message_index=0,
                parent_message_id=None,
                session_id=identity.session_id,
                request_id="req.first",
                prompt="first",
                lifecycle_state="completed",
            ),
            prompt_text="first",
            request_payload={"mode": "user_request"},
            response=None,
            result=None,
        )


def test_runtime_session_store_rejects_partial_completed_message_metadata(tmp_path: Path) -> None:
    store = RuntimeSessionStore(_runtime_dir(tmp_path))
    identity = store.create_session(runtime_hash="runtime.hash.alpha")
    message = RuntimeSessionMessage(
        message_id="msg.partial",
        message_index=0,
        parent_message_id=None,
        session_id=identity.session_id,
        request_id="req.partial",
        prompt="partial",
        lifecycle_state="completed",
    )
    message_dir = store.session_dir(identity.session_id) / "messages" / "0000_msg.partial"
    message_dir.mkdir(parents=True)
    (message_dir / "metadata.json").write_text(
        json.dumps(message.model_dump(), indent=2, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeLoadError, match="no persisted carryover artifacts"):
        store.seed_for_next_message(identity.session_id)


def test_runtime_session_store_does_not_persist_carryover_for_failed_turns(tmp_path: Path) -> None:
    store = RuntimeSessionStore(_runtime_dir(tmp_path))
    identity = store.create_session(runtime_hash="runtime.hash.alpha")
    message_id = store.allocate_message_id(identity.session_id, message_index=0, prompt="first")
    recorded = store.record_message(
        identity.session_id,
        RuntimeSessionMessage(
            message_id=message_id,
            message_index=0,
            parent_message_id=None,
            session_id=identity.session_id,
            request_id="req.first",
            prompt="first",
            lifecycle_state="failed",
        ),
        prompt_text="first",
        request_payload={"mode": "user_request"},
        response=None,
        result=_solve_result_with_state(
            long_term_graph=LongTermGraphSnapshot(),
            predictor_snapshot=PredictorSnapshot(),
            short_term_export=[{"kind": "assistant_summary", "content": "do not carry"}],
        ),
    )

    assert recorded.boundary_state_path is None
    assert recorded.long_term_graph_path is None
    assert recorded.predictor_snapshot_path is None
    assert store.seed_for_next_message(identity.session_id) is None


def test_runtime_session_store_rejects_corrupt_completed_carryover(tmp_path: Path) -> None:
    store = RuntimeSessionStore(_runtime_dir(tmp_path))
    identity = store.create_session(runtime_hash="runtime.hash.alpha")
    message_id = store.allocate_message_id(identity.session_id, message_index=0, prompt="first")
    recorded = store.record_message(
        identity.session_id,
        RuntimeSessionMessage(
            message_id=message_id,
            message_index=0,
            parent_message_id=None,
            session_id=identity.session_id,
            request_id="req.first",
            prompt="first",
            lifecycle_state="completed",
        ),
        prompt_text="first",
        request_payload={"mode": "user_request"},
        response=None,
        result=_solve_result_with_state(
            long_term_graph=LongTermGraphSnapshot(),
            predictor_snapshot=PredictorSnapshot(),
        ),
    )
    assert recorded.long_term_graph_path is not None
    Path(recorded.long_term_graph_path).write_text("{not json", encoding="utf-8")

    with pytest.raises(RuntimeLoadError, match="long-term graph"):
        store.seed_for_next_message(identity.session_id)


def test_runtime_session_store_rejects_invalid_boundary_rows(tmp_path: Path) -> None:
    store = RuntimeSessionStore(_runtime_dir(tmp_path))
    identity = store.create_session(runtime_hash="runtime.hash.alpha")
    message_id = store.allocate_message_id(identity.session_id, message_index=0, prompt="first")
    recorded = store.record_message(
        identity.session_id,
        RuntimeSessionMessage(
            message_id=message_id,
            message_index=0,
            parent_message_id=None,
            session_id=identity.session_id,
            request_id="req.first",
            prompt="first",
            lifecycle_state="completed",
        ),
        prompt_text="first",
        request_payload={"mode": "user_request"},
        response=None,
        result=_solve_result_with_state(
            long_term_graph=LongTermGraphSnapshot(),
            predictor_snapshot=PredictorSnapshot(),
        ),
    )
    assert recorded.boundary_state_path is not None
    Path(recorded.boundary_state_path).write_text(
        json.dumps({"short_term_carryover": "not-a-list"}),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeLoadError, match="invalid carryover rows"):
        store.seed_for_next_message(identity.session_id)


def test_runtime_session_store_rejects_invalid_boundary_row_items(tmp_path: Path) -> None:
    store = RuntimeSessionStore(_runtime_dir(tmp_path))
    identity = store.create_session(runtime_hash="runtime.hash.alpha")
    message_id = store.allocate_message_id(identity.session_id, message_index=0, prompt="first")
    recorded = store.record_message(
        identity.session_id,
        RuntimeSessionMessage(
            message_id=message_id,
            message_index=0,
            parent_message_id=None,
            session_id=identity.session_id,
            request_id="req.first",
            prompt="first",
            lifecycle_state="completed",
        ),
        prompt_text="first",
        request_payload={"mode": "user_request"},
        response=None,
        result=_solve_result_with_state(
            long_term_graph=LongTermGraphSnapshot(),
            predictor_snapshot=PredictorSnapshot(),
        ),
    )
    assert recorded.boundary_state_path is not None
    Path(recorded.boundary_state_path).write_text(
        json.dumps({"short_term_carryover": [{"kind": "ok"}, "bad-row"]}),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeLoadError, match="invalid carryover row 1"):
        store.seed_for_next_message(identity.session_id)


def test_runtime_session_store_rejects_corrupt_message_metadata(tmp_path: Path) -> None:
    store = RuntimeSessionStore(_runtime_dir(tmp_path))
    identity = store.create_session(runtime_hash="runtime.hash.alpha")
    message_id = store.allocate_message_id(identity.session_id, message_index=0, prompt="first")
    recorded = store.record_message(
        identity.session_id,
        RuntimeSessionMessage(
            message_id=message_id,
            message_index=0,
            parent_message_id=None,
            session_id=identity.session_id,
            request_id="req.first",
            prompt="first",
            lifecycle_state="completed",
        ),
        prompt_text="first",
        request_payload={"mode": "user_request"},
        response=None,
        result=_solve_result_with_state(long_term_graph=LongTermGraphSnapshot()),
    )
    metadata_path = Path(recorded.result_path).parent / "metadata.json"
    metadata_path.write_text("{not json", encoding="utf-8")

    with pytest.raises(RuntimeLoadError, match="message metadata"):
        store.latest_message(identity.session_id)


def test_runtime_session_store_rejects_missing_message_metadata(tmp_path: Path) -> None:
    store = RuntimeSessionStore(_runtime_dir(tmp_path))
    identity = store.create_session(runtime_hash="runtime.hash.alpha")
    message_dir = store.session_dir(identity.session_id) / "messages" / "0000_msg.missing"
    message_dir.mkdir(parents=True)
    (message_dir / "prompt.txt").write_text("partial", encoding="utf-8")

    with pytest.raises(RuntimeLoadError, match="metadata is missing"):
        store.latest_message(identity.session_id)
