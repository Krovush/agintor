from __future__ import annotations

import pytest


def test_runtime_session_seed_rejected_in_benchmark_mode() -> None:
    from agintor.contracts import RuntimeSessionSeed, RuntimeSolveRequest, BenchmarkTask

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
    from agintor.runtime.api import load_solve_request
    from agintor.contracts import OpenAITraceContext, RuntimeSessionSeed, RuntimeSolveRequest

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
