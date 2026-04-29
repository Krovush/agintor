from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agintor.exceptions import RuntimeLoadError
from agintor.runtime_session_store import RuntimeSessionMismatchError, RuntimeSessionStore
from agintor.schemas import (
    CapabilityExchange,
    LongTermGraphSnapshot,
    PredictorSnapshot,
    RuntimeSessionMessage,
    RuntimeSolveResponse,
    SolveResult,
)
from agintor.versioning import RUNTIME_CONTRACT_VERSION


def _runtime_dir(tmp_path: Path, name: str = "runtime.alpha") -> Path:
    runtime_dir = tmp_path / name
    runtime_dir.mkdir(parents=True, exist_ok=True)
    return runtime_dir


def _solve_result_with_state(
    *,
    long_term_graph: LongTermGraphSnapshot | None = None,
    predictor_snapshot: PredictorSnapshot | None = None,
    short_term_export: list[dict] | None = None,
) -> SolveResult:
    return SolveResult(
        request_id="req.1",
        runtime_hash="runtime.hash.alpha",
        run_id="",
        attempt_id="",
        run_root="",
        run_lifecycle_state="completed",
        artifact={"text": "ok"},
        status="completed",
        summary="ok",
        post_message_long_term_graph=long_term_graph,
        post_message_predictor_snapshot=predictor_snapshot,
        post_message_short_term_export=list(short_term_export or []),
    )


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


def test_runtime_session_seed_rejected_in_benchmark_mode() -> None:
    from agintor.schemas import RuntimeSessionSeed, RuntimeSolveRequest, BenchmarkTask

    seed = RuntimeSessionSeed(session_id="sess.1", message_index=1)
    task = BenchmarkTask(
        task_id="demo.task",
        family="top",
        prompt="hello",
        task_type="structured_ops",
        operations=[],
        expected={},
    )
    with pytest.raises(Exception):
        RuntimeSolveRequest(
            request_id="req.1",
            runtime_backend="local",
            mode="benchmark",
            seed=0,
            task=task,
            session_seed=seed,
        )


def test_runtime_session_seed_must_match_trace_identity() -> None:
    from agintor.runtime_api import load_solve_request
    from agintor.schemas import OpenAITraceContext, RuntimeSessionSeed, RuntimeSolveRequest

    seed = RuntimeSessionSeed(session_id="sess.source", message_index=2)
    with pytest.raises(Exception, match="runtime_session_id"):
        RuntimeSolveRequest(
            request_id="user.req",
            runtime_backend="local",
            mode="user_request",
            seed=0,
            solve_request=load_solve_request(prompt="hello"),
            session_seed=seed,
            trace_context=OpenAITraceContext(
                runtime_session_id="sess.other",
                runtime_message_index=2,
            ),
        )
    with pytest.raises(Exception, match="runtime_message_index"):
        RuntimeSolveRequest(
            request_id="user.req",
            runtime_backend="local",
            mode="user_request",
            seed=0,
            solve_request=load_solve_request(prompt="hello"),
            session_seed=seed,
            trace_context=OpenAITraceContext(
                runtime_session_id="sess.source",
                runtime_message_index=1,
            ),
        )


def test_host_solve_returns_post_message_state_for_user_request(tmp_path: Path) -> None:
    from agintor.project import init_runtime
    from agintor.providers import ReplayProvider
    from agintor.runtime_api import load_solve_request, runtime_solve_request_for_user_request
    from agintor.runtime_host import RuntimeHost

    runtime_dir = init_runtime(tmp_path / "runtime")
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    request = runtime_solve_request_for_user_request(
        runtime_backend="local",
        seed=0,
        solve_request=load_solve_request(prompt="Say hello."),
        runtime_session_id="sess.alpha",
        runtime_message_id="msg.0001",
        runtime_message_index=0,
    )
    response = host.solve(
        runtime_dir,
        request,
        provider=ReplayProvider([{"text": "hello", "model_name": "replay/small"}]),
    )
    result = response.solve_result
    assert result.mode == "user_request"
    assert result.post_message_long_term_graph is not None
    assert result.post_message_predictor_snapshot is not None


def test_host_solve_for_benchmark_does_not_return_post_message_state(tmp_path: Path) -> None:
    from agintor.project import init_runtime
    from agintor.providers import ReplayProvider
    from agintor.runtime_api import runtime_solve_request_for_task
    from agintor.runtime_host import RuntimeHost
    from agintor.schemas import BenchmarkTask

    runtime_dir = init_runtime(tmp_path / "runtime")
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    task = BenchmarkTask(
        task_id="demo.benchmark",
        family="top",
        prompt="say hi",
        task_type="structured_ops",
        operations=[],
        expected=None,
        verifier_type="none",
        verification_required=False,
        allow_best_effort=True,
    )
    request = runtime_solve_request_for_task(runtime_backend="local", seed=0, task=task)
    response = host.solve(
        runtime_dir,
        request,
        provider=ReplayProvider([{"text": "hello", "model_name": "replay/small"}]),
    )
    result = response.solve_result
    assert result.mode == "benchmark"
    assert result.post_message_long_term_graph is None
    assert result.post_message_predictor_snapshot is None
    assert result.post_message_short_term_export == []


def test_host_solve_session_seed_seeds_long_term_memory(tmp_path: Path) -> None:
    """Re-running a user_request with a session_seed populated from the prior turn
    should hydrate the long-term graph so the runtime starts the next message with
    the prior message's persistent memory."""

    from agintor.project import init_runtime
    from agintor.providers import ReplayProvider
    from agintor.runtime_api import load_solve_request, runtime_solve_request_for_user_request
    from agintor.runtime_host import RuntimeHost
    from agintor.schemas import RuntimeSessionSeed

    runtime_dir = init_runtime(tmp_path / "runtime")
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    first = host.solve(
        runtime_dir,
        runtime_solve_request_for_user_request(
            runtime_backend="local",
            seed=0,
            solve_request=load_solve_request(prompt="Remember the launch keyword: rosebud."),
            runtime_session_id="sess.beta",
            runtime_message_id="msg.0001",
            runtime_message_index=0,
        ),
        provider=ReplayProvider([{"text": "remembered", "model_name": "replay/small"}]),
    )
    long_term_graph = first.solve_result.post_message_long_term_graph
    predictor_snapshot = first.solve_result.post_message_predictor_snapshot
    assert long_term_graph is not None

    seed = RuntimeSessionSeed(
        session_id="sess.beta",
        message_index=1,
        parent_message_id="msg.0001",
        long_term_graph=long_term_graph,
        predictor_snapshot=predictor_snapshot,
        short_term_carryover=[
            {"event": "model_response", "summary": "first turn"},
        ],
    )
    second = host.solve(
        Path(runtime_dir),
        runtime_solve_request_for_user_request(
            runtime_backend="local",
            seed=0,
            solve_request=load_solve_request(prompt="What was the launch keyword?"),
            runtime_session_id="sess.beta",
            runtime_message_id="msg.0002",
            runtime_message_index=1,
            session_seed=seed,
        ),
        provider=ReplayProvider([{"text": "rosebud", "model_name": "replay/small"}]),
    )
    assert second.solve_result.mode == "user_request"
    assert second.solve_result.post_message_long_term_graph is not None


def test_session_seed_short_term_carryover_reaches_direct_response_prompt() -> None:
    from agintor.schemas import BenchmarkTask
    from agintor.task_runtime.operations import OperationsMixin

    task = BenchmarkTask(
        task_id="user.req.direct",
        family="e2e",
        prompt="What was the launch keyword?",
        task_type="direct_answer",
        operations=[],
        expected=None,
    )
    context = SimpleNamespace(
        task=task,
        shell=SimpleNamespace(
            message_board=SimpleNamespace(
                entries=[
                    {
                        "kind": "session_carryover",
                        "payload": {
                            "kind": "assistant_summary",
                            "content": "The launch keyword was rosebud.",
                        },
                    }
                ]
            )
        ),
    )

    prompt = OperationsMixin()._direct_response_prompt(context, {})

    assert "Session carryover:" in prompt
    assert "rosebud" in prompt


def test_session_seed_short_term_carryover_reaches_repo_patch_prompt(tmp_path: Path) -> None:
    from agintor.schemas import BenchmarkTask
    from agintor.task_runtime.bounded_io import BoundedIOMixin
    from agintor.task_runtime.operations import OperationsMixin

    class RuntimeHarness(OperationsMixin, BoundedIOMixin):
        def __init__(self, workspace: Path) -> None:
            self.runtime = SimpleNamespace(
                deployment_contract=SimpleNamespace(
                    filesystem_policy="read-only",
                    runtime_isolation_policy=SimpleNamespace(workspace_root=str(workspace)),
                )
            )

    target_file = tmp_path / "answer.txt"
    target_file.write_text("before", encoding="utf-8")
    captured: dict[str, str] = {}

    def run_model_request(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        return SimpleNamespace(
            text=json.dumps(
                {
                    "summary": "ok",
                    "files": [
                        {
                            "path": str(target_file),
                            "updated_content": "after",
                        }
                    ],
                }
            )
        )

    context = SimpleNamespace(
        request_id="solve.user.seed_0",
        runtime_backend="local",
        plan=SimpleNamespace(plan_id="plan.1"),
        active_frame=SimpleNamespace(frame_id="frame.root", worker_id=None),
        state=SimpleNamespace(side_effect_receipts=[]),
        task=BenchmarkTask(
            task_id="user.req.patch",
            family="e2e",
            prompt="Patch the answer file.",
            task_type="bounded_repo_patch",
            file_paths=[str(target_file)],
            operations=[],
            expected=None,
        ),
        shell=SimpleNamespace(
            workspace=tmp_path,
            message_board=SimpleNamespace(
                entries=[
                    {
                        "kind": "session_carryover",
                        "payload": {
                            "kind": "assistant_summary",
                            "content": "The launch keyword was rosebud.",
                        },
                    }
                ]
            ),
        ),
        run_model_request=run_model_request,
        record=lambda *args, **kwargs: None,
        record_side_effect=lambda receipt: context.state.side_effect_receipts.append(receipt.model_dump()),
        publish_checkpoint_boundary=lambda *args, **kwargs: None,
        raise_if_cancelled=lambda: None,
    )

    output = RuntimeHarness(tmp_path)._execute_repo_patch_node(
        context,
        SimpleNamespace(node_id="patch.1"),
        {
            "target_file_paths": [str(target_file)],
            "file_snapshots": [
                {
                    "path": str(target_file),
                    "content": "before",
                    "exists": True,
                }
            ],
        },
        "default",
        None,
    )

    assert output["applied"] is False
    assert "Session carryover:" in captured["prompt"]
    assert "rosebud" in captured["prompt"]


def test_solve_cmd_records_failed_runtime_chat_turn_and_uses_effective_profile_hash(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agintor import cli
    from agintor.artifacts import ArtifactMode

    runtime_dir = _runtime_dir(tmp_path)
    workspace_dir = tmp_path / "workspace"
    profile = SimpleNamespace(marker="effective-profile")
    released = {}

    class FakeLease:
        path = workspace_dir

        def release(self, *, failed: bool) -> None:
            released["failed"] = failed

    class FailingHost:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def solve(self, *args, **kwargs):
            raise RuntimeError("runtime exploded")

    def fake_load_runtime(runtime_path, *, runtime_profile=None, runtime_backend=None, **kwargs):
        assert runtime_profile is profile
        assert runtime_backend == "local"
        return SimpleNamespace(runtime_hash="runtime.hash.effective")

    monkeypatch.setattr(cli, "load_runtime_profile", lambda *args, **kwargs: profile)
    monkeypatch.setattr(cli, "_build_provider", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "_resolve_workspace", lambda *args, **kwargs: FakeLease())
    monkeypatch.setattr(cli, "load_runtime", fake_load_runtime)
    monkeypatch.setattr(cli, "RuntimeHost", FailingHost)

    with pytest.raises(RuntimeError, match="runtime exploded"):
        cli.solve_cmd(
            runtime_dir=str(runtime_dir),
            task_id=None,
            suite="demo",
            partition="train",
            prompt="hello",
            prompt_file=None,
            seed=0,
            provider="local",
            api_key_file=None,
            profile=None,
            workspace=str(workspace_dir),
            artifact_mode=ArtifactMode.NONE,
            runtime_backend="local",
            session=None,
            new_session=False,
        )

    store = RuntimeSessionStore(runtime_dir)
    sessions = store.list_sessions()
    assert len(sessions) == 1
    identity = store.load_session(sessions[0], runtime_hash="runtime.hash.effective")
    assert identity.runtime_hash == "runtime.hash.effective"
    messages = store.messages(identity.session_id)
    assert len(messages) == 1
    assert messages[0].lifecycle_state == "failed"
    assert messages[0].boundary_state_path is None
    assert store.seed_for_next_message(identity.session_id) is None
    assert released["failed"] is True


def test_solve_cmd_rejects_continuing_session_with_different_backend(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agintor import cli
    from agintor.artifacts import ArtifactMode

    runtime_dir = _runtime_dir(tmp_path)
    workspace_dir = tmp_path / "workspace"
    profile = SimpleNamespace(marker="effective-profile")
    existing = RuntimeSessionStore(runtime_dir).create_session(
        runtime_hash="runtime.hash.effective",
        runtime_backend="local",
    )

    class FakeLease:
        path = workspace_dir

        def release(self, *, failed: bool) -> None:
            pass

    class UnusedHost:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def solve(self, *args, **kwargs):
            raise AssertionError("host.solve should not run after backend pin mismatch")

    monkeypatch.setattr(cli, "load_runtime_profile", lambda *args, **kwargs: profile)
    monkeypatch.setattr(cli, "_build_provider", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "_resolve_workspace", lambda *args, **kwargs: FakeLease())
    monkeypatch.setattr(
        cli,
        "load_runtime",
        lambda *args, **kwargs: SimpleNamespace(runtime_hash="runtime.hash.effective"),
    )
    monkeypatch.setattr(cli, "RuntimeHost", UnusedHost)

    with pytest.raises(RuntimeSessionMismatchError, match="runtime backend"):
        cli.solve_cmd(
            runtime_dir=str(runtime_dir),
            task_id=None,
            suite="demo",
            partition="train",
            prompt="hello",
            prompt_file=None,
            seed=0,
            provider="local",
            api_key_file=None,
            profile=None,
            workspace=str(workspace_dir),
            artifact_mode=ArtifactMode.NONE,
            runtime_backend="docker",
            session=existing.session_id,
            new_session=False,
        )


def test_solve_cmd_persists_host_rewritten_docker_carryover_for_next_turn(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agintor import cli
    from agintor.artifacts import ArtifactMode
    from agintor.container_runtime import DockerRuntimeExecutor

    runtime_dir = _runtime_dir(tmp_path)
    workspace_dir = tmp_path / "workspace"
    docker_workspace = workspace_dir / "docker-workspace"
    docker_workspace.mkdir(parents=True)
    host_file = (tmp_path / "Host Files" / "report.json").resolve()
    host_file.parent.mkdir(parents=True)
    host_file.write_text("{}", encoding="utf-8")
    container_path = "/mnt/request-files/abc123/report.json"
    profile = SimpleNamespace(marker="effective-profile")
    released: dict[str, bool] = {}
    captured: dict[str, object] = {}

    class FakeLease:
        path = workspace_dir

        def release(self, *, failed: bool) -> None:
            released["failed"] = failed

    class DockerishHost:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def solve(self, runtime_path, request, **kwargs):
            captured["request"] = request
            response = RuntimeSolveResponse(
                request_id=request.request_id,
                capability_exchange=CapabilityExchange(
                    runtime_contract_version=RUNTIME_CONTRACT_VERSION,
                    supported_backends=["docker"],
                    runtime_asset_capabilities={"runtime_sdk": True},
                    resume_support=True,
                ),
                solve_result=SolveResult(
                    request_id=request.request_id,
                    runtime_hash="runtime.hash.effective",
                    run_lifecycle_state="completed",
                    mode="user_request",
                    artifact={
                        "path": container_path,
                        "summary": f"plain text mention {container_path}",
                    },
                    status="completed",
                    verification_status="best_effort",
                    summary="ok",
                    post_message_long_term_graph=LongTermGraphSnapshot(),
                    post_message_short_term_export=[
                        {
                            "kind": "post_message_export",
                            "path": container_path,
                            "content": {
                                "artifact_ref": container_path,
                                "summary": f"plain text mention {container_path}",
                            },
                        }
                    ],
                ),
            )
            DockerRuntimeExecutor(tmp_path / "executor")._rewrite_solve_response_paths(
                response,
                docker_workspace,
                request_file_reverse_map={container_path: str(host_file)},
            )
            return response

    monkeypatch.setattr(cli, "load_runtime_profile", lambda *args, **kwargs: profile)
    monkeypatch.setattr(cli, "_build_provider", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "_resolve_workspace", lambda *args, **kwargs: FakeLease())
    monkeypatch.setattr(
        cli,
        "load_runtime",
        lambda *args, **kwargs: SimpleNamespace(runtime_hash="runtime.hash.effective"),
    )
    monkeypatch.setattr(cli, "RuntimeHost", DockerishHost)

    cli.solve_cmd(
        runtime_dir=str(runtime_dir),
        task_id=None,
        suite="demo",
        partition="train",
        prompt="summarize the attached report",
        prompt_file=None,
        seed=0,
        provider="local",
        api_key_file=None,
        profile=None,
        workspace=str(workspace_dir),
        artifact_mode=ArtifactMode.NONE,
        runtime_backend="docker",
        session=None,
        new_session=False,
    )

    request = captured["request"]
    assert request.runtime_backend == "docker"
    assert request.trace_context.runtime_session_id
    assert request.trace_context.runtime_message_id
    assert request.trace_context.runtime_message_index == 0
    store = RuntimeSessionStore(runtime_dir)
    identity = store.load_session(
        store.list_sessions()[0],
        runtime_hash="runtime.hash.effective",
        runtime_backend="docker",
    )
    seed = store.seed_for_next_message(identity.session_id)
    assert seed is not None
    assert seed.short_term_carryover == [
        {
            "kind": "post_message_export",
            "path": str(host_file),
            "content": {
                "artifact_ref": str(host_file),
                "summary": f"plain text mention {container_path}",
            },
        }
    ]
    assert released["failed"] is False


def test_solve_cmd_failed_turn_ignores_stale_paused_run_for_same_prompt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agintor import cli
    from agintor.artifacts import ArtifactMode
    from agintor.run_store import RunStore

    runtime_dir = _runtime_dir(tmp_path)
    workspace_dir = tmp_path / "workspace"
    profile = SimpleNamespace(marker="effective-profile")

    class FakeLease:
        path = workspace_dir

        def release(self, *, failed: bool) -> None:
            pass

    class FailingHost:
        def __init__(self, workspace, *args, **kwargs) -> None:
            self.run_store = RunStore(workspace)

        def solve(self, runtime_path, request, **kwargs):
            stale_manifest = self.run_store.create_run(
                request_id=request.request_id,
                evaluation_unit_id=request.evaluation_unit_id,
                request_mode=request.mode,
                runtime_backend=request.runtime_backend,
                trace_context={
                    "runtime_session_id": "sess.previous",
                    "runtime_message_id": "msg.previous",
                    "runtime_message_index": 0,
                },
            )
            checkpoint_ref = str(Path(stale_manifest.run_root) / "checkpoints" / "checkpoint.stale.json")
            Path(checkpoint_ref).write_text("{}", encoding="utf-8")
            self.run_store.finish_run(
                stale_manifest,
                lifecycle_state="paused",
                latest_checkpoint_ref=checkpoint_ref,
                resumable=True,
                failure_kind="runtime_crash",
            )
            raise RuntimeError("runtime crashed before current checkpoint")

    monkeypatch.setattr(cli, "load_runtime_profile", lambda *args, **kwargs: profile)
    monkeypatch.setattr(cli, "_build_provider", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "_resolve_workspace", lambda *args, **kwargs: FakeLease())
    monkeypatch.setattr(
        cli,
        "load_runtime",
        lambda *args, **kwargs: SimpleNamespace(runtime_hash="runtime.hash.effective"),
    )
    monkeypatch.setattr(cli, "RuntimeHost", FailingHost)

    with pytest.raises(RuntimeError, match="runtime crashed"):
        cli.solve_cmd(
            runtime_dir=str(runtime_dir),
            task_id=None,
            suite="demo",
            partition="train",
            prompt="hello",
            prompt_file=None,
            seed=0,
            provider="local",
            api_key_file=None,
            profile=None,
            workspace=str(workspace_dir),
            artifact_mode=ArtifactMode.NONE,
            runtime_backend="local",
            session=None,
            new_session=False,
        )

    store = RuntimeSessionStore(runtime_dir)
    identity = store.load_session(store.list_sessions()[0], runtime_hash="runtime.hash.effective")
    message = store.messages(identity.session_id)[0]
    assert message.lifecycle_state == "failed"
    assert message.checkpoint_ref is None


def test_solve_cmd_records_paused_checkpoint_when_host_fails_after_checkpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agintor import cli
    from agintor.artifacts import ArtifactMode
    from agintor.run_store import RunStore

    runtime_dir = _runtime_dir(tmp_path)
    workspace_dir = tmp_path / "workspace"
    profile = SimpleNamespace(marker="effective-profile")

    class FakeLease:
        path = workspace_dir

        def release(self, *, failed: bool) -> None:
            pass

    class PausedHost:
        def __init__(self, workspace, *args, **kwargs) -> None:
            self.run_store = RunStore(workspace)

        def solve(self, runtime_path, request, **kwargs):
            manifest = self.run_store.create_run(
                request_id=request.request_id,
                evaluation_unit_id=request.evaluation_unit_id,
                request_mode=request.mode,
                runtime_backend=request.runtime_backend,
                trace_context=request.trace_context.model_dump() if request.trace_context is not None else None,
            )
            checkpoint_ref = str(Path(manifest.run_root) / "checkpoints" / "checkpoint.json")
            Path(checkpoint_ref).write_text("{}", encoding="utf-8")
            self.run_store.finish_run(
                manifest,
                lifecycle_state="paused",
                latest_checkpoint_ref=checkpoint_ref,
                resumable=True,
                failure_kind="runtime_crash",
            )
            raise RuntimeError("runtime crashed after checkpoint")

    monkeypatch.setattr(cli, "load_runtime_profile", lambda *args, **kwargs: profile)
    monkeypatch.setattr(cli, "_build_provider", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "_resolve_workspace", lambda *args, **kwargs: FakeLease())
    monkeypatch.setattr(
        cli,
        "load_runtime",
        lambda *args, **kwargs: SimpleNamespace(runtime_hash="runtime.hash.effective"),
    )
    monkeypatch.setattr(cli, "RuntimeHost", PausedHost)

    with pytest.raises(RuntimeError, match="runtime crashed"):
        cli.solve_cmd(
            runtime_dir=str(runtime_dir),
            task_id=None,
            suite="demo",
            partition="train",
            prompt="hello",
            prompt_file=None,
            seed=0,
            provider="local",
            api_key_file=None,
            profile=None,
            workspace=str(workspace_dir),
            artifact_mode=ArtifactMode.NONE,
            runtime_backend="local",
            session=None,
            new_session=False,
        )

    store = RuntimeSessionStore(runtime_dir)
    identity = store.load_session(store.list_sessions()[0], runtime_hash="runtime.hash.effective")
    message = store.messages(identity.session_id)[0]
    assert message.lifecycle_state == "paused"
    assert message.checkpoint_ref
