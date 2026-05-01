from __future__ import annotations

from .helpers import (
    time,
    ThreadPoolExecutor,
    SimpleNamespace,
    init_runtime,
    ReplayProvider,
    clone_provider,
    load_solve_request,
    runtime_solve_request_for_task,
    runtime_solve_request_for_user_request,
    RuntimeHost,
    ResumeRequest,
    _make_direct_response_task,
)


def test_clone_provider_shares_replay_coordinator_but_keeps_usage_local():
    request = SimpleNamespace(model_class="small")
    provider = ReplayProvider(
        [
            {"text": "first"},
            {"text": "second"},
            {"text": "third"},
        ]
    )
    cloned = clone_provider(provider)
    assert provider.generate(request).text == "first"
    assert cloned.generate(request).text == "second"
    assert provider.generate(request).text == "third"
    assert provider.usage_summary()["calls"] == 2
    assert cloned.usage_summary()["calls"] == 1

def test_replay_provider_reserved_clones_consume_only_their_window_and_keep_usage_local():
    request = SimpleNamespace(model_class="small")
    provider = ReplayProvider(
        [
            {"text": "w0-0"},
            {"text": "w0-1"},
            {"text": "w1-0"},
            {"text": "w1-1"},
        ]
    )
    left = provider.clone_for_allocation(provider.reserve_rows(2, allocation_key="request:w0"))
    right = provider.clone_for_allocation(provider.reserve_rows(2, allocation_key="request:w1"))

    assert left.generate(request).text == "w0-0"
    assert right.generate(request).text == "w1-0"
    assert left.generate(request).text == "w0-1"
    assert right.generate(request).text == "w1-1"
    assert provider.usage_summary()["calls"] == 0
    assert left.usage_summary()["calls"] == 2
    assert right.usage_summary()["calls"] == 2

def test_replay_provider_reserved_clones_are_stable_under_thread_timing_variation():
    request = SimpleNamespace(model_class="small")

    def run_with_delays(left_delay: float, right_delay: float):
        provider = ReplayProvider(
            [
                {"text": "w0-0"},
                {"text": "w0-1"},
                {"text": "w1-0"},
                {"text": "w1-1"},
            ]
        )
        left = provider.clone_for_allocation(provider.reserve_rows(2, allocation_key="request:w0"))
        right = provider.clone_for_allocation(provider.reserve_rows(2, allocation_key="request:w1"))

        def consume_two(clone, delay):
            time.sleep(delay)
            return [clone.generate(request).text, clone.generate(request).text]

        with ThreadPoolExecutor(max_workers=2) as executor:
            left_future = executor.submit(consume_two, left, left_delay)
            right_future = executor.submit(consume_two, right, right_delay)
            return left_future.result(), right_future.result()

    assert run_with_delays(0.0, 0.05) == (["w0-0", "w0-1"], ["w1-0", "w1-1"])
    assert run_with_delays(0.05, 0.0) == (["w0-0", "w0-1"], ["w1-0", "w1-1"])

def test_host_resume_applies_request_id_override_for_user_request(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    request = runtime_solve_request_for_user_request(
        runtime_backend="local",
        seed=0,
        solve_request=load_solve_request(prompt="Say hello."),
    )
    first = host.solve(
        runtime_dir,
        request,
        provider=ReplayProvider([{"text": "hello", "model_name": "replay/small"}]),
    )

    resumed = host.resume(
        runtime_dir,
        request=ResumeRequest(
            run_ref=first.solve_result.run_id,
            request_id="resume.user.override",
        ),
        provider=ReplayProvider([]),
    )

    assert resumed.solve_result.request_id == "resume.user.override"
    assert resumed.solve_result.mode == "user_request"

def test_host_resume_applies_request_id_override_for_benchmark_request(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    task = _make_direct_response_task("resume.override.benchmark")
    request = runtime_solve_request_for_task(runtime_backend="local", seed=0, task=task)
    first = host.solve(
        runtime_dir,
        request,
        provider=ReplayProvider([{"text": "hello", "model_name": "replay/small"}]),
    )

    resumed = host.resume(
        runtime_dir,
        request=ResumeRequest(
            run_ref=first.solve_result.run_id,
            request_id="resume.benchmark.override",
        ),
        provider=ReplayProvider([]),
    )

    assert resumed.solve_result.request_id == "resume.benchmark.override"
    assert resumed.solve_result.mode == "benchmark"

def test_host_run_batch_reports_sum_of_run_result_provider_usage(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    tasks = [
        (_make_direct_response_task("batch.usage.one"), 0),
        (_make_direct_response_task("batch.usage.two"), 0),
    ]
    response = host.run_batch(
        runtime_dir,
        tasks,
        provider=ReplayProvider(
            [
                {"text": "one", "model_name": "replay/small", "input_tokens": 3, "output_tokens": 2, "token_estimate": 5, "dollar_cost": 0.01},
                {"text": "two", "model_name": "replay/small", "input_tokens": 4, "output_tokens": 1, "token_estimate": 5, "dollar_cost": 0.02},
            ]
        ),
    )

    summed_usage: dict[str, float | int] = {}
    for run in response.run_results:
        for key, value in run.provider_usage.items():
            summed_usage[key] = summed_usage.get(key, 0) + value

    assert response.provider_usage == summed_usage

def test_host_run_batch_scopes_grouped_episode_trace_rows_to_each_invocation(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    task_one = _make_direct_response_task("episode.trace.one").model_copy(
        update={"transfer_scored": True, "episode_id": "episode-trace", "episode_order": 0}
    )
    task_two = _make_direct_response_task("episode.trace.two").model_copy(
        update={"transfer_scored": True, "episode_id": "episode-trace", "episode_order": 1}
    )

    response = host.run_batch(
        runtime_dir,
        [(task_one, 0), (task_two, 0)],
        provider=ReplayProvider(
            [
                {"text": "first", "model_name": "replay/small"},
                {"text": "second", "model_name": "replay/small"},
            ]
        ),
    )

    for run in response.run_results:
        request_ids = {row.get("request_id") for row in run.trace_rows() if row.get("request_id")}
        assert request_ids == {run.request_id}
