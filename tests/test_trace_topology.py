from __future__ import annotations

import json
from pathlib import Path

import pytest

from agintor import openai_trace
from agintor.openai_trace import (
    TRACE_GROUP_BENCHMARK_TASK,
    TRACE_GROUP_FACTORY_MESSAGE,
    TRACE_GROUP_RUNTIME_SESSION_MESSAGE,
    benchmark_task_trace_key,
    factory_message_trace_key,
    load_materialization_state,
    persist_openai_trace,
    runtime_message_trace_key,
    trace_grouping_key,
)
from agintor.runtime_api import (
    compile_execution_plan_from_solve_request,
    load_solve_request,
    runtime_batch_request_for_tasks,
    runtime_solve_request_for_user_request,
)
from agintor.schemas import BenchmarkTask, OpenAITraceContext


def _persist(
    *,
    session_id: str,
    purpose: str,
    trace_context: OpenAITraceContext,
    body: str,
) -> str:
    call_id = persist_openai_trace(
        provider="openai",
        method_name="responses.create",
        model_class="default",
        model_name="gpt-test",
        reasoning_effort=None,
        instructions=body,
        input_value=body,
        request_payload={"model": "gpt-test"},
        request_metadata={"mode": purpose, "trace_context": (trace_context).model_dump()},
        response_text="ok",
    )
    assert call_id is not None
    return call_id


def test_trace_grouping_dispatches_factory_runtime_and_benchmark() -> None:
    factory_context = OpenAITraceContext(
        session_id="session.factory",
        factory_chat_id="chat.alpha",
        factory_message_id="fmsg.0001",
        factory_message_index=0,
    )
    grouping = trace_grouping_key(factory_context)
    assert grouping is not None
    assert grouping[0] == TRACE_GROUP_FACTORY_MESSAGE
    assert grouping[1] == factory_message_trace_key(
        factory_chat_id="chat.alpha",
        factory_message_id="fmsg.0001",
        factory_message_index=0,
    )

    runtime_context = OpenAITraceContext(
        session_id="session.runtime",
        runtime_hash="runtime.hash.alpha",
        runtime_session_id="sess.alpha",
        runtime_message_id="msg.0001",
        runtime_message_index=0,
    )
    grouping = trace_grouping_key(runtime_context)
    assert grouping is not None
    assert grouping[0] == TRACE_GROUP_RUNTIME_SESSION_MESSAGE
    assert grouping[1] == runtime_message_trace_key(
        runtime_hash="runtime.hash.alpha",
        runtime_session_id="sess.alpha",
        runtime_message_id="msg.0001",
        runtime_message_index=0,
    )

    benchmark_context = OpenAITraceContext(
        session_id="session.benchmark",
        request_id="benchmark.demo.task.seed_0",
        evaluation_unit_id="benchmark.demo.task.seed_0",
        request_mode="benchmark",
        task_id="demo.task",
        seed=0,
        runtime_hash="runtime.hash.alpha",
    )
    grouping = trace_grouping_key(benchmark_context)
    assert grouping is not None
    assert grouping[0] == TRACE_GROUP_BENCHMARK_TASK
    assert grouping[1] == benchmark_task_trace_key(
        request_id="benchmark.demo.task.seed_0",
        task_id="demo.task",
        seed=0,
        runtime_hash="runtime.hash.alpha",
        evaluation_unit_id="benchmark.demo.task.seed_0",
    )


def test_factory_parent_trace_context_flows_into_batch_invocations() -> None:
    task = BenchmarkTask(
        task_id="demo.task",
        family="top",
        prompt="answer",
        task_type="structured_ops",
        operations=[],
        expected={},
    )
    factory_context = OpenAITraceContext(
        session_id="session.factory",
        factory_chat_id="chat.alpha",
        factory_message_id="fmsg.0001",
        factory_message_index=0,
        build_id="build.alpha",
    )

    request = runtime_batch_request_for_tasks(
        request_id="batch.1",
        runtime_backend="local",
        task_runs=[(task, 0)],
        trace_context=factory_context,
    )

    invocation_context = request.invocations[0].trace_context
    assert invocation_context.factory_chat_id == "chat.alpha"
    assert invocation_context.factory_message_id == "fmsg.0001"
    assert invocation_context.task_id == "demo.task"
    invocation_context = invocation_context.model_copy(update={"runtime_hash": "runtime.hash.alpha"})
    grouping = trace_grouping_key(invocation_context)
    assert grouping is not None
    assert grouping[0] == TRACE_GROUP_BENCHMARK_TASK
    assert grouping[1] == benchmark_task_trace_key(
        request_id="benchmark.demo.task.seed_0",
        task_id="demo.task",
        seed=0,
        runtime_hash="runtime.hash.alpha",
        evaluation_unit_id="benchmark.demo.task.seed_0",
    )


def test_user_request_clears_parent_transfer_episode_identity() -> None:
    parent_context = OpenAITraceContext(
        session_id="session.transfer-parent",
        request_mode="benchmark",
        episode_kind="transfer_episode",
        episode_step_index=3,
    )
    solve_request = load_solve_request(prompt="Say hello.")
    runtime_request = runtime_solve_request_for_user_request(
        runtime_backend="local",
        seed=0,
        solve_request=solve_request,
        trace_context=parent_context,
    )

    assert runtime_request.trace_context.episode_kind is None
    assert runtime_request.trace_context.episode_step_index is None
    assert runtime_request.trace_context.request_mode == "user_request"

    _, plan = compile_execution_plan_from_solve_request(
        solve_request,
        seed=0,
        runtime_hash="runtime.hash",
        runtime_dir="runtime",
        trace_context=runtime_request.trace_context,
    )

    assert plan.trace_context.episode_kind is None
    assert plan.trace_context.episode_step_index is None
    assert plan.trace_context.request_mode == "user_request"


def test_non_transfer_batch_clears_parent_transfer_episode_identity() -> None:
    task = BenchmarkTask(
        task_id="demo.non-transfer",
        family="top",
        prompt="answer",
        task_type="structured_ops",
        operations=[],
        expected={},
    )
    parent_context = OpenAITraceContext(
        session_id="session.transfer-parent",
        request_mode="benchmark",
        episode_kind="transfer_episode",
        episode_step_index=9,
    )

    request = runtime_batch_request_for_tasks(
        request_id="batch.clear-parent",
        runtime_backend="local",
        task_runs=[(task, 0)],
        trace_context=parent_context,
    )

    assert request.trace_context.episode_kind is None
    assert request.trace_context.episode_step_index is None
    invocation = request.invocations[0]
    assert invocation.episode_kind is None
    assert invocation.episode_step_index is None
    assert invocation.trace_context.episode_kind is None
    assert invocation.trace_context.episode_step_index is None


def test_trace_grouping_returns_none_without_identity() -> None:
    bare_context = OpenAITraceContext(session_id="session.bare", request_id="req.1")
    assert trace_grouping_key(bare_context) is None


def test_factory_message_trace_view_directory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGINTOR_OPENAI_TRACE_DIR", str(tmp_path / "openai_traces"))
    trace_context = OpenAITraceContext(
        session_id="session.factory",
        factory_chat_id="chat.alpha",
        factory_message_id="fmsg.0001",
        factory_message_index=0,
        build_id="build.alpha",
    )
    call_id = _persist(
        session_id="session.factory",
        purpose="planning",
        trace_context=trace_context,
        body="plan something",
    )

    session_dir = tmp_path / "openai_traces" / "sessions" / "session.factory"
    state = load_materialization_state(session_dir)
    assert state is not None
    assert state.factory_message_keys
    assert state.runtime_session_message_keys == []
    assert state.benchmark_task_keys == []

    view_dir = session_dir / "factory_projects" / "chat.alpha" / "m0_fmsg.0001"
    assert (view_dir / "INDEX.md").exists()
    assert call_id in (view_dir / "INDEX.md").read_text(encoding="utf-8")
    assert not (session_dir / "runtime_sessions").exists()
    assert not (session_dir / "benchmark_tasks").exists()


def test_runtime_session_message_trace_view_directory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGINTOR_OPENAI_TRACE_DIR", str(tmp_path / "openai_traces"))
    trace_context = OpenAITraceContext(
        session_id="session.runtime",
        runtime_hash="runtime.alpha",
        runtime_session_id="sess.alpha",
        runtime_message_id="msg.0001",
        runtime_message_index=0,
    )
    call_id = _persist(
        session_id="session.runtime",
        purpose="user_request",
        trace_context=trace_context,
        body="hello",
    )

    session_dir = tmp_path / "openai_traces" / "sessions" / "session.runtime"
    state = load_materialization_state(session_dir)
    assert state is not None
    assert state.runtime_session_message_keys
    assert state.factory_message_keys == []
    assert state.benchmark_task_keys == []

    view_dir = (
        session_dir
        / "runtime_sessions"
        / "runtime.alpha"
        / "sess.alpha"
        / "m0_msg.0001"
    )
    assert (view_dir / "INDEX.md").exists()
    assert call_id in (view_dir / "INDEX.md").read_text(encoding="utf-8")


def test_benchmark_single_task_trace_view_directory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGINTOR_OPENAI_TRACE_DIR", str(tmp_path / "openai_traces"))
    trace_context = OpenAITraceContext(
        session_id="session.bench",
        request_id="benchmark.demo.task.seed_0",
        evaluation_unit_id="benchmark.demo.task.seed_0",
        request_mode="benchmark",
        task_id="demo.task",
        seed=0,
        runtime_hash="runtime.alpha",
    )
    call_id = _persist(
        session_id="session.bench",
        purpose="user_request",
        trace_context=trace_context,
        body="solve task",
    )

    session_dir = tmp_path / "openai_traces" / "sessions" / "session.bench"
    state = load_materialization_state(session_dir)
    assert state is not None
    assert state.benchmark_task_keys
    assert state.factory_message_keys == []
    assert state.runtime_session_message_keys == []

    view_dir = (
        session_dir
        / "benchmark_tasks"
        / "demo.task"
        / "seed_0"
        / "runtime.alpha"
        / "benchmark.demo.task.seed_0"
    )
    assert (view_dir / "INDEX.md").exists()
    assert call_id in (view_dir / "INDEX.md").read_text(encoding="utf-8")


def test_persisted_records_no_longer_carry_user_request_episode_kind(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGINTOR_OPENAI_TRACE_DIR", str(tmp_path / "openai_traces"))
    trace_context = OpenAITraceContext(
        session_id="session.user-request",
        runtime_hash="runtime.alpha",
        runtime_session_id="sess.alpha",
        runtime_message_id="msg.0001",
        runtime_message_index=0,
    )
    _persist(
        session_id="session.user-request",
        purpose="user_request",
        trace_context=trace_context,
        body="hello",
    )

    session_dir = tmp_path / "openai_traces" / "sessions" / "session.user-request"
    raw_records = sorted((session_dir / "calls").glob("*.json"))
    assert raw_records
    payload = json.loads(raw_records[0].read_text(encoding="utf-8"))
    persisted_episode = payload["trace_context"].get("episode_kind")
    assert persisted_episode is None


def test_user_request_without_session_identity_stays_ungrouped_after_compilation() -> None:
    solve_request = load_solve_request(prompt="Say hello.")
    runtime_request = runtime_solve_request_for_user_request(
        runtime_backend="local",
        seed=0,
        solve_request=solve_request,
    )

    assert runtime_request.trace_context is not None
    assert runtime_request.trace_context.request_mode == "user_request"
    assert trace_grouping_key(runtime_request.trace_context) is None

    _, plan = compile_execution_plan_from_solve_request(
        solve_request,
        seed=0,
        runtime_hash="runtime.hash",
        runtime_dir="runtime",
        trace_context=runtime_request.trace_context,
    )

    assert plan.trace_context.request_mode == "user_request"
    assert plan.trace_context.task_id
    assert plan.trace_context.runtime_hash == "runtime.hash"
    assert trace_grouping_key(plan.trace_context) is None


def test_persist_trace_reports_materialization_failure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGINTOR_OPENAI_TRACE_DIR", str(tmp_path / "openai_traces"))

    def fail_grouping(*args, **kwargs):
        raise RuntimeError("grouping failed")

    monkeypatch.setattr(openai_trace, "_write_grouped_views", fail_grouping)

    with pytest.raises(RuntimeError, match="failed to persist OpenAI trace"):
        persist_openai_trace(
            provider="openai",
            method_name="responses.create",
            model_class="default",
            model_name="gpt-test",
            reasoning_effort=None,
            instructions="solve",
            input_value="hello",
            request_payload={"model": "gpt-test"},
            request_metadata={
                "mode": "benchmark",
                "trace_context": OpenAITraceContext(
                    session_id="session.failure",
                    request_id="benchmark.demo.task.seed_0",
                    evaluation_unit_id="benchmark.demo.task.seed_0",
                    request_mode="benchmark",
                    task_id="demo.task",
                    seed=0,
                    runtime_hash="runtime.alpha",
                ).model_dump(),
            },
            response_text="ok",
        )

    session_dir = tmp_path / "openai_traces" / "sessions" / "session.failure"
    assert sorted((session_dir / "calls").glob("*.json"))
    assert not (session_dir / "materialization_state.json").exists()


def test_episode_kind_validator_rejects_legacy_literals() -> None:
    for legacy in ("user_request", "single_task", "benchmark_duplicate", "batch", "not_real"):
        coerced = OpenAITraceContext(
            session_id="session.legacy",
            episode_kind=legacy,
            episode_step_index=7,
        )
        assert coerced.episode_kind is None
        assert coerced.episode_step_index is None
