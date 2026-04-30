from __future__ import annotations

import importlib
import json
from pathlib import Path
import sys

import pytest

import agintor.project as project
from agintor.exceptions import RuntimeLoadError
from agintor.providers import build_provider
from agintor.runtime_sdk import bundle_runtime_kernel
from agintor.runtime_api import (
    compile_execution_plan_from_task,
    load_solve_request,
    runtime_batch_request_for_tasks,
    runtime_solve_failure_response,
    runtime_solve_request_for_task,
    runtime_solve_request_for_user_request,
)
from agintor.runtime_host import RuntimeHost
from agintor.runtime_profile import RUNTIME_PROFILE_FILE, load_runtime_profile
from agintor.versioning import RUNTIME_CONTRACT_VERSION
from agintor.schemas import (
    AttemptManifest,
    BenchmarkTask,
    CapabilityExchange,
    CheckpointEnvelope,
    OpenAITraceContext,
    OperationSpec,
    ResumeRequest,
    RunManifest,
    RunResult,
    RuntimeBatchResponse,
    RuntimeIsolationPolicy,
    RuntimeResumeRequest,
    RuntimeSolveRequest,
    RuntimeSolveResponse,
    SolveResult,
)


def _capability_exchange() -> CapabilityExchange:
    provider_profile = _runtime_profile().runtime_provider
    credential_group = [
        name
        for name in [
            str(provider_profile.api_key_env or "").strip(),
            str(provider_profile.api_key_file_env or "").strip(),
        ]
        if name
    ]
    return CapabilityExchange(
        runtime_contract_version=RUNTIME_CONTRACT_VERSION,
        supported_backends=["local", "docker"],
        tool_runtimes=["python"],
        checkpoint_support=True,
        runtime_asset_capabilities={"traces": True, "checkpoints": True, "runtime_sdk": True},
        side_effect_receipts=True,
        resume_support=True,
        runtime_isolation_policy=RuntimeIsolationPolicy(required_guarantees=[]),
        supported_guarantees=[
            "timeout_enforcement",
            "workspace_isolation",
            "environment_filtering",
            "process_cleanup",
            "network_disablement",
        ],
        effective_guarantees=[
            "timeout_enforcement",
            "workspace_isolation",
            "environment_filtering",
        ],
        required_env_names=[],
        required_env_any_of=[credential_group] if credential_group else [],
        capability_flags=["inspect", "run_batch", "benchmark_mode", "prompt_mode"],
    )


def _runtime_profile():
    return load_runtime_profile()


def test_load_runtime_profile_accepts_legacy_provider_key_from_runtime_dir(tmp_path: Path):
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    (runtime_dir / RUNTIME_PROFILE_FILE).write_text(
        json.dumps({"provider": {"name": "local"}}, indent=2),
        encoding="utf-8",
    )

    profile = load_runtime_profile(runtime_dir)

    assert profile.runtime_provider.name == "local"
    assert profile.runtime_provider.api_key_env is None
    assert profile.runtime_provider.api_key_file_env is None
    assert profile.runtime_provider.base_url_env is None
    assert profile.runtime_provider.pricing_env is None
    assert profile.runtime_provider.model_map == {}
    assert profile.runtime_provider.reasoning_effort_map == {}
    assert profile.runtime_provider.pricing_map == {}


def test_load_runtime_profile_uses_minimax_defaults_for_legacy_provider_key(tmp_path: Path):
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    (runtime_dir / RUNTIME_PROFILE_FILE).write_text(
        json.dumps({"provider": {"name": "minimax"}}, indent=2),
        encoding="utf-8",
    )

    profile = load_runtime_profile(runtime_dir)

    assert profile.runtime_provider.name == "minimax"
    assert profile.runtime_provider.api_key_env == "AGINTOR_MAS_MINIMAX_API_KEY"
    assert profile.runtime_provider.api_key_file_env == "AGINTOR_MAS_MINIMAX_KEY_FILE"
    assert profile.runtime_provider.base_url is None
    assert profile.runtime_provider.base_url_env == "AGINTOR_MAS_MINIMAX_BASE_URL"
    assert profile.runtime_provider.pricing_env == "AGINTOR_MAS_MINIMAX_PRICING"
    assert profile.runtime_provider.model_map == {
        "small": "MiniMax-M2.7-Flash",
        "medium": "MiniMax-M2.7-Flash",
        "large": "MiniMax-M2.7-Flash",
    }
    assert not any(
        "OPENAI" in str(value)
        for value in [
            profile.runtime_provider.api_key_env,
            profile.runtime_provider.api_key_file_env,
            profile.runtime_provider.base_url_env,
            profile.runtime_provider.pricing_env,
            *profile.runtime_provider.model_map.values(),
        ]
    )


def test_load_runtime_profile_prefers_runtime_provider_over_legacy_provider(tmp_path: Path):
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "provider": {"name": "local"},
                "runtime_provider": {
                    "name": "minimax",
                    "api_key_env": "EXPLICIT_MINIMAX_KEY",
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    profile = load_runtime_profile(profile_path=profile_path)

    assert profile.runtime_provider.name == "minimax"
    assert profile.runtime_provider.api_key_env == "EXPLICIT_MINIMAX_KEY"
    assert profile.runtime_provider.api_key_file_env == "AGINTOR_MAS_MINIMAX_KEY_FILE"
    assert profile.runtime_provider.base_url is None
    assert profile.runtime_provider.base_url_env == "AGINTOR_MAS_MINIMAX_BASE_URL"
    assert profile.runtime_provider.pricing_env == "AGINTOR_MAS_MINIMAX_PRICING"
    assert "OPENAI" not in str(profile.runtime_provider.model_map)


def test_legacy_minimax_profile_allows_base_url_env_override(tmp_path: Path, monkeypatch):
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    (runtime_dir / RUNTIME_PROFILE_FILE).write_text(
        json.dumps({"provider": {"name": "minimax"}}, indent=2),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGINTOR_MAS_MINIMAX_BASE_URL", "https://minimax.example.test/anthropic")

    profile = load_runtime_profile(runtime_dir)
    provider = build_provider(profile.runtime_provider.name, provider_profile=profile.runtime_provider)

    assert profile.runtime_provider.base_url is None
    assert provider.base_url == "https://minimax.example.test/anthropic"


def test_baseline_runtime_templates_ship_with_package() -> None:
    """Regression: package-data templates must exist for init_runtime and wheel installs."""
    root = importlib.resources.files("agintor").joinpath("templates", "baseline_runtime")
    assert root.is_dir()
    assert (root / RUNTIME_PROFILE_FILE).is_file()
    assert (root / "deployment_contract.json").is_file()


def test_init_runtime_refreshes_runtime_manifest_contract_version(monkeypatch, tmp_path: Path):
    current_contract_version = f"{RUNTIME_CONTRACT_VERSION}.test"
    monkeypatch.setattr(project, "RUNTIME_CONTRACT_VERSION", current_contract_version)

    runtime_dir = project.init_runtime(tmp_path / "runtime")

    manifest = json.loads((runtime_dir / "runtime_manifest.json").read_text(encoding="utf-8"))
    contract = json.loads((runtime_dir / "deployment_contract.json").read_text(encoding="utf-8"))

    assert manifest["metadata"]["runtime_contract_version"] == current_contract_version
    assert contract["runtime_contract_version"] == current_contract_version


def test_refresh_deployment_contract_does_not_require_openai_credentials_for_legacy_local_profile(tmp_path: Path):
    runtime_dir = project.init_runtime(tmp_path / "runtime")
    (runtime_dir / RUNTIME_PROFILE_FILE).write_text(
        json.dumps({"provider": {"name": "local"}}, indent=2),
        encoding="utf-8",
    )

    project._refresh_deployment_contract(runtime_dir)

    contract = json.loads((runtime_dir / "deployment_contract.json").read_text(encoding="utf-8"))
    assert contract["required_env_any_of"] == []
    assert "OPENAI_API_KEY" not in contract["environment_allowlist"]
    assert "AGINTOR_OPENAI_KEY_FILE" not in contract["environment_allowlist"]


def _clear_runtime_provider_env(monkeypatch) -> None:
    monkeypatch.delenv("AGINTOR_MAS_MINIMAX_API_KEY", raising=False)
    monkeypatch.delenv("AGINTOR_MAS_MINIMAX_KEY_FILE", raising=False)


def _make_repo_patch_task(task_id: str) -> BenchmarkTask:
    return BenchmarkTask(
        task_id=task_id,
        family="tool",
        prompt="Update README.md.",
        task_type="bounded_repo_patch",
        file_paths=["README.md"],
        allowed_tool_categories=["filesystem/read", "filesystem/patch"],
        context_items=[],
        operations=[
            OperationSpec(
                op_id="apply_patch",
                kind="repo_patch",
                output_key="patch_result",
                description="Update README.md.",
                args={
                    "request_id": f"{task_id}.request",
                    "output_schema": {},
                    "target_file_paths": ["README.md"],
                },
                externally_visible=True,
            )
        ],
        expected=None,
        verifier_type="none",
        externally_visible=True,
        verification_required=False,
        allow_best_effort=True,
    )


def test_task_runtime_facade_is_exported_in_bundled_kernel(tmp_path: Path):
    from agintor.runner import TaskRuntime as HostTaskRuntime

    runtime_dir = tmp_path / "runtime"
    manifest = bundle_runtime_kernel(runtime_dir, force=True)
    sdk_path = str((runtime_dir / "runtime_sdk").resolve())

    assert HostTaskRuntime.__name__ == "TaskRuntime"
    assert hasattr(HostTaskRuntime, "run_task")
    assert hasattr(HostTaskRuntime, "resume_from_checkpoint")
    assert hasattr(HostTaskRuntime, "_run_branch_plan")
    assert hasattr(HostTaskRuntime, "_execute_isolated_frame")
    assert "agintor_runtime/task_runtime/base.py" in manifest.files
    assert "agintor_runtime/task_runtime/branch_execution.py" in manifest.files

    for module_name in list(sys.modules):
        if module_name == "agintor_runtime" or module_name.startswith("agintor_runtime."):
            del sys.modules[module_name]
    sys.path.insert(0, sdk_path)
    try:
        bundled_runner = importlib.import_module("agintor_runtime.runner")
        bundled_task_runtime = bundled_runner.TaskRuntime
        assert bundled_task_runtime.__name__ == "TaskRuntime"
        assert hasattr(bundled_task_runtime, "run_task")
        assert hasattr(bundled_task_runtime, "resume_from_checkpoint")
        assert hasattr(bundled_task_runtime, "_run_branch_plan")
        assert hasattr(bundled_task_runtime, "_execute_isolated_frame")
    finally:
        sys.path.remove(sdk_path)
        for module_name in list(sys.modules):
            if module_name == "agintor_runtime" or module_name.startswith("agintor_runtime."):
                del sys.modules[module_name]


class _FakeDockerExecutor:
    def __init__(
        self,
        *,
        capability_exchange: CapabilityExchange,
        solve_response: RuntimeSolveResponse | None = None,
        batch_response: RuntimeBatchResponse | None = None,
        resume_response: RuntimeSolveResponse | None = None,
        batch_handler=None,
    ) -> None:
        self.capability_exchange = capability_exchange
        self.solve_response = solve_response
        self.batch_response = batch_response
        self.resume_response = resume_response
        self.batch_handler = batch_handler
        self.inspect_requests: list[tuple[object, object]] = []
        self.solve_requests: list[tuple[object, object]] = []
        self.batch_requests: list[tuple[object, object]] = []
        self.resume_requests: list[tuple[object, object]] = []

    def inspect(self, runtime_dir, request):
        self.inspect_requests.append((runtime_dir, request))
        return self.capability_exchange

    def solve_protocol(self, runtime_dir, request, **kwargs):
        self.solve_requests.append((runtime_dir, request))
        assert self.solve_response is not None
        return self.solve_response

    def run_batch_protocol(self, runtime_dir, request, **kwargs):
        self.batch_requests.append((runtime_dir, request))
        if self.batch_handler is not None:
            return self.batch_handler(runtime_dir, request)
        assert self.batch_response is not None
        return self.batch_response

    def resume_protocol(self, runtime_dir, request, **kwargs):
        self.resume_requests.append((runtime_dir, request))
        assert self.resume_response is not None
        return self.resume_response


def _solve_response(
    *,
    request_id: str,
    capability_exchange: CapabilityExchange,
    artifact,
    mode: str = "user_request",
) -> RuntimeSolveResponse:
    return RuntimeSolveResponse(
        request_id=request_id,
        capability_exchange=capability_exchange,
        solve_result=SolveResult(
            request_id=request_id,
            runtime_hash="hash",
            mode=mode,
            artifact=artifact,
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

    monkeypatch.setattr("agintor.runtime_host.runtime_batch_request_for_tasks", build_docker_batch_request)
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

    monkeypatch.setattr("agintor.runtime_host.runtime_batch_request_for_tasks", build_mixed_batch_request)
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
