from __future__ import annotations

import json
from pathlib import Path

import pytest

from agintor.core.exceptions import RuntimeLoadError
from agintor.providers import build_provider
from agintor.runtime.api import (
    compile_execution_plan_from_task,
    load_solve_request,
    runtime_solve_request_for_user_request,
)
from agintor.runtime.host import RuntimeHost
from agintor.core.versioning import RUNTIME_CONTRACT_VERSION
from agintor.contracts import (
    AttemptManifest,
    BenchmarkTask,
    CheckpointEnvelope,
    OpenAITraceContext,
    ResumeRequest,
    RunManifest,
    RuntimeResumeRequest,
    RuntimeSolveRequest,
    RuntimeSolveResponse,
    SolveResult,
)

from ._helpers import _FakeDockerExecutor, _capability_exchange, _runtime_profile, _solve_response


def test_runtime_host_resume_uses_run_ref_and_reuses_original_solve_request_for_preflight(monkeypatch, tmp_path: Path):
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    runtime_profile = _runtime_profile()
    provider = build_provider(runtime_profile.runtime_provider.name, provider_profile=runtime_profile.runtime_provider)
    original_request = runtime_solve_request_for_user_request(
        runtime_backend="local",
        seed=0,
        solve_request=load_solve_request(prompt="Say hello."),
    )
    run_root = tmp_path / "host" / "runs" / "run.123"
    run_root.mkdir(parents=True)
    checkpoint_path = run_root / "checkpoints" / "checkpoint.resume.test.0002.json"
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.write_text("{}", encoding="utf-8")
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    manifest = RunManifest(
        run_id="run.123",
        run_root=str(run_root),
        request_id=original_request.request_id,
        request_mode="user_request",
        runtime_backend="local",
    )
    attempt = AttemptManifest(
        attempt_id="attempt_0002",
        run_id=manifest.run_id,
        run_root=manifest.run_root,
        sequence_no=2,
        launch_kind="resume",
        workspace_root=str(run_root / "attempts" / "attempt_0002" / "workspace"),
    )
    capability_exchange = _capability_exchange()
    monkeypatch.setattr(host, "inspect", lambda *args, **kwargs: capability_exchange)
    monkeypatch.setattr(
        host.run_store,
        "resolve_resume_target",
        lambda **kwargs: type(
            "ResumeTarget",
            (),
            {
                "run_manifest": manifest,
                "checkpoint_path": checkpoint_path,
                "checkpoint_store_dir": checkpoint_path.parent.resolve(),
            },
        )(),
    )
    monkeypatch.setattr(
        host.run_store,
        "load_request_bundle",
        lambda run_ref: {"request_kind": "runtime_solve_request", "payload": json.loads(json.dumps((original_request).model_dump()))},
    )
    checkpoint_task = BenchmarkTask(
        task_id="resume.task",
        family="top",
        prompt="Resume task",
        task_type="structured_ops",
        operations=[],
        expected={},
    )
    checkpoint_plan = compile_execution_plan_from_task(
        checkpoint_task,
        request_id=original_request.request_id,
        seed=0,
        runtime_hash="runtime-hash",
        runtime_dir="/mnt/runtime",
    )
    monkeypatch.setattr(
        host.run_store,
        "load_checkpoint_envelope",
        lambda checkpoint_ref: CheckpointEnvelope(
            checkpoint_id="checkpoint.resume.test.0002",
            runtime_contract_version=RUNTIME_CONTRACT_VERSION,
            runtime_hash="runtime-hash",
            run_id=manifest.run_id,
            run_root=manifest.run_root,
            request_id=original_request.request_id,
            plan_id=checkpoint_plan.plan_id,
            task_id=checkpoint_task.task_id,
            seed=0,
            plan_snapshot=(checkpoint_plan).model_dump(),
            task_payload=(checkpoint_task).model_dump(),
        ),
    )
    monkeypatch.setattr(host.run_store, "begin_attempt", lambda *args, **kwargs: attempt)
    captured: dict[str, RuntimeResumeRequest] = {}
    preflight: dict[str, RuntimeSolveRequest] = {}
    def succeed(runtime_dir, runtime_request, **kwargs):
        captured["request"] = runtime_request
        return RuntimeSolveResponse(
            request_id=runtime_request.request_id,
            capability_exchange=capability_exchange,
            solve_result=SolveResult(
                request_id=runtime_request.request_id,
                runtime_hash="hash",
                mode="user_request",
                artifact={"status": "resumed"},
                status="best_effort",
                verification_status="best_effort",
                summary="ok",
                checks=[],
                budget={},
                provider_usage={},
                faults={"hard_invalid": False},
                verified=False,
                best_effort=True,
            ),
        )

    monkeypatch.setattr(
        host,
        "_preflight_solve_contract",
        lambda runtime_dir, capability_exchange, request, **kwargs: preflight.setdefault("request", request),
    )
    monkeypatch.setattr(host, "_run_local_resume", succeed)
    resumed = host.resume(
        runtime_dir,
        ResumeRequest(
            run_ref=manifest.run_id,
            request_id="resume.user.override",
            trace_context=OpenAITraceContext(
                runtime_session_id="sess.resume",
                runtime_message_id="msg.resume",
                runtime_message_index=4,
            ),
        ),
        provider=provider,
        runtime_profile=runtime_profile,
    )
    assert isinstance(captured["request"], RuntimeResumeRequest)
    assert captured["request"].checkpoint_ref == str(checkpoint_path.resolve())
    assert captured["request"].run_ref == manifest.run_id
    assert captured["request"].request_id == "resume.user.override"
    assert captured["request"].trace_context.request_id == "resume.user.override"
    assert captured["request"].trace_context.runtime_dir == str(runtime_dir.resolve())
    assert captured["request"].trace_context.runtime_session_id == "sess.resume"
    assert captured["request"].trace_context.runtime_message_id == "msg.resume"
    assert captured["request"].trace_context.runtime_message_index == 4
    assert isinstance(preflight["request"], RuntimeSolveRequest)
    assert preflight["request"].request_id == "resume.user.override"
    assert preflight["request"].trace_context.request_id == "resume.user.override"
    assert preflight["request"].trace_context.runtime_dir == str(runtime_dir.resolve())
    assert preflight["request"].trace_context.runtime_session_id == "sess.resume"
    assert preflight["request"].trace_context.runtime_message_id == "msg.resume"
    assert preflight["request"].trace_context.runtime_message_index == 4
    assert resumed.solve_result.artifact == {"status": "resumed"}
    assert resumed.solve_result.request_id == "resume.user.override"

def test_runtime_host_resume_accepts_runtime_task_invocation_bundle_and_uses_checkpoint_task(
    monkeypatch,
    tmp_path: Path,
):
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    request_task = BenchmarkTask(
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
    checkpoint_task = request_task.model_copy(update={"task_id": "episode.step2", "episode_order": 1})
    checkpoint_path = tmp_path / "host" / "runs" / "run.123" / "checkpoints" / "checkpoint.resume.test.0002.json"
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.write_text("{}", encoding="utf-8")
    manifest = RunManifest(
        run_id="run.123",
        run_root=str(checkpoint_path.parents[1]),
        request_id="benchmark.episode.step1.seed_0",
        request_mode="batch",
        runtime_backend="local",
    )
    attempt = AttemptManifest(
        attempt_id="attempt_0002",
        run_id=manifest.run_id,
        run_root=manifest.run_root,
        sequence_no=2,
        launch_kind="resume",
        workspace_root=str(Path(manifest.run_root) / "attempts" / "attempt_0002" / "workspace"),
    )
    checkpoint_plan = compile_execution_plan_from_task(
        checkpoint_task,
        request_id="benchmark.episode.step2.seed_0",
        seed=0,
        runtime_hash="runtime-hash",
        runtime_dir=str(tmp_path / "runtime"),
    )
    checkpoint_envelope = CheckpointEnvelope(
        checkpoint_id="checkpoint.resume.test.0002",
        runtime_contract_version=RUNTIME_CONTRACT_VERSION,
        runtime_hash="runtime-hash",
        run_id=manifest.run_id,
        run_root=manifest.run_root,
        request_id=checkpoint_plan.request_id,
        plan_id=checkpoint_plan.plan_id,
        task_id=checkpoint_task.task_id,
        seed=0,
        plan_snapshot=(checkpoint_plan).model_dump(),
        task_payload=(checkpoint_task).model_dump(),
    )
    monkeypatch.setattr(
        host.run_store,
        "resolve_resume_target",
        lambda **kwargs: type(
            "ResumeTarget",
            (),
            {
                "run_manifest": manifest,
                "checkpoint_path": checkpoint_path,
                "checkpoint_store_dir": checkpoint_path.parent.resolve(),
            },
        )(),
    )
    monkeypatch.setattr(
        host.run_store,
        "load_request_bundle",
        lambda run_ref: {
            "request": {
                "request_kind": "runtime_task_invocation",
                "payload": {
                    "request_id": "benchmark.episode.step1.seed_0",
                    "seed": 0,
                    "task": (request_task).model_dump(),
                },
            }
        },
    )
    monkeypatch.setattr(host.run_store, "load_checkpoint_envelope", lambda checkpoint_ref: checkpoint_envelope)
    monkeypatch.setattr(host.run_store, "begin_attempt", lambda *args, **kwargs: attempt)

    runtime_request, preflight_request, _, _ = host._resolve_runtime_resume_request(
        ResumeRequest(run_ref=manifest.run_id)
    )

    assert isinstance(runtime_request, RuntimeResumeRequest)
    assert preflight_request.mode == "benchmark"
    assert preflight_request.task.task_id == "episode.step2"

def test_runtime_host_resume_resolves_backend_before_inspect(monkeypatch, tmp_path: Path):
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    runtime_profile = _runtime_profile()
    provider = build_provider(runtime_profile.runtime_provider.name, provider_profile=runtime_profile.runtime_provider)
    manifest = host.run_store.create_run(
        request_id="resume.backend",
        evaluation_unit_id="resume.backend",
        request_mode="user_request",
        runtime_backend="docker",
    )
    attempt = host.run_store.begin_attempt(manifest, launch_kind="resume")
    preflight_request = RuntimeSolveRequest(
        request_id="resume.backend",
        evaluation_unit_id="resume.backend",
        runtime_backend="docker",
        mode="user_request",
        seed=0,
        solve_request=load_solve_request(prompt="Resume hello."),
    )
    runtime_request = RuntimeResumeRequest(
        request_id="resume.backend",
        evaluation_unit_id="resume.backend",
        run_ref=manifest.run_id,
        checkpoint_ref=str(tmp_path / "checkpoint.json"),
        run_id=manifest.run_id,
        run_root=manifest.run_root,
        attempt_id=attempt.attempt_id,
        runtime_backend="docker",
        checkpoint_store_dir=str(tmp_path / "checkpoints"),
    )
    capability_exchange = _capability_exchange()
    docker = _FakeDockerExecutor(
        capability_exchange=capability_exchange,
        resume_response=_solve_response(
            request_id=runtime_request.request_id,
            capability_exchange=capability_exchange,
            artifact={"status": "resumed"},
        ),
    )
    call_order: list[str] = []

    def resolve(resume_request, **kwargs):
        call_order.append("resolve")
        return runtime_request, preflight_request, manifest, attempt

    def preflight(runtime_dir, capability_exchange, request, **kwargs):
        call_order.append("preflight")
        assert request.runtime_backend == "docker"

    def fail_local(*args, **kwargs):
        raise AssertionError("local transport should not be used for a docker-backed resume")

    monkeypatch.setattr(host, "_resolve_runtime_resume_request", resolve)
    monkeypatch.setattr(host, "_docker_executor", lambda: docker)
    monkeypatch.setattr(host, "_run_local_inspect", fail_local)
    monkeypatch.setattr(host, "_run_local_resume", fail_local)
    monkeypatch.setattr(host, "_preflight_solve_contract", preflight)

    resumed = host.resume(
        "dummy-runtime",
        ResumeRequest(checkpoint_ref=str(tmp_path / "checkpoint.json")),
        provider=provider,
        runtime_profile=runtime_profile,
    )

    assert call_order[0] == "resolve"
    assert docker.inspect_requests[0][1].requested_backend == "docker"
    assert docker.resume_requests[0][1].runtime_backend == "docker"
    assert resumed.solve_result.artifact == {"status": "resumed"}

def test_runtime_host_resume_finalizes_attempt_on_protocol_mismatch(monkeypatch, tmp_path: Path):
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    monkeypatch.setattr(host.run_store, "prune_run", lambda manifest: manifest)
    runtime_profile = _runtime_profile()
    provider = build_provider(runtime_profile.runtime_provider.name, provider_profile=runtime_profile.runtime_provider)
    capability_exchange = _capability_exchange()
    monkeypatch.setattr(host, "inspect", lambda *args, **kwargs: capability_exchange)
    manifest = host.run_store.create_run(
        request_id="resume.protocol",
        evaluation_unit_id="resume.protocol",
        request_mode="user_request",
        runtime_backend="local",
    )
    attempt = host.run_store.begin_attempt(manifest, launch_kind="resume")
    preflight_request = RuntimeSolveRequest(
        request_id="resume.protocol",
        evaluation_unit_id="resume.protocol",
        runtime_backend="local",
        mode="user_request",
        seed=0,
        solve_request=load_solve_request(prompt="Resume hello."),
    )
    runtime_request = RuntimeResumeRequest(
        request_id="resume.protocol",
        evaluation_unit_id="resume.protocol",
        run_ref=manifest.run_id,
        checkpoint_ref=str(tmp_path / "checkpoint.json"),
        run_id=manifest.run_id,
        run_root=manifest.run_root,
        attempt_id=attempt.attempt_id,
        runtime_backend="local",
        checkpoint_store_dir=str(tmp_path / "checkpoints"),
    )
    monkeypatch.setattr(
        host,
        "_resolve_runtime_resume_request",
        lambda request, **kwargs: (runtime_request, preflight_request, manifest, attempt),
    )
    monkeypatch.setattr(host, "_preflight_solve_contract", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        host,
        "_run_local_resume",
        lambda *args, **kwargs: RuntimeSolveResponse(
            request_id="wrong.resume.request",
            capability_exchange=capability_exchange,
            solve_result=SolveResult(
                request_id="wrong.resume.request",
                runtime_hash="hash",
                mode="user_request",
                artifact={"status": "wrong"},
                status="best_effort",
                verification_status="best_effort",
                summary="ok",
                checks=[],
                budget={},
                provider_usage={},
                faults={"hard_invalid": False},
                verified=False,
                best_effort=True,
            ),
        ),
    )

    with pytest.raises(RuntimeLoadError, match="request_id mismatch"):
        host.resume(
            "dummy-runtime",
            ResumeRequest(checkpoint_ref=str(tmp_path / "checkpoint.json")),
            provider=provider,
            runtime_profile=runtime_profile,
        )

    manifest = host.run_store.load_run_manifest(manifest.run_id)
    attempt = host.run_store.load_attempt_manifest(manifest.run_id, attempt.attempt_id)
    assert manifest.lifecycle_state == "failed"
    assert manifest.last_failure_kind == "protocol_mismatch"
    assert attempt.lifecycle_state == "failed"
    assert attempt.failure_kind == "protocol_mismatch"

def test_runtime_host_resume_rejects_missing_runtime_resume_support(monkeypatch, tmp_path: Path):
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    monkeypatch.setattr(host.run_store, "prune_run", lambda manifest: manifest)
    runtime_profile = _runtime_profile()
    provider = build_provider(runtime_profile.runtime_provider.name, provider_profile=runtime_profile.runtime_provider)
    request = ResumeRequest(checkpoint_ref=str(tmp_path / "checkpoint.json"))
    manifest = host.run_store.create_run(
        request_id="resume.support",
        evaluation_unit_id="resume.support",
        request_mode="user_request",
        runtime_backend="local",
    )
    attempt = host.run_store.begin_attempt(manifest, launch_kind="resume")
    runtime_request = RuntimeResumeRequest(
        request_id="resume.support",
        evaluation_unit_id="resume.support",
        run_ref=manifest.run_id,
        checkpoint_ref=str(tmp_path / "checkpoint.json"),
        run_id=manifest.run_id,
        run_root=manifest.run_root,
        attempt_id=attempt.attempt_id,
        runtime_backend="local",
        checkpoint_store_dir=str(tmp_path / "checkpoints"),
    )
    original_request = RuntimeSolveRequest(
        request_id="resume.support",
        evaluation_unit_id="resume.support",
        runtime_backend="local",
        mode="user_request",
        seed=0,
        solve_request=load_solve_request(prompt="Resume hello."),
    )
    capability_exchange = _capability_exchange().model_copy(update={"resume_support": False})
    monkeypatch.setattr(
        host,
        "_resolve_runtime_resume_request",
        lambda request, **kwargs: (runtime_request, original_request, manifest, attempt),
    )
    monkeypatch.setattr(host, "inspect", lambda *args, **kwargs: capability_exchange)

    with pytest.raises(RuntimeLoadError, match="resume support"):
        host.resume("dummy-runtime", request, provider=provider, runtime_profile=runtime_profile)

    manifest = host.run_store.load_run_manifest(manifest.run_id)
    attempt = host.run_store.load_attempt_manifest(manifest.run_id, attempt.attempt_id)
    assert manifest.lifecycle_state == "failed"
    assert manifest.last_failure_kind == "resume_not_supported"
    assert attempt.lifecycle_state == "failed"
    assert attempt.failure_kind == "resume_not_supported"

def test_runtime_host_resume_finalizes_attempt_when_inspect_fails(monkeypatch, tmp_path: Path):
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    monkeypatch.setattr(host.run_store, "prune_run", lambda manifest: manifest)
    runtime_profile = _runtime_profile()
    provider = build_provider(runtime_profile.runtime_provider.name, provider_profile=runtime_profile.runtime_provider)
    manifest = host.run_store.create_run(
        request_id="resume.inspect-fail",
        evaluation_unit_id="resume.inspect-fail",
        request_mode="user_request",
        runtime_backend="local",
    )
    attempt = host.run_store.begin_attempt(manifest, launch_kind="resume")
    runtime_request = RuntimeResumeRequest(
        request_id="resume.inspect-fail",
        evaluation_unit_id="resume.inspect-fail",
        run_ref=manifest.run_id,
        checkpoint_ref=str(tmp_path / "checkpoint.json"),
        run_id=manifest.run_id,
        run_root=manifest.run_root,
        attempt_id=attempt.attempt_id,
        runtime_backend="local",
        checkpoint_store_dir=str(tmp_path / "checkpoints"),
    )
    original_request = RuntimeSolveRequest(
        request_id="resume.inspect-fail",
        evaluation_unit_id="resume.inspect-fail",
        runtime_backend="local",
        mode="user_request",
        seed=0,
        solve_request=load_solve_request(prompt="Resume hello."),
    )
    monkeypatch.setattr(
        host,
        "_resolve_runtime_resume_request",
        lambda request, **kwargs: (runtime_request, original_request, manifest, attempt),
    )
    monkeypatch.setattr(host, "inspect", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeLoadError("inspect failed")))

    with pytest.raises(RuntimeLoadError, match="inspect failed"):
        host.resume(
            "dummy-runtime",
            ResumeRequest(checkpoint_ref=str(tmp_path / "checkpoint.json")),
            provider=provider,
            runtime_profile=runtime_profile,
        )

    manifest = host.run_store.load_run_manifest(manifest.run_id)
    attempt = host.run_store.load_attempt_manifest(manifest.run_id, attempt.attempt_id)
    assert manifest.lifecycle_state == "failed"
    assert manifest.last_failure_kind == "host_launch_failure"
    assert attempt.lifecycle_state == "failed"
    assert attempt.failure_kind == "host_launch_failure"

def test_runtime_host_resolve_runtime_resume_request_preserves_checkpoint_store_dir(monkeypatch, tmp_path: Path):
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    manifest = RunManifest(
        run_id="run.resume.external",
        run_root=str((tmp_path / "host" / "runs" / "run.resume.external").resolve()),
        request_id="resume.external",
        request_mode="user_request",
        runtime_backend="local",
    )
    attempt = AttemptManifest(
        attempt_id="attempt_0001",
        run_id=manifest.run_id,
        run_root=manifest.run_root,
        sequence_no=1,
        launch_kind="resume",
        workspace_root=str((Path(manifest.run_root) / "attempts" / "attempt_0001" / "workspace").resolve()),
    )
    checkpoint_store_dir = (tmp_path / "external-store").resolve()
    checkpoint_store_dir.mkdir(parents=True)
    checkpoint_path = checkpoint_store_dir / "checkpoint.resume.external.json"
    checkpoint_path.write_text("{}", encoding="utf-8")
    request = runtime_solve_request_for_user_request(
        runtime_backend="local",
        seed=0,
        solve_request=load_solve_request(prompt="Resume hello."),
    )
    checkpoint_task = BenchmarkTask(
        task_id="resume.external.task",
        family="top",
        prompt="Resume hello.",
        task_type="structured_ops",
        operations=[],
        expected={},
    )
    checkpoint_plan = compile_execution_plan_from_task(
        checkpoint_task,
        request_id=request.request_id,
        seed=0,
        runtime_hash="runtime-hash",
        runtime_dir=str(tmp_path / "runtime"),
    )
    checkpoint_envelope = CheckpointEnvelope(
        checkpoint_id="checkpoint.resume.external",
        runtime_contract_version=RUNTIME_CONTRACT_VERSION,
        runtime_hash="runtime-hash",
        run_id=manifest.run_id,
        run_root=manifest.run_root,
        request_id=request.request_id,
        plan_id=checkpoint_plan.plan_id,
        task_id=checkpoint_task.task_id,
        seed=0,
        plan_snapshot=(checkpoint_plan).model_dump(),
        task_payload=(checkpoint_task).model_dump(),
    )
    monkeypatch.setattr(
        host.run_store,
        "resolve_resume_target",
        lambda **kwargs: type(
            "ResumeTarget",
            (),
            {
                "run_manifest": manifest,
                "checkpoint_path": checkpoint_path,
                "checkpoint_store_dir": checkpoint_store_dir,
            },
        )(),
    )
    monkeypatch.setattr(
        host.run_store,
        "load_request_bundle",
        lambda run_ref: {"request_kind": "runtime_solve_request", "payload": json.loads(json.dumps((request).model_dump()))},
    )
    monkeypatch.setattr(host.run_store, "load_checkpoint_envelope", lambda checkpoint_ref: checkpoint_envelope)
    monkeypatch.setattr(host.run_store, "begin_attempt", lambda *args, **kwargs: attempt)

    runtime_request, _, _, _ = host._resolve_runtime_resume_request(
        ResumeRequest(checkpoint_ref=str(checkpoint_path))
    )

    assert runtime_request.checkpoint_ref == str(checkpoint_path)
    assert runtime_request.checkpoint_store_dir == str(checkpoint_store_dir)
