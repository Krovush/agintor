from __future__ import annotations

from pathlib import Path


def test_host_solve_returns_post_message_state_for_user_request(tmp_path: Path) -> None:
    from agintor.runtime.project import init_runtime
    from agintor.providers import ReplayProvider
    from agintor.runtime.api import load_solve_request, runtime_solve_request_for_user_request
    from agintor.runtime.host import RuntimeHost

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
    from agintor.runtime.project import init_runtime
    from agintor.providers import ReplayProvider
    from agintor.runtime.api import runtime_solve_request_for_task
    from agintor.runtime.host import RuntimeHost
    from agintor.contracts import BenchmarkTask

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

    from agintor.runtime.project import init_runtime
    from agintor.providers import ReplayProvider
    from agintor.runtime.api import load_solve_request, runtime_solve_request_for_user_request
    from agintor.runtime.host import RuntimeHost
    from agintor.contracts import RuntimeSessionSeed

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
