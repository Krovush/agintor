from __future__ import annotations

from pathlib import Path

import pytest

from agintor.core.exceptions import RuntimeLoadError
from agintor.providers import build_provider
from agintor.runtime.api import load_solve_request, runtime_solve_request_for_task, runtime_solve_request_for_user_request
from agintor.runtime.host import RuntimeHost
from agintor.contracts import (
    BenchmarkTask,
    RunResult,
    RuntimeIsolationPolicy,
    RuntimeSolveRequest,
    RuntimeSolveResponse,
    SolveResult,
)

from ._helpers import (
    _FakeDockerExecutor,
    _capability_exchange,
    _clear_runtime_provider_env,
    _runtime_profile,
    _solve_response,
)


def test_solve_preflight_rejects_missing_runtime_credential_group_for_direct_response(monkeypatch, tmp_path: Path):
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    _clear_runtime_provider_env(monkeypatch)
    runtime_profile = _runtime_profile()
    provider = build_provider(runtime_profile.runtime_provider.name, provider_profile=runtime_profile.runtime_provider)
    request = runtime_solve_request_for_user_request(
        runtime_backend="local",
        seed=0,
        solve_request=load_solve_request(prompt="Say hello."),
    )
    monkeypatch.setattr(host, "inspect", lambda *args, **kwargs: _capability_exchange())
    called = {"solve": False}

    def fail_if_called(*args, **kwargs):
        called["solve"] = True
        raise AssertionError("solve launch should not happen when required credential groups are missing")

    monkeypatch.setattr(host, "_run_local_solve", fail_if_called)

    with pytest.raises(RuntimeLoadError, match="one of .*"):
        host.solve("dummy-runtime", request, provider=provider, runtime_profile=runtime_profile)
    assert called["solve"] is False

def test_solve_preflight_allows_builtin_prompt_plan_without_runtime_credentials(monkeypatch, tmp_path: Path):
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    _clear_runtime_provider_env(monkeypatch)
    runtime_profile = _runtime_profile()
    provider = build_provider(runtime_profile.runtime_provider.name, provider_profile=runtime_profile.runtime_provider)
    request = runtime_solve_request_for_user_request(
        runtime_backend="local",
        seed=0,
        solve_request=load_solve_request(
            prompt="Given the numbers [2, 3, 5], compute the sum and product and return JSON with keys sum and product."
        ),
    )
    capability_exchange = _capability_exchange()
    monkeypatch.setattr(host, "inspect", lambda *args, **kwargs: capability_exchange)
    called = {"solve": False}
    response = RuntimeSolveResponse(
        request_id=request.request_id,
        capability_exchange=capability_exchange,
        solve_result=SolveResult(
            request_id=request.request_id,
            runtime_hash="hash",
            mode="user_request",
            artifact={"sum": 10, "product": 30},
            status="verified",
            verification_status="verified",
            summary="ok",
            checks=[],
            budget={},
            provider_usage={},
            faults={"hard_invalid": False},
            verified=True,
            best_effort=False,
        ),
    )

    def succeed(*args, **kwargs):
        called["solve"] = True
        return response

    monkeypatch.setattr(host, "_run_local_solve", succeed)

    solved = host.solve("dummy-runtime", request, provider=provider, runtime_profile=runtime_profile)
    assert called["solve"] is True
    assert solved.solve_result.artifact == {"sum": 10, "product": 30}


def test_host_solve_rescores_private_benchmark_with_recorded_trace(monkeypatch, tmp_path: Path):
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    _clear_runtime_provider_env(monkeypatch)
    runtime_profile = _runtime_profile()
    provider = build_provider(runtime_profile.runtime_provider.name, provider_profile=runtime_profile.runtime_provider)
    task = BenchmarkTask(
        task_id="tool.sealed.trace",
        family="tool",
        prompt="Emit the sealed trace event.",
        task_type="trace",
        expected=None,
        private_expected="sealed_trace_event",
        verifier_type="trace_event",
    )
    request = runtime_solve_request_for_task(runtime_backend="local", seed=0, task=task)
    capability_exchange = _capability_exchange()
    monkeypatch.setattr(host, "inspect", lambda *args, **kwargs: capability_exchange)

    def local_solve(_runtime_dir, runtime_request, **kwargs):
        assert runtime_request.task is not None
        assert runtime_request.task.expected is None
        assert runtime_request.task.private_expected is None
        assert runtime_request.task.verifier_type == "none"
        return RuntimeSolveResponse(
            request_id=runtime_request.request_id,
            capability_exchange=capability_exchange,
            solve_result=SolveResult(
                request_id=runtime_request.request_id,
                runtime_hash="hash",
                mode="benchmark",
                artifact={"ok": True},
                status="best_effort",
                verification_status="best_effort",
                summary="runtime-visible task had no exact verifier",
                checks=[],
                budget={},
                provider_usage={},
                faults={"hard_invalid": False},
                trace_ref=RunResult.encode_trace_ref([{"event": "sealed_trace_event"}]),
                verified=False,
                best_effort=True,
            ),
        )

    monkeypatch.setattr(host, "_run_local_solve", local_solve)

    solved = host.solve("dummy-runtime", request, provider=provider, runtime_profile=runtime_profile)

    assert solved.solve_result.status == "verified"
    assert solved.solve_result.verified is True

def test_solve_preflight_allows_local_only_symbol_lookup_with_context_items_without_runtime_credentials(
    monkeypatch,
    tmp_path: Path,
):
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    _clear_runtime_provider_env(monkeypatch)
    runtime_profile = _runtime_profile()
    provider = build_provider(runtime_profile.runtime_provider.name, provider_profile=runtime_profile.runtime_provider)
    solve_request = load_solve_request(prompt="What is the value of MEMORY_ALPHA?")
    solve_request.context_items = [
        {"symbol": "MEMORY_ALPHA", "value": "alpha-value", "blob": "x" * 600},
        {"note": "x" * 600},
        {"note": "x" * 600},
    ]
    solve_request.budget_overrides["context_window_tokens"] = 32
    request = runtime_solve_request_for_user_request(
        runtime_backend="local",
        seed=0,
        solve_request=solve_request,
    )
    monkeypatch.setattr(host, "inspect", lambda *args, **kwargs: _capability_exchange())
    called = {"solve": False}
    response = RuntimeSolveResponse(
        request_id=request.request_id,
        capability_exchange=_capability_exchange(),
        solve_result=SolveResult(
            request_id=request.request_id,
            runtime_hash="hash",
            mode="user_request",
            artifact={"answer": "alpha-value"},
            status="verified",
            verification_status="verified",
            summary="ok",
            checks=[],
            budget={},
            provider_usage={},
            faults={"hard_invalid": False},
            verified=True,
            best_effort=False,
        ),
    )

    def succeed(*args, **kwargs):
        called["solve"] = True
        return response

    monkeypatch.setattr(host, "_run_local_solve", succeed)

    solved = host.solve("dummy-runtime", request, provider=provider, runtime_profile=runtime_profile)
    assert called["solve"] is True
    assert solved.solve_result.artifact == {"answer": "alpha-value"}

def test_solve_preflight_accepts_resolved_provider_credentials_from_api_key_file(monkeypatch, tmp_path: Path):
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    _clear_runtime_provider_env(monkeypatch)
    runtime_profile = _runtime_profile()
    key_file = tmp_path / "minimax.key"
    key_file.write_text("test-minimax-key\n", encoding="utf-8")
    provider = build_provider(
        runtime_profile.runtime_provider.name,
        provider_profile=runtime_profile.runtime_provider,
        api_key_file=str(key_file),
    )
    request = runtime_solve_request_for_user_request(
        runtime_backend="local",
        seed=0,
        solve_request=load_solve_request(prompt="Say hello."),
    )
    capability_exchange = _capability_exchange()
    monkeypatch.setattr(host, "inspect", lambda *args, **kwargs: capability_exchange)
    called = {"solve": False}
    response = RuntimeSolveResponse(
        request_id=request.request_id,
        capability_exchange=capability_exchange,
        solve_result=SolveResult(
            request_id=request.request_id,
            runtime_hash="hash",
            mode="user_request",
            artifact="hello",
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

    def succeed(*args, **kwargs):
        called["solve"] = True
        return response

    monkeypatch.setattr(host, "_run_local_solve", succeed)

    solved = host.solve("dummy-runtime", request, provider=provider, runtime_profile=runtime_profile)
    assert called["solve"] is True
    assert solved.solve_result.status == "best_effort"
    assert solved.solve_result.artifact == "hello"

def test_runtime_host_solve_creates_durable_run_root_and_returns_identity(monkeypatch, tmp_path: Path):
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    runtime_profile = _runtime_profile()
    provider = build_provider(runtime_profile.runtime_provider.name, provider_profile=runtime_profile.runtime_provider)
    request = runtime_solve_request_for_user_request(
        runtime_backend="local",
        seed=0,
        solve_request=load_solve_request(prompt="Say hello."),
    )
    capability_exchange = _capability_exchange()
    monkeypatch.setattr(host, "inspect", lambda *args, **kwargs: capability_exchange)
    captured: dict[str, RuntimeSolveRequest] = {}
    response = RuntimeSolveResponse(
        request_id=request.request_id,
        capability_exchange=capability_exchange,
        solve_result=SolveResult(
            request_id=request.request_id,
            runtime_hash="hash",
            mode="user_request",
            artifact={"status": "solved"},
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

    def succeed(runtime_dir, runtime_request, **kwargs):
        captured["request"] = runtime_request
        return response

    monkeypatch.setattr(host, "_preflight_solve_contract", lambda *args, **kwargs: None)
    monkeypatch.setattr(host, "_run_local_solve", succeed)
    solved = host.solve("dummy-runtime", request, provider=provider, runtime_profile=runtime_profile)
    assert isinstance(captured["request"], RuntimeSolveRequest)
    assert captured["request"].run_id
    assert captured["request"].run_root
    assert captured["request"].attempt_id == "attempt_0001"
    assert solved.solve_result.run_id == captured["request"].run_id
    assert solved.solve_result.run_root == captured["request"].run_root
    assert solved.solve_result.attempt_id == "attempt_0001"

def test_runtime_host_solve_finalization_preserves_external_checkpoint_ref(monkeypatch, tmp_path: Path):
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    runtime_profile = _runtime_profile()
    provider = build_provider(runtime_profile.runtime_provider.name, provider_profile=runtime_profile.runtime_provider)
    request = runtime_solve_request_for_user_request(
        runtime_backend="local",
        seed=0,
        solve_request=load_solve_request(prompt="Say hello."),
    )
    capability_exchange = _capability_exchange()
    external_checkpoint_ref = str((tmp_path / "external-checkpoints" / "checkpoint.solve.json").resolve())
    monkeypatch.setattr(host, "inspect", lambda *args, **kwargs: capability_exchange)
    monkeypatch.setattr(host.run_store, "latest_usable_checkpoint_ref", lambda run_ref: None)
    response = RuntimeSolveResponse(
        request_id=request.request_id,
        capability_exchange=capability_exchange,
        solve_result=SolveResult(
            request_id=request.request_id,
            runtime_hash="hash",
            latest_checkpoint_ref=external_checkpoint_ref,
            checkpoint_ref=external_checkpoint_ref,
            run_lifecycle_state="paused",
            run_resumable=True,
            run_prune_eligible=False,
            mode="user_request",
            artifact={"status": "checkpointed"},
            status="best_effort",
            verification_status="best_effort",
            summary="paused with checkpoint",
            checks=[],
            budget={},
            provider_usage={},
            faults={"hard_invalid": False},
            verified=False,
            best_effort=True,
        ),
    )

    monkeypatch.setattr(host, "_preflight_solve_contract", lambda *args, **kwargs: None)
    monkeypatch.setattr(host, "_run_local_solve", lambda *args, **kwargs: response)

    solved = host.solve("dummy-runtime", request, provider=provider, runtime_profile=runtime_profile)
    manifest = host.run_store.load_run_manifest(solved.solve_result.run_id)

    assert manifest.lifecycle_state == "paused"
    assert manifest.latest_checkpoint_ref == external_checkpoint_ref
    assert solved.solve_result.latest_checkpoint_ref == external_checkpoint_ref
    assert solved.solve_result.checkpoint_ref == external_checkpoint_ref
    assert solved.solve_result.run_resumable is True

def test_runtime_host_solve_uses_requested_docker_backend_on_local_default_host(monkeypatch, tmp_path: Path):
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    monkeypatch.setattr(host.run_store, "prune_run", lambda manifest: manifest)
    runtime_profile = _runtime_profile()
    provider = build_provider(runtime_profile.runtime_provider.name, provider_profile=runtime_profile.runtime_provider)
    request = runtime_solve_request_for_user_request(
        runtime_backend="docker",
        seed=0,
        solve_request=load_solve_request(prompt="Say hello."),
    )
    capability_exchange = _capability_exchange()
    docker = _FakeDockerExecutor(
        capability_exchange=capability_exchange,
        solve_response=_solve_response(
            request_id=request.request_id,
            capability_exchange=capability_exchange,
            artifact={"status": "docker"},
        ),
    )

    def fail_local(*args, **kwargs):
        raise AssertionError("local transport should not be used for a docker-selected solve")

    monkeypatch.setattr(host, "_docker_executor", lambda: docker)
    monkeypatch.setattr(host, "_run_local_inspect", fail_local)
    monkeypatch.setattr(host, "_run_local_solve", fail_local)
    monkeypatch.setattr(host, "_preflight_solve_contract", lambda *args, **kwargs: None)

    solved = host.solve("dummy-runtime", request, provider=provider, runtime_profile=runtime_profile)
    manifest = host.run_store.load_run_manifest(solved.solve_result.run_id)

    assert docker.inspect_requests[0][1].requested_backend == "docker"
    assert docker.solve_requests[0][1].runtime_backend == "docker"
    assert manifest.runtime_backend == "docker"
    assert solved.solve_result.artifact == {"status": "docker"}

def test_runtime_host_solve_uses_requested_local_backend_on_docker_default_host(monkeypatch, tmp_path: Path):
    host = RuntimeHost(tmp_path / "host", runtime_backend="docker")
    monkeypatch.setattr(host.run_store, "prune_run", lambda manifest: manifest)
    runtime_profile = _runtime_profile()
    provider = build_provider(runtime_profile.runtime_provider.name, provider_profile=runtime_profile.runtime_provider)
    request = runtime_solve_request_for_user_request(
        runtime_backend="local",
        seed=0,
        solve_request=load_solve_request(prompt="Say hello."),
    )
    capability_exchange = _capability_exchange()
    captured: dict[str, str] = {}

    def local_inspect(runtime_dir, inspect_request, *, runtime_backend):
        captured["inspect_backend"] = runtime_backend
        captured["inspect_request_backend"] = inspect_request.requested_backend
        return capability_exchange

    def local_solve(runtime_dir, runtime_request, *, runtime_backend, **kwargs):
        captured["solve_backend"] = runtime_backend
        captured["solve_request_backend"] = runtime_request.runtime_backend
        return _solve_response(
            request_id=runtime_request.request_id,
            capability_exchange=capability_exchange,
            artifact={"status": "local"},
        )

    def fail_docker():
        raise AssertionError("docker transport should not be used for a local-selected solve")

    monkeypatch.setattr(host, "_run_local_inspect", local_inspect)
    monkeypatch.setattr(host, "_run_local_solve", local_solve)
    monkeypatch.setattr(host, "_docker_executor", fail_docker)
    monkeypatch.setattr(host, "_preflight_solve_contract", lambda *args, **kwargs: None)

    solved = host.solve("dummy-runtime", request, provider=provider, runtime_profile=runtime_profile)
    manifest = host.run_store.load_run_manifest(solved.solve_result.run_id)

    assert captured["inspect_backend"] == "local"
    assert captured["inspect_request_backend"] == "local"
    assert captured["solve_backend"] == "local"
    assert captured["solve_request_backend"] == "local"
    assert host._runtime_env(Path("dummy-runtime"), "local")["AGINTOR_RUNTIME_BACKEND"] == "local"
    assert manifest.runtime_backend == "local"
    assert solved.solve_result.artifact == {"status": "local"}

def test_runtime_host_solve_finalizes_attempt_on_capability_drift(monkeypatch, tmp_path: Path):
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    monkeypatch.setattr(host.run_store, "prune_run", lambda manifest: manifest)
    runtime_profile = _runtime_profile()
    provider = build_provider(runtime_profile.runtime_provider.name, provider_profile=runtime_profile.runtime_provider)
    capability_exchange = _capability_exchange()
    request = runtime_solve_request_for_user_request(
        runtime_backend="local",
        seed=0,
        solve_request=load_solve_request(prompt="Say hello."),
    )
    monkeypatch.setattr(host, "inspect", lambda *args, **kwargs: capability_exchange)
    captured: dict[str, str] = {}

    def drift(runtime_dir, runtime_request, **kwargs):
        captured["run_id"] = runtime_request.run_id
        captured["attempt_id"] = runtime_request.attempt_id
        return RuntimeSolveResponse(
            request_id=runtime_request.request_id,
            capability_exchange=capability_exchange.model_copy(update={"effective_guarantees": ["network_disablement"]}),
            solve_result=SolveResult(
                request_id=runtime_request.request_id,
                runtime_hash="hash",
                mode="user_request",
                artifact={"status": "drifted"},
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

    monkeypatch.setattr(host, "_preflight_solve_contract", lambda *args, **kwargs: None)
    monkeypatch.setattr(host, "_run_local_solve", drift)

    with pytest.raises(RuntimeLoadError, match="capability exchange changed"):
        host.solve("dummy-runtime", request, provider=provider, runtime_profile=runtime_profile)

    manifest = host.run_store.load_run_manifest(captured["run_id"])
    attempt = host.run_store.load_attempt_manifest(manifest.run_id, captured["attempt_id"])
    assert manifest.lifecycle_state == "failed"
    assert manifest.last_failure_kind == "capability_drift"
    assert attempt.lifecycle_state == "failed"
    assert attempt.failure_kind == "capability_drift"

def test_runtime_guarantee_preflight_rejects_missing_required_guarantee(tmp_path: Path):
    capability_exchange = _capability_exchange().model_copy(
        update={
            "runtime_isolation_policy": RuntimeIsolationPolicy(required_guarantees=["network_disablement"]),
            "effective_guarantees": [
                "timeout_enforcement",
                "workspace_isolation",
                "environment_filtering",
                "process_cleanup",
            ],
        }
    )
    with pytest.raises(RuntimeLoadError, match="network_disablement"):
        RuntimeHost._preflight_runtime_guarantees(tmp_path / "runtime", capability_exchange)

def test_solve_preflight_rejects_network_incompatible_service_action_before_run_creation(monkeypatch, tmp_path: Path):
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    runtime_profile = _runtime_profile()
    provider = build_provider(runtime_profile.runtime_provider.name, provider_profile=runtime_profile.runtime_provider)
    request = runtime_solve_request_for_user_request(
        runtime_backend="local",
        seed=0,
        solve_request=load_solve_request(prompt="GET https://service.example.test/status"),
    )
    capability_exchange = _capability_exchange().model_copy(
        update={
            "runtime_isolation_policy": RuntimeIsolationPolicy(
                required_guarantees=[],
                network_policy="provider-only",
                filesystem_policy="workspace-read-write",
            )
        }
    )
    monkeypatch.setattr(host, "inspect", lambda *args, **kwargs: capability_exchange)
    called = {"solve": False}

    def fail_if_called(*args, **kwargs):
        called["solve"] = True
        raise AssertionError("solve launch should not happen when the compiled plan exceeds runtime network policy")

    monkeypatch.setattr(host, "_run_local_solve", fail_if_called)

    with pytest.raises(RuntimeLoadError, match="network/service transport 'http'"):
        host.solve("dummy-runtime", request, provider=provider, runtime_profile=runtime_profile)

    assert called["solve"] is False
    assert list((tmp_path / "host" / "runs").glob("run.*")) == []

def test_solve_preflight_rejects_invalid_service_action_prompt_before_run_creation(monkeypatch, tmp_path: Path):
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    runtime_profile = _runtime_profile()
    provider = build_provider(runtime_profile.runtime_provider.name, provider_profile=runtime_profile.runtime_provider)
    request = runtime_solve_request_for_user_request(
        runtime_backend="local",
        seed=0,
        solve_request=load_solve_request(prompt="GET file:///tmp/secret.txt"),
    )
    capability_exchange = _capability_exchange()
    monkeypatch.setattr(host, "inspect", lambda *args, **kwargs: capability_exchange)
    called = {"solve": False}

    def fail_if_called(*args, **kwargs):
        called["solve"] = True
        raise AssertionError("solve launch should not happen when prompt adaptation compiles an invalid service_action")

    monkeypatch.setattr(host, "_run_local_solve", fail_if_called)

    with pytest.raises(RuntimeLoadError, match="only permits URL schemes"):
        host.solve("dummy-runtime", request, provider=provider, runtime_profile=runtime_profile)

    assert called["solve"] is False
    assert list((tmp_path / "host" / "runs").glob("run.*")) == []
