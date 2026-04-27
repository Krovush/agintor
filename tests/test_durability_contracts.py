from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError
import json
from pathlib import Path
import sqlite3
from threading import Event
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from agintor import openai_trace, state_store
from agintor.openai_trace import load_materialization_state, persist_openai_trace
from agintor.run_store import RunStore
from agintor.schemas import (
    CHECKPOINT_ENVELOPE_SCHEMA_VERSION,
    CheckpointEnvelope,
    MemoryNode,
    OpenAITraceContext,
    RecoveryAttempt,
    TraceCursorSnapshot,
    WorkingMemorySnapshot,
)
from agintor.shell import FixedShell
from agintor.task_runtime.memory import MemoryMixin
from agintor.versioning import RUNTIME_CONTRACT_VERSION


def _memory_node(node_id: str, content: str, *, source_task_id: str = "task.1") -> MemoryNode:
    return MemoryNode(
        node_id=node_id,
        type="TaskNote",
        label=node_id,
        content=content,
        embedding=[],
        symbol_set=[node_id],
        file_paths=[],
        source_task_id=source_task_id,
        verifier_support=0.8,
        timestamps={},
        provenance={
            "supporting_receipt_ids": ["receipt.1"],
            "supporting_verifier_ids": ["verifier.1"],
        },
    )


def test_checkpoint_envelope_uses_v4_and_rejects_legacy_payloads() -> None:
    envelope = CheckpointEnvelope(
        checkpoint_id="checkpoint.schema.0001",
        runtime_contract_version=RUNTIME_CONTRACT_VERSION,
        runtime_hash="runtime.hash",
        request_id="request.1",
        plan_id="plan.1",
        task_id="task.1",
        seed=0,
    )
    payload = envelope.model_dump()

    assert payload["checkpoint_schema_version"] == CHECKPOINT_ENVELOPE_SCHEMA_VERSION

    payload["checkpoint_schema_version"] = "agintor.checkpoint-envelope.v3"
    with pytest.raises(ValidationError, match="unsupported checkpoint envelope schema"):
        CheckpointEnvelope.model_validate(payload)

    missing_schema_payload = envelope.model_dump()
    missing_schema_payload.pop("checkpoint_schema_version")
    assert CheckpointEnvelope.model_validate(missing_schema_payload).checkpoint_schema_version == CHECKPOINT_ENVELOPE_SCHEMA_VERSION
    with pytest.raises(ValueError, match="persisted checkpoint envelopes must include"):
        CheckpointEnvelope.model_validate_persisted(missing_schema_payload)

    legacy_payload = envelope.model_dump()
    legacy_payload["working_state_summary"] = {}
    with pytest.raises(ValidationError, match="working_state_summary"):
        CheckpointEnvelope.model_validate(legacy_payload)


def test_checkpoint_owned_typed_snapshots_and_recovery_contract_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        WorkingMemorySnapshot.model_validate({"captured_at": 1.0, "extra": "forbidden"})
    with pytest.raises(ValidationError):
        TraceCursorSnapshot.model_validate({"runtime_trace_length": 0, "captured_at": 1.0, "extra": "forbidden"})

    recovery = RecoveryAttempt(
        recovery_attempt_id="recovery.1",
        run_id="run.1",
        attempt_id="attempt_0001",
        selected_checkpoint_ref="checkpoint.json",
        reconciliation_policy="strict",
        compatibility_result="exact_compatible",
        current_fingerprint_id="fingerprint.1",
        resume_explanation="restored",
    )
    assert recovery.compatibility_result == "exact_compatible"

    payload = recovery.model_dump()
    payload["restore_state"] = "restored"
    with pytest.raises(ValidationError):
        RecoveryAttempt.model_validate(payload)


def test_checkpoint_file_load_rejects_missing_schema_version(tmp_path: Path) -> None:
    shell = FixedShell(tmp_path / "workspace")
    envelope = CheckpointEnvelope(
        checkpoint_id="checkpoint.persisted-schema.0001",
        runtime_contract_version=RUNTIME_CONTRACT_VERSION,
        runtime_hash="runtime.hash",
        request_id="request.1",
        plan_id="plan.1",
        task_id="task.1",
        seed=0,
    )
    checkpoint_ref = shell.save_checkpoint_envelope(envelope).ref
    checkpoint_path = Path(checkpoint_ref)
    payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    payload.pop("checkpoint_schema_version")
    checkpoint_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    with pytest.raises(ValueError, match="persisted checkpoint envelopes must include"):
        shell.load_checkpoint_envelope(checkpoint_ref=checkpoint_ref)


def test_trace_materialization_skips_records_without_runtime_identity(tmp_path: Path, monkeypatch) -> None:
    trace_root = tmp_path / "openai_traces"
    monkeypatch.setenv("AGINTOR_OPENAI_TRACE_DIR", str(trace_root))

    first_call_id = persist_openai_trace(
        provider="openai",
        method_name="responses.create",
        model_class="default",
        model_name="gpt-test",
        reasoning_effort=None,
        instructions="plan",
        input_value="hello",
        request_payload={"model": "gpt-test"},
        request_metadata={"mode": "planning"},
        response_text="ok",
    )
    assert first_call_id
    session_dirs = list((trace_root / "sessions").iterdir())
    assert len(session_dirs) == 1
    session_dir = session_dirs[0]
    state = load_materialization_state(session_dir)
    assert state is not None
    assert state.call_count == 1
    assert state.grouped_call_count == 0
    assert state.runtime_task_keys == []
    assert state.known_call_ids == [first_call_id]
    assert state.last_finalized_call_id == first_call_id
    assert state.materialized_runtime_task_keys == []
    assert state.pending_runtime_task_keys == []
    assert state.errors == []
    assert (session_dir / "INDEX.md").exists()
    assert (session_dir / "TRANSCRIPT.md").exists()
    assert not (session_dir / "builds").exists()
    assert not (session_dir / "solves").exists()
    assert not (session_dir / "runtime_tasks").exists()
    raw_record = json.loads(next((session_dir / "calls").glob("*.json")).read_text(encoding="utf-8"))
    assert "runtime_task_key" not in raw_record
    assert raw_record["trace_context"]["session_id"] == session_dir.name
    assert raw_record["request_metadata"]["trace_context"]["session_id"] == session_dir.name

    trace_context = OpenAITraceContext(
        session_id=session_dir.name,
        request_id="request.1",
        evaluation_unit_id="evaluation.1",
        task_id="task.1",
        seed=0,
        episode_kind="single_task",
    )
    second_call_id = persist_openai_trace(
        provider="openai",
        method_name="responses.create",
        model_class="default",
        model_name="gpt-test",
        reasoning_effort=None,
        instructions="solve",
        input_value="hello",
        request_payload={"model": "gpt-test"},
        request_metadata={"mode": "user_request", "trace_context": (trace_context).model_dump()},
        response_text="ok",
    )
    assert second_call_id
    state = load_materialization_state(session_dir)
    assert state is not None
    assert state.call_count == 2
    assert state.grouped_call_count == 0
    assert state.runtime_task_keys == []
    assert state.materialized_solve_request_ids == ["request.1"]
    assert state.pending_solve_request_ids == []
    assert (session_dir / "solves" / "request.1" / "INDEX.md").exists()
    assert (session_dir / "solves" / "request.1" / "TRANSCRIPT.md").exists()
    assert not (session_dir / "runtime_tasks").exists()

    complete_trace_context = trace_context.model_copy(update={"runtime_hash": "runtime.hash.1"})
    third_call_id = persist_openai_trace(
        provider="openai",
        method_name="responses.create",
        model_class="default",
        model_name="gpt-test",
        reasoning_effort=None,
        instructions="solve",
        input_value="hello again",
        request_payload={"model": "gpt-test"},
        request_metadata={"mode": "user_request", "trace_context": (complete_trace_context).model_dump()},
        response_text="ok",
    )
    assert third_call_id
    state = load_materialization_state(session_dir)
    assert state is not None
    assert state.schema_version == "agintor.trace-materialization.v1"
    assert state.call_count == 3
    assert state.grouped_call_count == 1
    assert state.known_call_ids == [first_call_id, second_call_id, third_call_id]
    assert state.last_finalized_call_id == third_call_id
    assert state.runtime_task_keys == ["task.1|seed_0|runtime.hash.1|evaluation.1"]
    assert state.materialized_runtime_task_keys == state.runtime_task_keys
    assert state.materialized_solve_request_ids == ["request.1"]
    assert state.pending_build_ids == []
    assert state.pending_solve_request_ids == []
    assert state.pending_runtime_task_keys == []
    assert state.errors == []
    runtime_view = (
        session_dir
        / "runtime_tasks"
        / "task.1"
        / "seed_0"
        / "runtimes"
        / "runtime.hash.1"
        / "requests"
        / "evaluation.1"
    )
    assert (runtime_view / "INDEX.md").exists()
    assert (runtime_view / "TRANSCRIPT.md").exists()
    assert "hello again" in (runtime_view / "TRANSCRIPT.md").read_text(encoding="utf-8")

    from agintor.openai_trace import rebuild_trace_materialization

    rebuilt_state = rebuild_trace_materialization(session_dir)
    assert rebuilt_state.known_call_ids == state.known_call_ids
    assert rebuilt_state.materialized_solve_request_ids == state.materialized_solve_request_ids
    assert rebuilt_state.materialized_runtime_task_keys == state.materialized_runtime_task_keys


def test_concurrent_trace_persistence_rebuilds_grouped_views_without_losing_calls(
    tmp_path: Path,
    monkeypatch,
) -> None:
    trace_root = tmp_path / "openai_traces"
    monkeypatch.setenv("AGINTOR_OPENAI_TRACE_DIR", str(trace_root))
    monkeypatch.setenv("AGINTOR_OPENAI_TRACE_SESSION_ID", "session.concurrent")
    stale_load_started = Event()
    allow_stale_rebuild = Event()
    stale_load_armed = {"value": False}
    original_load = openai_trace._load_trace_records

    def coordinated_load(session_dir):
        records = original_load(session_dir)
        if len(records) == 1 and not stale_load_armed["value"]:
            stale_load_armed["value"] = True
            stale_load_started.set()
            allow_stale_rebuild.wait(timeout=2.0)
        return records

    monkeypatch.setattr(openai_trace, "_load_trace_records", coordinated_load)

    def write_call(label: str) -> str | None:
        trace_context = OpenAITraceContext(
            session_id="session.concurrent",
            request_id=f"request.{label}",
            evaluation_unit_id="evaluation.concurrent",
            task_id="task.concurrent",
            seed=0,
            runtime_hash="runtime.concurrent",
            episode_kind="single_task",
        )
        return persist_openai_trace(
            provider="openai",
            method_name="responses.create",
            model_class="default",
            model_name="gpt-test",
            reasoning_effort=None,
            instructions=f"solve {label}",
            input_value=f"hello {label}",
            request_payload={"model": "gpt-test"},
            request_metadata={"mode": "user_request", "trace_context": (trace_context).model_dump()},
            response_text=f"ok {label}",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(write_call, "first")
        assert stale_load_started.wait(timeout=2.0)
        second_future = pool.submit(write_call, "second")
        try:
            second_call_id = second_future.result(timeout=0.5)
        except TimeoutError:
            allow_stale_rebuild.set()
            second_call_id = second_future.result(timeout=5.0)
        else:
            allow_stale_rebuild.set()
        first_call_id = first_future.result(timeout=5.0)

    assert first_call_id is not None
    assert second_call_id is not None
    session_dir = trace_root / "sessions" / "session.concurrent"
    state = load_materialization_state(session_dir)
    assert state is not None
    assert state.call_count == 2
    assert state.grouped_call_count == 2
    assert state.materialized_runtime_task_keys == [
        "task.concurrent|seed_0|runtime.concurrent|evaluation.concurrent"
    ]
    assert state.pending_runtime_task_keys == []
    runtime_view = (
        session_dir
        / "runtime_tasks"
        / "task.concurrent"
        / "seed_0"
        / "runtimes"
        / "runtime.concurrent"
        / "requests"
        / "evaluation.concurrent"
    )
    assert (runtime_view / "INDEX.md").exists()
    index_text = (runtime_view / "INDEX.md").read_text(encoding="utf-8")
    assert first_call_id in index_text
    assert second_call_id in index_text


def test_trace_materialization_separates_runtime_task_groups_by_runtime_hash(
    tmp_path: Path,
    monkeypatch,
) -> None:
    trace_root = tmp_path / "openai_traces"
    monkeypatch.setenv("AGINTOR_OPENAI_TRACE_DIR", str(trace_root))
    call_ids: list[str] = []

    for runtime_hash in ("runtime.alpha", "runtime.beta"):
        trace_context = OpenAITraceContext(
            session_id="session.runtime-hash",
            request_id="request.same",
            evaluation_unit_id="evaluation.same",
            task_id="task.same",
            seed=7,
            runtime_hash=runtime_hash,
            episode_kind="single_task",
        )
        call_id = persist_openai_trace(
            provider="openai",
            method_name="responses.create",
            model_class="default",
            model_name="gpt-test",
            reasoning_effort=None,
            instructions=f"solve {runtime_hash}",
            input_value="hello",
            request_payload={"model": "gpt-test"},
            request_metadata={"mode": "user_request", "trace_context": (trace_context).model_dump()},
            response_text="ok",
        )
        assert call_id is not None
        call_ids.append(call_id)

    session_dir = trace_root / "sessions" / "session.runtime-hash"
    state = load_materialization_state(session_dir)
    assert state is not None
    assert state.known_call_ids == call_ids
    assert state.last_finalized_call_id == call_ids[-1]
    assert state.materialized_solve_request_ids == ["request.same"]
    assert state.materialized_runtime_task_keys == [
        "task.same|seed_7|runtime.alpha|evaluation.same",
        "task.same|seed_7|runtime.beta|evaluation.same",
    ]
    alpha_view = (
        session_dir
        / "runtime_tasks"
        / "task.same"
        / "seed_7"
        / "runtimes"
        / "runtime.alpha"
        / "requests"
        / "evaluation.same"
    )
    beta_view = (
        session_dir
        / "runtime_tasks"
        / "task.same"
        / "seed_7"
        / "runtimes"
        / "runtime.beta"
        / "requests"
        / "evaluation.same"
    )
    assert call_ids[0] in (alpha_view / "INDEX.md").read_text(encoding="utf-8")
    assert call_ids[1] in (beta_view / "INDEX.md").read_text(encoding="utf-8")


def test_memory_promotion_uses_policy_hook_with_durable_write_scope(tmp_path: Path) -> None:
    shell = FixedShell(tmp_path, run_id="run.1", attempt_id="attempt.1")
    existing = _memory_node("existing", "old content")
    shell.long_term.upsert(existing)

    class RecordingMemoryPolicy:
        def __init__(self) -> None:
            self.upsert_calls: list[tuple[str, str | None]] = []

        def score_memory_unit(self, ctx, unit, existing_nodes) -> float:
            return 1.0

        def should_promote(self, ctx, unit, score) -> bool:
            return True

        def dedup_candidates(self, ctx, unit, existing_nodes) -> tuple[str, str | None]:
            return "merge", existing.node_id

        def upsert_memory(self, ctx, unit, action, target_id) -> None:
            self.upsert_calls.append((action, target_id))
            target = ctx.shell.long_term.nodes[target_id]
            merged = target.model_copy(
                update={
                    "content": unit.content,
                    "symbol_set": sorted(set(target.symbol_set) | set(unit.symbol_set)),
                    "verifier_support": max(target.verifier_support, unit.verifier_support),
                }
            )
            ctx.shell.long_term.upsert(merged)

    class RuntimeMemoryHarness(MemoryMixin):
        pass

    policy = RecordingMemoryPolicy()
    harness = RuntimeMemoryHarness()
    harness.runtime = SimpleNamespace(memory=policy)
    harness.shell = shell
    events: list[tuple[str, dict[str, object]]] = []
    context = SimpleNamespace(
        shell=shell,
        task=SimpleNamespace(task_id="task.1"),
        state=SimpleNamespace(latest_checkpoint_ref="checkpoint.1", promoted_nodes=0),
        record=lambda event, **payload: events.append((event, payload)),
    )

    harness._promote_memory_candidate(context, _memory_node("candidate", "new detailed content"))

    assert policy.upsert_calls == [("merge", existing.node_id)]
    assert context.state.promoted_nodes == 1
    assert events[-1][0] == "memory_promoted"
    assert shell.long_term.nodes[existing.node_id].content == "new detailed content"
    latest_write = shell.long_term.write_log[-1]
    assert latest_write.action == "merge"
    assert latest_write.target_node_id == existing.node_id
    assert latest_write.source_task_id == "task.1"
    assert latest_write.source_attempt_id == "attempt.1"
    assert latest_write.source_checkpoint_ref == "checkpoint.1"
    assert latest_write.verifier_support_refs == ["receipt.1", "verifier.1"]


def test_state_store_fails_closed_for_newer_sqlite_schema(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "store")
    manifest = store.create_run(
        request_id="state.schema",
        evaluation_unit_id="state.schema",
        request_mode="user_request",
        runtime_backend="local",
    )
    state_store.initialize(manifest.run_root)

    with sqlite3.connect(state_store.state_db_path(manifest.run_root)) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO state_store_metadata (key, value) VALUES ('schema_version', ?)",
            (str(state_store.STATE_STORE_SCHEMA_VERSION + 1),),
        )
        conn.commit()

    with pytest.raises(state_store.StateStoreError):
        state_store.initialize(manifest.run_root)


def test_state_store_initialize_persists_schema_version_without_followup_write(tmp_path: Path) -> None:
    run_root = tmp_path / "run"

    state_store.initialize(run_root)

    with sqlite3.connect(state_store.state_db_path(run_root)) as conn:
        row = conn.execute(
            "SELECT value FROM state_store_metadata WHERE key = 'schema_version'"
        ).fetchone()

    assert row is not None
    assert row[0] == str(state_store.STATE_STORE_SCHEMA_VERSION)


def test_state_store_rebuilds_dirty_index_from_canonical_checkpoint(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "store")
    manifest = store.create_run(
        request_id="state.rebuild",
        evaluation_unit_id="state.rebuild",
        request_mode="user_request",
        runtime_backend="local",
    )
    envelope = CheckpointEnvelope(
        checkpoint_id="checkpoint.state.rebuild.0001",
        runtime_contract_version=RUNTIME_CONTRACT_VERSION,
        runtime_hash="runtime-hash",
        run_id=manifest.run_id,
        run_root=manifest.run_root,
        attempt_id="attempt_0001",
        request_id=manifest.request_id,
        plan_id="plan.state.rebuild",
        task_id="task.state.rebuild",
        seed=0,
        sequence_no=1,
        resume_eligible=True,
    )
    store.write_checkpoint(envelope)

    with sqlite3.connect(state_store.state_db_path(manifest.run_root)) as conn:
        conn.execute("DELETE FROM checkpoints")
        conn.commit()
    state_store.mark_index_dirty(manifest.run_root, reason="test_deleted_checkpoint_index")

    indexed = state_store.open_state_store(manifest.run_root).latest_usable_checkpoint(run_id=manifest.run_id)

    assert indexed is not None
    assert indexed["checkpoint_id"] == envelope.checkpoint_id
    assert not (Path(manifest.run_root) / "state" / state_store.INDEX_DIRTY_FILE).exists()


def test_retrieval_signal_scores_are_indexed(tmp_path: Path) -> None:
    shell = FixedShell(tmp_path / "workspace", run_id="run.1", attempt_id="attempt.1")
    node = _memory_node("symbol.one", "durable retrieval content")
    shell.long_term.upsert(node)
    shell.long_term.retrieve_candidates(
        "durable retrieval content",
        exact_symbols=["symbol.one"],
        file_paths=[],
        task_id="task.1",
        seed=0,
        request_id="request.1",
    )
    store = RunStore(tmp_path / "store")
    manifest = store.create_run(
        request_id="request.1",
        evaluation_unit_id="request.1",
        request_mode="user_request",
        runtime_backend="local",
    )
    envelope = CheckpointEnvelope(
        checkpoint_id="checkpoint.retrieval.0001",
        runtime_contract_version=RUNTIME_CONTRACT_VERSION,
        runtime_hash="runtime-hash",
        run_id=manifest.run_id,
        run_root=manifest.run_root,
        attempt_id="attempt_0001",
        request_id=manifest.request_id,
        plan_id="plan.retrieval",
        task_id="task.1",
        seed=0,
        sequence_no=1,
        shell_state_snapshot=shell.snapshot_checkpoint_shell_state(checkpoint_id="checkpoint.retrieval.0001"),
    )
    store.write_checkpoint(envelope)

    with state_store.open_state_store(manifest.run_root)._connection() as conn:
        rows = conn.execute(
            "SELECT verifier_support_score, lexical_overlap_score, embedding_similarity_score, same_task_affinity_score "
            "FROM retrieval_signal_rows"
        ).fetchall()

    assert rows
    assert rows[0]["verifier_support_score"] == node.verifier_support
    assert rows[0]["same_task_affinity_score"] == 1.0
