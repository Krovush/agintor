from __future__ import annotations

from agintor.runtime.api import (
    load_solve_request,
    runtime_batch_request_for_tasks,
    runtime_solve_failure_response,
    runtime_solve_request_for_task,
)
from agintor.contracts import BenchmarkTask, OpenAITraceContext

from ._helpers import _capability_exchange


def test_runtime_solve_failure_response_shapes_contract_error():
    capability_exchange = _capability_exchange()
    request = load_solve_request(prompt="Say hello.")

    response = runtime_solve_failure_response(
        request,
        "runtime-hash",
        capability_exchange,
        mode="user_request",
        summary="minimax credentials are required for hosted model calls.",
        fault_code="missing_provider_credentials",
    )

    assert response.solve_result.status == "failed"
    assert response.solve_result.verification_status == "failed"
    assert response.solve_result.artifact["error"] == "missing_provider_credentials"
    assert response.solve_result.faults["contract_error"] is True

def test_runtime_batch_request_normalizes_invocation_request_ids():
    task = BenchmarkTask(
        task_id="demo.task",
        family="top",
        prompt="Compute a value",
        task_type="structured_ops",
        operations=[],
        expected={},
    )
    request = runtime_batch_request_for_tasks(
        request_id="batch.test",
        runtime_backend="local",
        task_runs=[(task, 7)],
    )
    assert request.request_id == "batch.test"
    assert request.trace_context.request_id == "batch.test"
    assert request.invocations[0].request_id == "benchmark.demo.task.seed_7"
    assert request.invocations[0].episode_kind is None
    assert request.invocations[0].trace_context.request_id == "benchmark.demo.task.seed_7"
    assert request.invocations[0].trace_context.episode_kind is None

def test_runtime_batch_request_classifies_transfer_and_duplicate_invocations():
    duplicate_task = BenchmarkTask(
        task_id="demo.duplicate",
        family="top",
        prompt="Compute a value",
        task_type="structured_ops",
        operations=[],
        expected={},
    )
    transfer_task_one = BenchmarkTask(
        task_id="episode.step1",
        family="top",
        prompt="Step one",
        task_type="structured_ops",
        operations=[],
        expected={},
        transfer_scored=True,
        episode_id="episode-alpha",
        episode_order=0,
    )
    transfer_task_two = transfer_task_one.model_copy(update={"task_id": "episode.step2", "episode_order": 1})

    request = runtime_batch_request_for_tasks(
        request_id="batch.classify",
        runtime_backend="local",
        task_runs=[
            (duplicate_task, 7),
            (duplicate_task, 7),
            (transfer_task_one, 3),
            (transfer_task_two, 3),
        ],
    )

    duplicate_invocations = request.invocations[:2]
    assert [invocation.episode_kind for invocation in duplicate_invocations] == [None, None]
    assert len({invocation.evaluation_unit_id for invocation in duplicate_invocations}) == 2
    assert {invocation.task.task_id for invocation in duplicate_invocations} == {"demo.duplicate"}

    transfer_invocations = request.invocations[2:]
    assert [invocation.episode_kind for invocation in transfer_invocations] == [
        "transfer_episode",
        "transfer_episode",
    ]
    assert [invocation.episode_step_index for invocation in transfer_invocations] == [0, 1]
    assert len({invocation.evaluation_unit_id for invocation in transfer_invocations}) == 1

def test_runtime_solve_request_for_task_canonicalizes_trace_identity():
    task = BenchmarkTask(
        task_id="demo.task",
        family="top",
        prompt="Compute a value",
        task_type="structured_ops",
        operations=[],
        expected={},
    )
    request = runtime_solve_request_for_task(
        runtime_backend="local",
        seed=11,
        task=task,
        trace_context=OpenAITraceContext(
            session_id="session-1",
            build_id="build-1",
            request_id="stale-request",
            task_id="stale-task",
            seed=999,
        ),
    )

    assert request.request_id == "benchmark.demo.task.seed_11"
    assert request.trace_context.session_id == "session-1"
    assert request.trace_context.build_id == "build-1"
    assert request.trace_context.provider_role == "runtime"
    assert request.trace_context.request_id == "benchmark.demo.task.seed_11"
    assert request.trace_context.task_id == "demo.task"
    assert request.trace_context.seed == 11
