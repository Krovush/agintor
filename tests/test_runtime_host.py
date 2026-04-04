from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import subprocess
from pathlib import Path
import sys

import pytest

from agintor.benchmarks import build_demo_suite
from agintor.exceptions import RuntimeLoadError
from agintor.providers import LocalDeterministicProvider, provider_payload
from agintor.pydantic_compat import model_dump
from agintor.runtime_api import load_solve_request, solve_result_from_run_result_with_context
from agintor.runtime_host import RuntimeHost
from agintor.runtime_sdk import KERNEL_MANIFEST_FILE, KERNEL_VERSION, STORAGE_SCHEMA_VERSION
from agintor.schemas import RunResult, RuntimeBatchRequest, RuntimeTaskInvocation

pytestmark = pytest.mark.usefixtures("module_failure_artifact_bucket")


def test_runtime_host_inspect_reports_versioned_capabilities(runtime_dir: Path, tmp_path: Path) -> None:
    host = RuntimeHost(tmp_path / "runtime_host", runtime_backend="local", artifact_mode="always")

    capability = host.inspect(runtime_dir)

    assert capability.runtime_abi == "agintor-runtime-abi-v3"
    assert capability.kernel_version == KERNEL_VERSION
    assert capability.storage_schema_version == STORAGE_SCHEMA_VERSION
    assert capability.checkpoint_support is True
    assert capability.runtime_asset_capabilities["runtime_sdk"] is True


def test_runtime_host_runs_copied_runtime_through_bundled_kernel(runtime_dir: Path, tmp_path: Path) -> None:
    copied_runtime = tmp_path / "copied_runtime"
    shutil.copytree(runtime_dir, copied_runtime)
    host = RuntimeHost(tmp_path / "runtime_host", runtime_backend="local", artifact_mode="always")
    task = build_demo_suite().by_id("top.sum_product")

    response = host.run_batch(
        copied_runtime,
        [(task, 0)],
        provider=LocalDeterministicProvider(),
    )

    assert response.run_results[0].verifier_score == 1.0
    assert response.run_results[0].checkpoint_ref
    manifest = json.loads((copied_runtime / "runtime_manifest.json").read_text(encoding="utf-8"))
    assert manifest["immutable_manifest"] == [
        "runtime_sdk/kernel_manifest.json",
        "deployment_contract.json",
        "runtime_profile.json",
    ]
    assert (copied_runtime / "runtime_sdk" / KERNEL_MANIFEST_FILE).exists()


def test_runtime_host_inspect_fails_closed_on_kernel_version_mismatch(runtime_dir: Path, tmp_path: Path) -> None:
    broken_runtime = tmp_path / "broken_runtime"
    shutil.copytree(runtime_dir, broken_runtime)
    kernel_manifest_path = broken_runtime / "runtime_sdk" / KERNEL_MANIFEST_FILE
    kernel_manifest = json.loads(kernel_manifest_path.read_text(encoding="utf-8"))
    kernel_manifest["kernel_version"] = "agintor-kernel-v999"
    kernel_manifest_path.write_text(json.dumps(kernel_manifest, indent=2), encoding="utf-8")
    host = RuntimeHost(tmp_path / "runtime_host", runtime_backend="local", artifact_mode="always")

    with pytest.raises(RuntimeLoadError):
        host.inspect(broken_runtime)


def test_bundled_runtime_entry_runs_with_runtime_sdk_only(runtime_dir: Path, tmp_path: Path) -> None:
    copied_runtime = tmp_path / "copied_runtime"
    shutil.copytree(runtime_dir, copied_runtime)
    task = build_demo_suite().by_id("top.sum_product")
    input_json = tmp_path / "batch_request.json"
    provider_json = tmp_path / "provider.json"
    output_json = tmp_path / "batch_response.json"
    workspace_dir = tmp_path / "workspace"
    input_json.write_text(
        json.dumps(
            model_dump(
                RuntimeBatchRequest(
                    request_id="batch.self_contained",
                    runtime_backend="local",
                    invocations=[RuntimeTaskInvocation(seed=0, task=task)],
                )
            ),
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    provider_json.write_text(json.dumps(provider_payload(LocalDeterministicProvider()), indent=2, sort_keys=True), encoding="utf-8")
    isolated_cwd = tmp_path / "isolated_cwd"
    isolated_cwd.mkdir()
    env = dict(os.environ)
    env["PYTHONPATH"] = str((copied_runtime / "runtime_sdk").resolve())

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "agintor_runtime.runtime_entry",
            "run-batch",
            "--runtime-dir",
            str(copied_runtime),
            "--input-json",
            str(input_json),
            "--provider-json",
            str(provider_json),
            "--workspace",
            str(workspace_dir),
            "--output-json",
            str(output_json),
        ],
        cwd=isolated_cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    response = json.loads(output_json.read_text(encoding="utf-8"))
    assert response["run_results"][0]["verifier_score"] == 1.0


def test_runtime_entry_forwards_budget_overrides_to_task_runtime(runtime_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    input_json = tmp_path / "batch_request.json"
    provider_json = tmp_path / "provider.json"
    output_json = tmp_path / "batch_response.json"
    workspace_dir = tmp_path / "workspace"
    task = build_demo_suite().by_id("top.sum_product")
    input_json.write_text(
        json.dumps(
            model_dump(
                RuntimeBatchRequest(
                    request_id="batch.budget_overrides",
                    runtime_backend="local",
                    budget_overrides={"M_max": 3, "Q_max": 1},
                    invocations=[RuntimeTaskInvocation(seed=0, task=task)],
                )
            ),
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    provider_json.write_text(json.dumps(provider_payload(LocalDeterministicProvider()), indent=2, sort_keys=True), encoding="utf-8")
    captured: list[dict[str, int]] = []
    runtime_sdk_path = str((runtime_dir / "runtime_sdk").resolve())
    monkeypatch.syspath_prepend(runtime_sdk_path)
    runtime_entry_module = importlib.import_module("agintor_runtime.runtime_entry")

    class FakeTaskRuntime:
        def __init__(self, runtime, shell, provider, budget_overrides=None, runtime_profile=None):
            del runtime, shell, provider, runtime_profile
            captured.append(dict(budget_overrides or {}))

        def run_task(self, task, seed):
            return RunResult(
                task_id=task.task_id,
                seed=seed,
                artifact={"ok": True},
                verifier_score=1.0,
                cost=0.0,
                latency=0.0,
                faults=0,
            )

    monkeypatch.setattr(runtime_entry_module, "TaskRuntime", FakeTaskRuntime)

    exit_code = runtime_entry_module._run_batch(
        argparse.Namespace(
            runtime_dir=str(runtime_dir),
            input_json=str(input_json),
            provider_json=str(provider_json),
            profile_json=None,
            workspace=str(workspace_dir),
            artifact_mode="none",
            output_json=str(output_json),
        )
    )

    assert exit_code == 0
    assert captured == [{"M_max": 3, "Q_max": 1}]


def test_solve_result_marks_failed_exact_verifier_as_unverified() -> None:
    request = load_solve_request(prompt="Compute the sum and product for [2, 3].")
    run = RunResult(
        task_id="user.solve.exact_failure",
        seed=0,
        artifact={"sum": 5, "product": 5},
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
    assert result.best_effort is False
