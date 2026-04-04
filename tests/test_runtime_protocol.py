from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import agintor.container_runtime as container_runtime_module

from agintor.benchmarks import build_demo_suite
from agintor.container_runtime import DockerRuntimeExecutor
from agintor.providers import LocalDeterministicProvider
from agintor.pydantic_compat import model_dump
from agintor.runtime_api import solve_result_from_run_result_with_context
from agintor.runtime_profile import default_runtime_profile
from agintor.runtime_sdk import KERNEL_BUNDLE_DIR
from agintor.schemas import (
    CapabilityExchange,
    RunResult,
    RuntimeBatchRequest,
    RuntimeBatchResponse,
    RuntimeTaskInvocation,
    SolveRequest,
)


def _capability() -> CapabilityExchange:
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
        capability_flags=["inspect", "run_batch", "checkpoint_refs"],
    )


def _bundled_runtime_entry(runtime_dir: Path):
    bundle_root = (runtime_dir / "runtime_sdk").resolve()
    previous_sys_path = list(sys.path)
    try:
        sys.path.insert(0, str(bundle_root))
        removable = [name for name in sys.modules if name == "agintor_runtime" or name.startswith("agintor_runtime.")]
        for name in removable:
            sys.modules.pop(name, None)
        return importlib.import_module("agintor_runtime.runtime_entry")
    finally:
        sys.path[:] = previous_sys_path


def test_runtime_entry_passes_budget_overrides_to_task_runtime(runtime_dir: Path, tmp_path: Path, monkeypatch) -> None:
    runtime_entry_module = _bundled_runtime_entry(runtime_dir)
    task = build_demo_suite().by_id("top.sum_product")
    request = RuntimeBatchRequest(
        request_id="batch.runtime-entry",
        runtime_backend="local",
        invocations=[RuntimeTaskInvocation(seed=0, task=task)],
        budget_overrides={"M_max": 2, "Q_max": 1},
    )
    input_json = tmp_path / "request.json"
    provider_json = tmp_path / "provider.json"
    output_json = tmp_path / "response.json"
    input_json.write_text(json.dumps(model_dump(request), indent=2, sort_keys=True), encoding="utf-8")
    provider_json.write_text(json.dumps({"kind": "local"}, indent=2, sort_keys=True), encoding="utf-8")
    captured: dict[str, object] = {}

    class FakeTaskRuntime:
        def __init__(self, runtime, shell, provider, *, budget_overrides=None, runtime_profile=None):
            del runtime, shell, provider, runtime_profile
            captured["budget_overrides"] = dict(budget_overrides or {})

        def run_task(self, task, seed):
            return RunResult(
                task_id=task.task_id,
                seed=seed,
                artifact=task.expected,
                verifier_score=1.0,
                cost=0.0,
                latency=0.0,
                faults=0,
            )

    monkeypatch.setattr(runtime_entry_module, "load_runtime_profile", lambda runtime_dir, profile_path=None: default_runtime_profile())
    monkeypatch.setattr(runtime_entry_module, "build_provider_from_payload", lambda payload, provider_profile=None: LocalDeterministicProvider())
    monkeypatch.setattr(runtime_entry_module, "load_runtime", lambda *args, **kwargs: SimpleNamespace(capability_exchange=_capability()))
    monkeypatch.setattr(runtime_entry_module, "TaskRuntime", FakeTaskRuntime)

    args = argparse.Namespace(
        runtime_dir=str(runtime_dir),
        input_json=str(input_json),
        provider_json=str(provider_json),
        profile_json=None,
        workspace=str(tmp_path / "workspace"),
        artifact_mode="none",
        output_json=str(output_json),
    )

    assert runtime_entry_module._run_batch(args) == 0
    assert captured["budget_overrides"] == {"M_max": 2, "Q_max": 1}


def test_solve_result_marks_failed_exact_verification_as_unverified() -> None:
    request = SolveRequest(
        request_id="solve.request",
        prompt="Compute the sum and product.",
        context_items=[],
        file_paths=[],
        output_schema={},
        allowed_tool_categories=["math/basic"],
        verification_preference="verified_if_available",
        budget_overrides={},
    )
    run = RunResult(
        task_id="user.solve.request",
        seed=0,
        artifact={"sum": 9, "product": 20},
        verifier_score=0.0,
        cost=0.0,
        latency=0.0,
        faults=0,
        trace=[{"event": "check_result", "checker": "benchmark", "passed": False}],
    )

    result = solve_result_from_run_result_with_context(
        request,
        run,
        "runtime-hash",
        mode="user_request",
        provider_usage={},
    )

    assert result.status == "unverified"
    assert result.verification_status == "exact_verifier_failed"
    assert result.verified is False
    assert result.best_effort is False


def test_container_batch_protocol_sets_runtime_sdk_pythonpath_and_rewrites_host_paths(
    runtime_dir: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    executor = DockerRuntimeExecutor(tmp_path / "docker_ws", repo_root=Path.cwd(), artifact_mode="always")
    monkeypatch.setattr(DockerRuntimeExecutor, "ensure_image", lambda self: None)
    captured_command: list[str] = []

    def fake_run(command, **kwargs):
        del kwargs
        captured_command[:] = [str(item) for item in command]
        output_dir: Path | None = None
        workspace_dir: Path | None = None
        for index, item in enumerate(captured_command):
            if item != "-v":
                continue
            mount = captured_command[index + 1]
            if mount.endswith(":/mnt/output"):
                output_dir = Path(mount.split(":/mnt/output", 1)[0])
            elif mount.endswith(":/mnt/workspace"):
                workspace_dir = Path(mount.split(":/mnt/workspace", 1)[0])
        assert output_dir is not None
        assert workspace_dir is not None
        response = RuntimeBatchResponse(
            request_id="batch.container",
            capability_exchange=_capability(),
            run_results=[
                RunResult(
                    task_id="top.sum_product",
                    seed=0,
                    artifact={"sum": 10, "product": 30},
                    verifier_score=1.0,
                    cost=0.0,
                    latency=0.0,
                    faults=0,
                    trace_path="/mnt/workspace/seed_0/traces/top.sum_product_0.json",
                    checkpoint_ref="/mnt/workspace/seed_0/checkpoints/top.sum_product_0.json",
                )
            ],
            provider_usage={},
        )
        (output_dir / "run_result.json").write_text(
            json.dumps(model_dump(response), indent=2, sort_keys=True),
            encoding="utf-8",
        )

        class Completed:
            returncode = 0
            stdout = ""
            stderr = ""

        return Completed()

    monkeypatch.setattr(container_runtime_module.subprocess, "run", fake_run)

    response = executor.run_batch_protocol(
        runtime_dir,
        RuntimeBatchRequest(
            request_id="batch.container",
            runtime_backend="docker",
            invocations=[RuntimeTaskInvocation(seed=0, task=build_demo_suite().by_id("top.sum_product"))],
        ),
        provider=LocalDeterministicProvider(),
    )

    assert f"PYTHONPATH=/mnt/runtime/{KERNEL_BUNDLE_DIR}" in captured_command
    workspace_mount = next(mount for mount in captured_command if mount.endswith(":/mnt/workspace"))
    host_workspace = Path(workspace_mount.split(":/mnt/workspace", 1)[0]).resolve()
    assert response.run_results[0].trace_path == str(host_workspace / "seed_0" / "traces" / "top.sum_product_0.json")
    assert response.run_results[0].checkpoint_ref == str(host_workspace / "seed_0" / "checkpoints" / "top.sum_product_0.json")
