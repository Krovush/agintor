from __future__ import annotations

from pathlib import Path

import pytest

from agintor.exceptions import RuntimeLoadError
from agintor.providers import build_provider
from agintor.runtime_api import load_solve_request, runtime_solve_failure_response, runtime_solve_request_for_user_request
from agintor.runtime_host import RuntimeHost
from agintor.runtime_profile import load_runtime_profile
from agintor.schemas import CapabilityExchange, RuntimeSolveResponse, SolveResult


def _capability_exchange() -> CapabilityExchange:
    return CapabilityExchange(
        runtime_abi="agintor-runtime-abi-v3",
        kernel_version="agintor-kernel-v1",
        storage_schema_version="agintor-storage-v1",
        supported_backends=["local", "docker"],
        tool_runtimes=["python"],
        checkpoint_support=True,
        runtime_asset_capabilities={"traces": True, "checkpoints": True, "runtime_sdk": True},
        side_effect_receipts=False,
        required_env_names=[],
        required_env_any_of=[["AGINTOR_MAS_MINIMAX_API_KEY", "AGINTOR_MAS_MINIMAX_KEY_FILE"]],
        capability_flags=["inspect", "run_batch", "benchmark_mode", "prompt_mode"],
    )


def _runtime_profile():
    return load_runtime_profile()


def _clear_runtime_provider_env(monkeypatch) -> None:
    monkeypatch.delenv("AGINTOR_MAS_MINIMAX_API_KEY", raising=False)
    monkeypatch.delenv("AGINTOR_MAS_MINIMAX_KEY_FILE", raising=False)


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
    monkeypatch.setattr(host, "inspect", lambda runtime_dir: _capability_exchange())
    called = {"solve": False}

    def fail_if_called(*args, **kwargs):
        called["solve"] = True
        raise AssertionError("solve launch should not happen when required credential groups are missing")

    monkeypatch.setattr(host, "_run_local_solve", fail_if_called)

    with pytest.raises(RuntimeLoadError, match="one of AGINTOR_MAS_MINIMAX_API_KEY, AGINTOR_MAS_MINIMAX_KEY_FILE"):
        host.solve("dummy-runtime", request, provider=provider, runtime_profile=runtime_profile)
    assert called["solve"] is False


def test_solve_preflight_allows_deterministic_prompt_without_runtime_credentials(monkeypatch, tmp_path: Path):
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
    monkeypatch.setattr(host, "inspect", lambda runtime_dir: capability_exchange)
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
            recoverability="terminal",
            verified=True,
            best_effort=False,
        ),
    )
    monkeypatch.setattr(host, "_run_local_solve", lambda *args, **kwargs: response)

    solved = host.solve("dummy-runtime", request, provider=provider, runtime_profile=runtime_profile)
    assert solved.solve_result.verified is True
    assert solved.solve_result.artifact == {"sum": 10, "product": 30}


def test_solve_preflight_rejects_prompt_mode_memory_compaction_without_runtime_credentials(monkeypatch, tmp_path: Path):
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
    monkeypatch.setattr(host, "inspect", lambda runtime_dir: _capability_exchange())
    called = {"solve": False}

    def fail_if_called(*args, **kwargs):
        called["solve"] = True
        raise AssertionError("solve launch should not happen when prompt-mode hosted credentials are missing")

    monkeypatch.setattr(host, "_run_local_solve", fail_if_called)

    with pytest.raises(RuntimeLoadError, match="one of AGINTOR_MAS_MINIMAX_API_KEY, AGINTOR_MAS_MINIMAX_KEY_FILE"):
        host.solve("dummy-runtime", request, provider=provider, runtime_profile=runtime_profile)
    assert called["solve"] is False


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
    monkeypatch.setattr(host, "inspect", lambda runtime_dir: capability_exchange)
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
            recoverability="terminal",
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
