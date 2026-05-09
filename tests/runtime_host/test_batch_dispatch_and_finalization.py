from __future__ import annotations

import json
from pathlib import Path

import pytest

from agintor.core.exceptions import RuntimeLoadError
from agintor.providers import build_provider
from agintor.runtime.api import runtime_batch_request_for_tasks
from agintor.runtime.host import RuntimeHost
from agintor.contracts import (
    BenchmarkTask,
    CheckpointEnvelope,
    OperationSpec,
    RunResult,
    RuntimeBatchResponse,
    RuntimeIsolationPolicy,
)

from ._helpers import (
    _FakeDockerExecutor,
    _capability_exchange,
    _clear_runtime_provider_env,
    _make_repo_patch_task,
    _runtime_profile,
)


def test_runtime_host_run_batch_uses_effective_backend_for_dispatch(monkeypatch, tmp_path: Path):
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    monkeypatch.setattr(host.run_store, "prune_run", lambda manifest: manifest)
    runtime_profile = _runtime_profile()
    provider = build_provider(runtime_profile.runtime_provider.name, provider_profile=runtime_profile.runtime_provider)
    task = BenchmarkTask(
        task_id="batch.backend.task",
        family="top",
        prompt="Compute a value",
        task_type="structured_ops",
        operations=[],
        expected={},
    )
    original_builder = runtime_batch_request_for_tasks

    def build_docker_batch_request(*, request_id, runtime_backend, task_runs, budget_overrides=None, trace_context=None):
        return original_builder(
            request_id=request_id,
            runtime_backend="docker",
            task_runs=task_runs,
            budget_overrides=budget_overrides,
            trace_context=trace_context,
        )

    capability_exchange = _capability_exchange()

    def batch_handler(runtime_dir, request):
        invocation = request.invocations[0]
        return RuntimeBatchResponse(
            request_id=request.request_id,
            capability_exchange=capability_exchange,
            run_results=[
                RunResult(
                    request_id=invocation.request_id,
                    plan_id="plan.batch",
                    run_id=invocation.run_id,
                    run_root=invocation.run_root,
                    attempt_id=invocation.attempt_id,
                    runtime_hash="runtime-hash",
                    task_id=invocation.task.task_id,
                    seed=invocation.seed,
                    artifact={"status": "docker-batch"},
                    verifier_score=1.0,
                    cost=0.0,
                    latency=0.1,
                    faults=0,
                )
            ],
            provider_usage={},
        )

    docker = _FakeDockerExecutor(
        capability_exchange=capability_exchange,
        batch_handler=batch_handler,
    )

    def fail_local(*args, **kwargs):
        raise AssertionError("local transport should not be used for a docker-selected batch")

    monkeypatch.setattr("agintor.runtime.host.host.runtime_batch_request_for_tasks", build_docker_batch_request)
    monkeypatch.setattr(host, "_docker_executor", lambda: docker)
    monkeypatch.setattr(host, "_run_local_inspect", fail_local)
    monkeypatch.setattr(host, "_run_local_batch", fail_local)

    response = host.run_batch(
        "dummy-runtime",
        [(task, 0)],
        provider=provider,
        runtime_profile=runtime_profile,
    )
    manifest = host.run_store.load_run_manifest(response.run_results[0].run_id)

    assert docker.inspect_requests[0][1].requested_backend == "docker"
    assert docker.batch_requests[0][1].runtime_backend == "docker"
    assert all(invocation.runtime_backend == "docker" for invocation in docker.batch_requests[0][1].invocations)
    assert manifest.runtime_backend == "docker"


def test_runtime_host_run_batch_rescores_private_tasks_before_finalization(monkeypatch, tmp_path: Path):
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    monkeypatch.setattr(host.run_store, "prune_run", lambda manifest: manifest)
    runtime_profile = _runtime_profile()
    provider = build_provider(runtime_profile.runtime_provider.name, provider_profile=runtime_profile.runtime_provider)
    capability_exchange = _capability_exchange()
    monkeypatch.setattr(host, "inspect", lambda *args, **kwargs: capability_exchange)

    task = BenchmarkTask(
        task_id="batch.private.number",
        family="tool",
        prompt="Return the hidden number.",
        task_type="number",
        operations=[],
        expected=None,
        private_expected=7,
        verifier_type="number_exact",
    )
    captured: dict[str, object] = {}

    def succeed(runtime_dir, request, **kwargs):
        invocation = request.invocations[0]
        payload = request.model_dump(mode="json")
        captured["invocation"] = invocation
        captured["payload"] = payload
        return RuntimeBatchResponse(
            request_id=request.request_id,
            capability_exchange=capability_exchange,
            run_results=[
                RunResult(
                    request_id=invocation.request_id,
                    plan_id="plan.batch.private",
                    run_id=invocation.run_id,
                    run_root=invocation.run_root,
                    attempt_id=invocation.attempt_id,
                    runtime_hash="runtime-hash",
                    runtime_backend=invocation.runtime_backend,
                    task_id=invocation.task.task_id,
                    seed=invocation.seed,
                    artifact=7,
                    verifier_score=0.0,
                    cost=0.0,
                    latency=0.1,
                    faults=0,
                    run_lifecycle_state="completed",
                    lifecycle_state="completed",
                )
            ],
            provider_usage={},
        )

    monkeypatch.setattr(host, "_run_local_batch", succeed)

    response = host.run_batch(
        "dummy-runtime",
        [(task, 0)],
        provider=provider,
        runtime_profile=runtime_profile,
    )
    run = response.run_results[0]
    invocation = captured["invocation"]
    payload = captured["payload"]
    manifest = host.run_store.load_run_manifest(run.run_id)

    assert run.verifier_score == 1.0
    assert run.run_lifecycle_state == "completed"
    assert manifest.lifecycle_state == "completed"
    assert invocation.task.private_expected is None
    assert invocation.task.verifier_type == "none"
    assert invocation.authoritative_task.private_expected == 7
    assert "authoritative_task" not in payload["invocations"][0]
    assert "private_expected" not in json.dumps(payload, sort_keys=True)


def test_runtime_host_run_batch_rejects_mixed_backends_before_launch(monkeypatch, tmp_path: Path):
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    runtime_profile = _runtime_profile()
    provider = build_provider(runtime_profile.runtime_provider.name, provider_profile=runtime_profile.runtime_provider)
    task_one = BenchmarkTask(
        task_id="batch.mixed.one",
        family="top",
        prompt="Compute a value",
        task_type="structured_ops",
        operations=[],
        expected={},
    )
    task_two = task_one.model_copy(update={"task_id": "batch.mixed.two"})
    original_builder = runtime_batch_request_for_tasks
    called = {"inspect": False, "local": False, "docker": False}

    def build_mixed_batch_request(*, request_id, runtime_backend, task_runs, budget_overrides=None, trace_context=None):
        request = original_builder(
            request_id=request_id,
            runtime_backend="docker",
            task_runs=task_runs,
            budget_overrides=budget_overrides,
            trace_context=trace_context,
        )
        return request.model_copy(
            update={
                "invocations": [
                    request.invocations[0].model_copy(update={"runtime_backend": "docker"}),
                    request.invocations[1].model_copy(update={"runtime_backend": "local"}),
                ]
            }
        )

    def fail_inspect(*args, **kwargs):
        called["inspect"] = True
        raise AssertionError("inspect should not run for a mixed-backend batch")

    def fail_local(*args, **kwargs):
        called["local"] = True
        raise AssertionError("local batch transport should not run for a mixed-backend batch")

    def fail_docker():
        called["docker"] = True
        raise AssertionError("docker executor should not be created for a mixed-backend batch")

    monkeypatch.setattr("agintor.runtime.host.host.runtime_batch_request_for_tasks", build_mixed_batch_request)
    monkeypatch.setattr(host, "_run_local_inspect", fail_inspect)
    monkeypatch.setattr(host, "_run_local_batch", fail_local)
    monkeypatch.setattr(host, "_docker_executor", fail_docker)

    with pytest.raises(RuntimeLoadError, match="mixed runtime backends"):
        host.run_batch(
            "dummy-runtime",
            [(task_one, 0), (task_two, 0)],
            provider=provider,
            runtime_profile=runtime_profile,
        )

    assert called == {"inspect": False, "local": False, "docker": False}

def test_runtime_host_batch_finalization_preserves_external_checkpoint_ref(monkeypatch, tmp_path: Path):
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    monkeypatch.setattr(host.run_store, "prune_run", lambda manifest: manifest)
    runtime_profile = _runtime_profile()
    provider = build_provider(runtime_profile.runtime_provider.name, provider_profile=runtime_profile.runtime_provider)
    capability_exchange = _capability_exchange()
    external_checkpoint_ref = str((tmp_path / "external-checkpoints" / "checkpoint.resume.json").resolve())
    monkeypatch.setattr(host, "inspect", lambda *args, **kwargs: capability_exchange)
    monkeypatch.setattr(host.run_store, "latest_usable_checkpoint_ref", lambda run_ref: None)

    task_one = BenchmarkTask(
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
    task_two = task_one.model_copy(update={"task_id": "episode.step2", "episode_order": 1, "prompt": "Step two"})

    def succeed(runtime_dir, request, **kwargs):
        shared_run_id = request.invocations[0].run_id
        shared_run_root = request.invocations[0].run_root
        shared_attempt_id = request.invocations[0].attempt_id
        return RuntimeBatchResponse(
            request_id=request.request_id,
            capability_exchange=capability_exchange,
            run_results=[
                RunResult(
                    request_id=request.invocations[0].request_id,
                    plan_id="plan-1",
                    run_id=shared_run_id,
                    run_root=shared_run_root,
                    attempt_id=shared_attempt_id,
                    task_id=task_one.task_id,
                    seed=0,
                    artifact={"status": "checkpointed"},
                    verifier_score=0.0,
                    cost=0.0,
                    latency=0.1,
                    faults=0,
                    checkpoint_ref=external_checkpoint_ref,
                    latest_checkpoint_ref=external_checkpoint_ref,
                    run_lifecycle_state="paused",
                    lifecycle_state="paused",
                ),
                RunResult(
                    request_id=request.invocations[1].request_id,
                    plan_id="plan-2",
                    run_id=shared_run_id,
                    run_root=shared_run_root,
                    attempt_id=shared_attempt_id,
                    task_id=task_two.task_id,
                    seed=0,
                    artifact={"status": "ok"},
                    verifier_score=1.0,
                    cost=0.0,
                    latency=0.1,
                    faults=0,
                    run_lifecycle_state="completed",
                    lifecycle_state="completed",
                ),
            ],
            provider_usage={},
        )

    monkeypatch.setattr(host, "_run_local_batch", succeed)

    response = host.run_batch(
        "dummy-runtime",
        [(task_one, 0), (task_two, 0)],
        provider=provider,
        runtime_profile=runtime_profile,
    )
    manifest = host.run_store.load_run_manifest(response.run_results[0].run_id)

    assert manifest.lifecycle_state == "paused"
    assert manifest.latest_checkpoint_ref == external_checkpoint_ref
    assert all(run.latest_checkpoint_ref == external_checkpoint_ref for run in response.run_results)
    assert all(run.checkpoint_ref == external_checkpoint_ref for run in response.run_results)
    assert all(run.run_resumable is True for run in response.run_results)

def test_runtime_host_run_batch_marks_group_paused_from_first_failure_with_checkpoint(monkeypatch, tmp_path: Path):
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    monkeypatch.setattr(host.run_store, "prune_run", lambda manifest: manifest)
    runtime_profile = _runtime_profile()
    provider = build_provider(runtime_profile.runtime_provider.name, provider_profile=runtime_profile.runtime_provider)
    capability_exchange = _capability_exchange()
    monkeypatch.setattr(host, "inspect", lambda *args, **kwargs: capability_exchange)

    task_one = BenchmarkTask(
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
    task_two = task_one.model_copy(update={"task_id": "episode.step2", "episode_order": 1, "prompt": "Step two"})

    def succeed(runtime_dir, request, **kwargs):
        shared_run_id = request.invocations[0].run_id
        shared_run_root = request.invocations[0].run_root
        shared_attempt_id = request.invocations[0].attempt_id
        checkpoint_ref = str((Path(shared_run_root) / "checkpoints" / "checkpoint.resume.json").resolve())
        checkpoint_payload = CheckpointEnvelope(
            checkpoint_id="checkpoint.resume",
            runtime_contract_version=capability_exchange.runtime_contract_version,
            runtime_hash="runtime-hash",
            run_id=shared_run_id,
            run_root=shared_run_root,
            attempt_id=shared_attempt_id,
            request_id=request.invocations[0].request_id,
            plan_id="plan-1",
            task_id=task_one.task_id,
            seed=0,
        )
        checkpoint_path = Path(checkpoint_ref)
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        checkpoint_path.write_text(json.dumps((checkpoint_payload).model_dump(), indent=2, sort_keys=True), encoding="utf-8")
        (checkpoint_path.parent / "LATEST.json").write_text(
            json.dumps({"ref": checkpoint_ref}, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return RuntimeBatchResponse(
            request_id=request.request_id,
            capability_exchange=capability_exchange,
            run_results=[
                RunResult(
                    request_id=request.invocations[0].request_id,
                    plan_id="plan-1",
                    run_id=shared_run_id,
                    run_root=shared_run_root,
                    attempt_id=shared_attempt_id,
                    task_id=task_one.task_id,
                    seed=0,
                    artifact={"error": "first_failure"},
                    verifier_score=0.0,
                    cost=0.0,
                    latency=0.1,
                    faults=1,
                    hard_invalid=True,
                    invalid_reason="first failure",
                    failure_kind="first_failure",
                    checkpoint_ref=checkpoint_ref,
                ),
                RunResult(
                    request_id=request.invocations[1].request_id,
                    plan_id="plan-2",
                    run_id=shared_run_id,
                    run_root=shared_run_root,
                    attempt_id=shared_attempt_id,
                    task_id=task_two.task_id,
                    seed=0,
                    artifact={"status": "ok"},
                    verifier_score=1.0,
                    cost=0.0,
                    latency=0.1,
                    faults=0,
                ),
            ],
            provider_usage={},
        )

    monkeypatch.setattr(host, "_run_local_batch", succeed)

    response = host.run_batch(
        "dummy-runtime",
        [(task_one, 0), (task_two, 0)],
        provider=provider,
        runtime_profile=runtime_profile,
    )
    manifest = host.run_store.load_run_manifest(response.run_results[0].run_id)

    assert manifest.lifecycle_state == "paused"
    assert manifest.task_id is None
    assert manifest.last_failure_kind == "first_failure"
    assert manifest.latest_checkpoint_ref
    assert all(run.run_lifecycle_state == "paused" for run in response.run_results)
    assert all(run.run_resumable is True for run in response.run_results)

def test_runtime_host_run_batch_marks_group_failed_without_checkpoint(monkeypatch, tmp_path: Path):
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    monkeypatch.setattr(host.run_store, "prune_run", lambda manifest: manifest)
    runtime_profile = _runtime_profile()
    provider = build_provider(runtime_profile.runtime_provider.name, provider_profile=runtime_profile.runtime_provider)
    capability_exchange = _capability_exchange()
    monkeypatch.setattr(host, "inspect", lambda *args, **kwargs: capability_exchange)

    task_one = BenchmarkTask(
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
    task_two = task_one.model_copy(update={"task_id": "episode.step2", "episode_order": 1, "prompt": "Step two"})

    def succeed(runtime_dir, request, **kwargs):
        shared_run_id = request.invocations[0].run_id
        shared_run_root = request.invocations[0].run_root
        shared_attempt_id = request.invocations[0].attempt_id
        return RuntimeBatchResponse(
            request_id=request.request_id,
            capability_exchange=capability_exchange,
            run_results=[
                RunResult(
                    request_id=request.invocations[0].request_id,
                    plan_id="plan-1",
                    run_id=shared_run_id,
                    run_root=shared_run_root,
                    attempt_id=shared_attempt_id,
                    task_id=task_one.task_id,
                    seed=0,
                    artifact={"error": "first_failure"},
                    verifier_score=0.0,
                    cost=0.0,
                    latency=0.1,
                    faults=1,
                    hard_invalid=True,
                    invalid_reason="first failure",
                    failure_kind="first_failure",
                ),
                RunResult(
                    request_id=request.invocations[1].request_id,
                    plan_id="plan-2",
                    run_id=shared_run_id,
                    run_root=shared_run_root,
                    attempt_id=shared_attempt_id,
                    task_id=task_two.task_id,
                    seed=0,
                    artifact={"status": "ok"},
                    verifier_score=1.0,
                    cost=0.0,
                    latency=0.1,
                    faults=0,
                ),
            ],
            provider_usage={},
        )

    monkeypatch.setattr(host, "_run_local_batch", succeed)

    response = host.run_batch(
        "dummy-runtime",
        [(task_one, 0), (task_two, 0)],
        provider=provider,
        runtime_profile=runtime_profile,
    )
    manifest = host.run_store.load_run_manifest(response.run_results[0].run_id)

    assert manifest.lifecycle_state == "failed"
    assert manifest.latest_checkpoint_ref is None
    assert manifest.last_failure_kind == "first_failure"
    assert all(run.run_lifecycle_state == "failed" for run in response.run_results)
    assert all(run.run_resumable is False for run in response.run_results)

def test_runtime_host_run_batch_backfills_manifest_runtime_hash_from_run_results(monkeypatch, tmp_path: Path):
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    monkeypatch.setattr(host.run_store, "prune_run", lambda manifest: manifest)
    runtime_profile = _runtime_profile()
    provider = build_provider(runtime_profile.runtime_provider.name, provider_profile=runtime_profile.runtime_provider)
    capability_exchange = _capability_exchange()
    monkeypatch.setattr(host, "inspect", lambda *args, **kwargs: capability_exchange)

    task = BenchmarkTask(
        task_id="batch.runtime-hash",
        family="top",
        prompt="Compute a value",
        task_type="structured_ops",
        operations=[],
        expected={},
    )

    def succeed(runtime_dir, request, **kwargs):
        invocation = request.invocations[0]
        return RuntimeBatchResponse(
            request_id=request.request_id,
            capability_exchange=capability_exchange,
            run_results=[
                RunResult(
                    request_id=invocation.request_id,
                    plan_id="plan-runtime-hash",
                    run_id=invocation.run_id,
                    run_root=invocation.run_root,
                    attempt_id=invocation.attempt_id,
                    runtime_hash="runtime-hash",
                    task_id=task.task_id,
                    seed=0,
                    artifact={"status": "ok"},
                    verifier_score=1.0,
                    cost=0.0,
                    latency=0.1,
                    faults=0,
                )
            ],
            provider_usage={},
        )

    monkeypatch.setattr(host, "_run_local_batch", succeed)

    response = host.run_batch(
        "dummy-runtime",
        [(task, 0)],
        provider=provider,
        runtime_profile=runtime_profile,
    )
    manifest = host.run_store.load_run_manifest(response.run_results[0].run_id)

    assert manifest.runtime_hash == "runtime-hash"

def test_runtime_host_run_batch_finalizes_missing_group_result_before_raising(monkeypatch, tmp_path: Path):
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    monkeypatch.setattr(host.run_store, "prune_run", lambda manifest: manifest)
    runtime_profile = _runtime_profile()
    provider = build_provider(runtime_profile.runtime_provider.name, provider_profile=runtime_profile.runtime_provider)
    capability_exchange = _capability_exchange()
    monkeypatch.setattr(host, "inspect", lambda *args, **kwargs: capability_exchange)
    task = BenchmarkTask(
        task_id="batch.missing",
        family="top",
        prompt="Compute a value",
        task_type="structured_ops",
        operations=[],
        expected={},
    )
    captured: dict[str, str] = {}

    def missing(runtime_dir, request, **kwargs):
        captured["run_id"] = request.invocations[0].run_id
        captured["attempt_id"] = request.invocations[0].attempt_id
        return RuntimeBatchResponse(
            request_id=request.request_id,
            capability_exchange=capability_exchange,
            run_results=[],
            provider_usage={},
        )

    monkeypatch.setattr(host, "_run_local_batch", missing)

    with pytest.raises(RuntimeLoadError, match="omitted run results"):
        host.run_batch(
            "dummy-runtime",
            [(task, 0)],
            provider=provider,
            runtime_profile=runtime_profile,
        )

    manifest = host.run_store.load_run_manifest(captured["run_id"])
    attempt = host.run_store.load_attempt_manifest(manifest.run_id, captured["attempt_id"])
    assert manifest.lifecycle_state == "failed"
    assert manifest.last_failure_kind == "missing_batch_result"
    assert attempt.lifecycle_state == "failed"
    assert attempt.failure_kind == "missing_batch_result"

def test_runtime_host_run_batch_keeps_benchmark_duplicates_as_single_task_units(monkeypatch, tmp_path: Path):
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    monkeypatch.setattr(host.run_store, "prune_run", lambda manifest: manifest)
    runtime_profile = _runtime_profile()
    provider = build_provider(runtime_profile.runtime_provider.name, provider_profile=runtime_profile.runtime_provider)
    capability_exchange = _capability_exchange()
    monkeypatch.setattr(host, "inspect", lambda *args, **kwargs: capability_exchange)
    task = BenchmarkTask(
        task_id="benchmark.duplicate.task",
        family="top",
        prompt="Compute a value",
        task_type="structured_ops",
        operations=[],
        expected={},
    )

    def succeed(runtime_dir, request, **kwargs):
        return RuntimeBatchResponse(
            request_id=request.request_id,
            capability_exchange=capability_exchange,
            run_results=[
                RunResult(
                    request_id=invocation.request_id,
                    plan_id=f"plan.{index}",
                    run_id=invocation.run_id,
                    run_root=invocation.run_root,
                    attempt_id=invocation.attempt_id,
                    task_id=invocation.task.task_id,
                    seed=invocation.seed,
                    artifact={"status": f"ok-{index}"},
                    verifier_score=1.0,
                    cost=0.0,
                    latency=0.1,
                    faults=0,
                )
                for index, invocation in enumerate(request.invocations)
            ],
            provider_usage={},
        )

    monkeypatch.setattr(host, "_run_local_batch", succeed)

    response = host.run_batch(
        "dummy-runtime",
        [(task, 0), (task, 0)],
        provider=provider,
        runtime_profile=runtime_profile,
    )

    manifests = [host.run_store.load_run_manifest(run.run_id) for run in response.run_results]
    assert len({run.run_id for run in response.run_results}) == 2
    assert {manifest.task_id for manifest in manifests} == {task.task_id}

def test_run_batch_preflight_rejects_provider_backed_benchmark_tasks(monkeypatch, tmp_path: Path):
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    _clear_runtime_provider_env(monkeypatch)
    runtime_profile = _runtime_profile()
    provider = build_provider(runtime_profile.runtime_provider.name, provider_profile=runtime_profile.runtime_provider)
    capability_exchange = _capability_exchange()
    monkeypatch.setattr(host, "inspect", lambda *args, **kwargs: capability_exchange)
    called = {"batch": False}

    task = BenchmarkTask(
        task_id="batch.direct-response",
        family="top",
        prompt="Say hello.",
        task_type="structured_ops",
        operations=[
            OperationSpec(
                op_id="respond",
                kind="direct_response",
                output_key="response",
                description="Return a greeting.",
                args={},
                externally_visible=True,
            )
        ],
        expected="hello",
        verifier_type="string_exact",
        verification_required=True,
        allow_best_effort=False,
        externally_visible=True,
    )

    def fail_if_called(*args, **kwargs):
        called["batch"] = True
        raise AssertionError("batch launch should not happen when runtime credentials are missing")

    monkeypatch.setattr(host, "_run_local_batch", fail_if_called)

    with pytest.raises(RuntimeLoadError, match="one of .*"):
        host.run_batch(
            "dummy-runtime",
            [(task, 0)],
            provider=provider,
            runtime_profile=runtime_profile,
        )

    assert called["batch"] is False

def test_run_batch_preflight_rejects_read_only_runtime_for_repo_patch_plan_before_run_creation(
    monkeypatch,
    tmp_path: Path,
):
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    runtime_profile = _runtime_profile()
    provider = build_provider(runtime_profile.runtime_provider.name, provider_profile=runtime_profile.runtime_provider)
    capability_exchange = _capability_exchange().model_copy(
        update={
            "required_env_names": [],
            "required_env_any_of": [],
            "runtime_isolation_policy": RuntimeIsolationPolicy(
                required_guarantees=[],
                network_policy="none",
                filesystem_policy="workspace-read-only",
            ),
        }
    )
    monkeypatch.setattr(host, "inspect", lambda *args, **kwargs: capability_exchange)
    called = {"batch": False}

    def fail_if_called(*args, **kwargs):
        called["batch"] = True
        raise AssertionError("batch launch should not happen when the compiled plan exceeds runtime filesystem policy")

    monkeypatch.setattr(host, "_run_local_batch", fail_if_called)

    with pytest.raises(RuntimeLoadError, match="writable filesystem access"):
        host.run_batch(
            "dummy-runtime",
            [(_make_repo_patch_task("batch.repo-patch"), 0)],
            provider=provider,
            runtime_profile=runtime_profile,
        )

    assert called["batch"] is False
    assert list((tmp_path / "host" / "runs").glob("run.*")) == []

def test_runtime_host_run_batch_rejects_stray_run_results(monkeypatch, tmp_path: Path):
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    monkeypatch.setattr(host.run_store, "prune_run", lambda manifest: manifest)
    runtime_profile = _runtime_profile()
    provider = build_provider(runtime_profile.runtime_provider.name, provider_profile=runtime_profile.runtime_provider)
    capability_exchange = _capability_exchange()
    monkeypatch.setattr(host, "inspect", lambda *args, **kwargs: capability_exchange)
    task = BenchmarkTask(
        task_id="batch.stray",
        family="top",
        prompt="Compute a value",
        task_type="structured_ops",
        operations=[],
        expected={},
    )
    captured: dict[str, str] = {}

    def succeed(runtime_dir, request, **kwargs):
        invocation = request.invocations[0]
        captured["run_id"] = invocation.run_id
        captured["attempt_id"] = invocation.attempt_id
        return RuntimeBatchResponse(
            request_id=request.request_id,
            capability_exchange=capability_exchange,
            run_results=[
                RunResult(
                    request_id=invocation.request_id,
                    plan_id="plan.expected",
                    run_id=invocation.run_id,
                    run_root=invocation.run_root,
                    attempt_id=invocation.attempt_id,
                    task_id=task.task_id,
                    seed=invocation.seed,
                    artifact={"status": "ok"},
                    verifier_score=1.0,
                    cost=0.0,
                    latency=0.1,
                    faults=0,
                ),
                RunResult(
                    request_id="unexpected.request",
                    plan_id="plan.unexpected",
                    run_id="run.unexpected",
                    run_root=str((tmp_path / "unexpected").resolve()),
                    attempt_id="attempt_unexpected",
                    task_id="unexpected.task",
                    seed=0,
                    artifact={"status": "unexpected"},
                    verifier_score=0.0,
                    cost=0.0,
                    latency=0.1,
                    faults=0,
                ),
            ],
            provider_usage={},
        )

    monkeypatch.setattr(host, "_run_local_batch", succeed)

    with pytest.raises(RuntimeLoadError, match="unexpected run_id"):
        host.run_batch(
            "dummy-runtime",
            [(task, 0)],
            provider=provider,
            runtime_profile=runtime_profile,
        )

    manifest = host.run_store.load_run_manifest(captured["run_id"])
    attempt = host.run_store.load_attempt_manifest(manifest.run_id, captured["attempt_id"])
    assert manifest.lifecycle_state == "failed"
    assert manifest.last_failure_kind == "protocol_mismatch"
    assert attempt.lifecycle_state == "failed"
    assert attempt.failure_kind == "protocol_mismatch"
