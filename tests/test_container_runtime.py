from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agintor.container_runtime import DockerRuntimeExecutor
from agintor.exceptions import RuntimeLoadError
from agintor.providers import LocalDeterministicProvider
from agintor.pydantic_compat import model_dump
from agintor.project import init_runtime
from agintor.runtime_api import load_solve_request, runtime_solve_request_for_user_request
from agintor.runtime_loader import (
    KERNEL_VERSION,
    RUNTIME_ABI_VERSION,
    STORAGE_SCHEMA_VERSION,
    load_runtime,
    resolve_docker_launch_policy,
)
from agintor.schemas import (
    AsyncHandle,
    AttemptManifest,
    BenchmarkTask,
    CapabilityExchange,
    CheckpointEnvelope,
    CheckpointReference,
    InspectRequest,
    RequestFileRef,
    RunManifest,
    RuntimeBatchRequest,
    RuntimeBatchResponse,
    RuntimeResumeRequest,
    RuntimeSolveResponse,
    RuntimeTaskInvocation,
    SideEffectReceipt,
    SolveResult,
)


def _capability_exchange() -> CapabilityExchange:
    return CapabilityExchange(
        runtime_abi="agintor-runtime-abi-v5",
        kernel_version="agintor-kernel-v1",
        storage_schema_version="agintor-storage-v3",
        supported_backends=["local", "docker"],
        tool_runtimes=["python"],
        checkpoint_support=True,
        runtime_asset_capabilities={"traces": True, "checkpoints": True, "runtime_sdk": True},
        side_effect_receipts=True,
        resume_support=True,
        runtime_isolation_policy={"required_guarantees": []},
        supported_guarantees=[],
        effective_guarantees=[],
        required_env_names=[],
        required_env_any_of=[],
        capability_flags=["inspect", "run_batch", "benchmark_mode", "prompt_mode"],
    )


def _task(task_id: str) -> BenchmarkTask:
    return BenchmarkTask(
        task_id=task_id,
        family="top",
        prompt="Say hello.",
        task_type="structured_ops",
        operations=[],
        expected={},
    )


def _deployment_contract_payload(
    *,
    network_policy: str = "provider-only",
    runtime_isolation_policy: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "entry_command": "agintor solve <runtime_dir> --prompt \"<request>\"",
        "runtime_abi": RUNTIME_ABI_VERSION,
        "kernel_version": KERNEL_VERSION,
        "storage_schema_version": STORAGE_SCHEMA_VERSION,
        "python_version": ">=3.11",
        "supported_backends": ["local", "docker"],
        "required_env_names": [],
        "environment_allowlist": [],
        "network_policy": network_policy,
        "filesystem_policy": "workspace-read-write",
        "runtime_isolation_policy": runtime_isolation_policy,
        "notes": [],
    }


def _write_deployment_contract(runtime_dir: Path, payload: dict[str, object]) -> None:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "deployment_contract.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def test_resolve_docker_launch_policy_requires_network_none_for_restricted_contract(tmp_path: Path):
    runtime_dir = tmp_path / "restricted-runtime"
    _write_deployment_contract(runtime_dir, _deployment_contract_payload(network_policy="restricted"))

    launch_policy = resolve_docker_launch_policy(runtime_dir)

    assert launch_policy.network_none is True


def test_resolve_docker_launch_policy_allows_valid_provider_only_contract(tmp_path: Path):
    runtime_dir = tmp_path / "provider-runtime"
    _write_deployment_contract(runtime_dir, _deployment_contract_payload())

    launch_policy = resolve_docker_launch_policy(runtime_dir)

    assert launch_policy.network_none is False


def test_resolve_docker_launch_policy_raises_for_missing_contract(tmp_path: Path):
    runtime_dir = tmp_path / "missing-runtime"
    runtime_dir.mkdir(parents=True)

    with pytest.raises(RuntimeLoadError, match="missing deployment_contract.json"):
        resolve_docker_launch_policy(runtime_dir)


def test_resolve_docker_launch_policy_raises_for_corrupt_contract(tmp_path: Path):
    runtime_dir = tmp_path / "corrupt-runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "deployment_contract.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(RuntimeLoadError, match="invalid JSON"):
        resolve_docker_launch_policy(runtime_dir)


def test_resolve_docker_launch_policy_raises_for_schema_invalid_contract(tmp_path: Path):
    runtime_dir = tmp_path / "invalid-runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "deployment_contract.json").write_text(
        json.dumps({"network_policy": "restricted"}, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeLoadError, match="invalid deployment contract schema"):
        resolve_docker_launch_policy(runtime_dir)


def test_load_runtime_rejects_local_backend_for_restricted_network_policy(tmp_path: Path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    contract_path = runtime_dir / "deployment_contract.json"
    payload = json.loads(contract_path.read_text(encoding="utf-8"))
    payload["network_policy"] = "restricted"
    payload["runtime_isolation_policy"]["network_policy"] = "restricted"
    payload["runtime_isolation_policy"]["required_guarantees"] = []
    contract_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    with pytest.raises(RuntimeLoadError, match="cannot satisfy network policy"):
        load_runtime(runtime_dir, runtime_backend="local")


def test_inspect_fails_closed_before_any_subprocess_launch_on_contract_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    runtime_dir = tmp_path / "corrupt-runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "deployment_contract.json").write_text("{not json", encoding="utf-8")
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def _unexpected_run(*args: object, **kwargs: object):
        calls.append((args, kwargs))
        raise AssertionError("subprocess.run should not be called when launch policy resolution fails")

    monkeypatch.setattr("agintor.container_runtime.subprocess.run", _unexpected_run)

    executor = DockerRuntimeExecutor(tmp_path / "executor")
    request = InspectRequest(
        request_id="inspect.1",
        requested_backend="docker",
        expected_runtime_abi=RUNTIME_ABI_VERSION,
        expected_kernel_version=KERNEL_VERSION,
        expected_storage_schema_version=STORAGE_SCHEMA_VERSION,
    )

    with pytest.raises(RuntimeLoadError, match="invalid JSON"):
        executor.inspect(runtime_dir, request)

    assert calls == []


def test_containerize_solve_request_rewrites_durable_run_root(tmp_path: Path):
    run_root = tmp_path / "host" / "runs" / "run.123"
    run_root.mkdir(parents=True)
    request = runtime_solve_request_for_user_request(
        runtime_backend="docker",
        seed=0,
        solve_request=load_solve_request(prompt="Say hello."),
    ).copy(
        update={
            "run_id": "run.123",
            "run_root": str(run_root),
            "attempt_id": "attempt_0001",
        }
    )

    container_request, mount_root = DockerRuntimeExecutor._containerize_solve_request(request)

    assert mount_root == run_root.parent
    assert container_request.run_root == "/mnt/runs/run.123"


def test_containerize_batch_request_rewrites_invocation_run_roots(tmp_path: Path):
    runs_root = tmp_path / "host" / "runs"
    run_one = runs_root / "run.001"
    run_two = runs_root / "run.002"
    run_one.mkdir(parents=True)
    run_two.mkdir(parents=True)
    request = RuntimeBatchRequest(
        request_id="batch.1",
        runtime_backend="docker",
        invocations=[
            RuntimeTaskInvocation(
                request_id="benchmark.one.seed_0",
                run_id="run.001",
                run_root=str(run_one),
                attempt_id="attempt_0001",
                seed=0,
                task=_task("one"),
            ),
            RuntimeTaskInvocation(
                request_id="benchmark.two.seed_0",
                run_id="run.002",
                run_root=str(run_two),
                attempt_id="attempt_0001",
                seed=0,
                task=_task("two"),
            ),
        ],
    )

    container_request, mount_root = DockerRuntimeExecutor._containerize_batch_request(request)

    assert mount_root == runs_root
    assert [invocation.run_root for invocation in container_request.invocations] == [
        "/mnt/runs/run.001",
        "/mnt/runs/run.002",
    ]


def test_docker_run_batch_wrapper_accepts_real_benchmark_tasks_without_hash_crash(tmp_path: Path, monkeypatch):
    executor = DockerRuntimeExecutor(tmp_path / "executor")
    captured: dict[str, RuntimeBatchRequest] = {}

    def fake_run_batch_protocol(runtime_dir, request, **kwargs):
        captured["request"] = request
        return RuntimeBatchResponse(
            request_id=request.request_id,
            capability_exchange=_capability_exchange(),
            run_results=[],
            provider_usage={},
        )

    monkeypatch.setattr(executor, "run_batch_protocol", fake_run_batch_protocol)

    runs = executor.run_batch(
        "dummy-runtime",
        [(_task("one"), 0)],
        provider=None,  # type: ignore[arg-type]
    )

    assert runs == []
    assert captured["request"].request_id.startswith("docker.")


def test_containerize_solve_request_file_refs_rewrites_absolute_host_paths_with_spaces(tmp_path: Path):
    run_root = tmp_path / "host" / "runs" / "run.123"
    run_root.mkdir(parents=True)
    host_file = tmp_path / "Folder With Spaces" / "notes file.txt"
    host_file.parent.mkdir(parents=True, exist_ok=True)
    host_file.write_text("hello", encoding="utf-8")
    solve_request = load_solve_request(prompt=f"Inspect {host_file} and summarize it.")
    request = runtime_solve_request_for_user_request(
        runtime_backend="docker",
        seed=0,
        solve_request=solve_request,
    ).copy(
        update={
            "run_id": "run.123",
            "run_root": str(run_root),
            "attempt_id": "attempt_0001",
        }
    )

    container_request, mounts, reverse_map = DockerRuntimeExecutor._containerize_solve_request_file_refs(
        request,
        run_mount_root=run_root.parent,
    )

    assert mounts
    assert any(str(host_file.resolve()) in mount for mount in mounts)
    assert len(container_request.solve_request.request_file_refs) == 1
    request_file_ref = container_request.solve_request.request_file_refs[0]
    assert request_file_ref.source_path == str(host_file)
    assert request_file_ref.host_path == str(host_file.resolve())
    assert request_file_ref.runtime_path.startswith("/mnt/request-files/")
    assert container_request.solve_request.file_paths == [request_file_ref.runtime_path]
    assert reverse_map[request_file_ref.runtime_path] == str(host_file.resolve())


def test_rewrite_solve_response_paths_rewrites_request_file_artifact_paths(tmp_path: Path):
    host_file = (tmp_path / "Folder With Spaces" / "app file.py").resolve()
    host_file.parent.mkdir(parents=True, exist_ok=True)
    container_path = "/mnt/request-files/abc123/app file.py"
    workspace_dir = tmp_path / "docker-workspace"
    workspace_dir.mkdir(parents=True)
    response = RuntimeSolveResponse(
        request_id="solve.1",
        capability_exchange=_capability_exchange(),
        solve_result=SolveResult(
            request_id="solve.1",
            runtime_hash="hash",
            artifact={
                "applied": True,
                "updated_files": [{"path": container_path, "diff": "stub"}],
            },
            status="best_effort",
            verification_status="best_effort",
            summary="ok",
        ),
    )
    executor = DockerRuntimeExecutor(tmp_path / "executor")

    executor._rewrite_solve_response_paths(
        response,
        workspace_dir,
        request_file_reverse_map={container_path: str(host_file)},
    )

    assert response.solve_result.artifact["updated_files"][0]["path"] == str(host_file)


def test_container_resume_request_prefers_run_mount_for_checkpoint_paths(tmp_path: Path):
    run_root = tmp_path / "host" / "runs" / "run.123"
    checkpoint_path = run_root / "checkpoints" / "checkpoint.json"
    checkpoint_path.parent.mkdir(parents=True)
    checkpoint_path.write_text("{}", encoding="utf-8")
    request = RuntimeResumeRequest(
        request_id="resume.1",
        run_ref="run.123",
        checkpoint_ref=str(checkpoint_path),
        run_id="run.123",
        run_root=str(run_root),
        attempt_id="attempt_0002",
        checkpoint_store_dir=str(checkpoint_path.parent),
    )

    container_request, checkpoint_store_dir, mount_root = DockerRuntimeExecutor._container_resume_request(
        request
    )

    assert mount_root == run_root.parent
    assert checkpoint_store_dir == run_root.parent
    assert container_request.run_root == "/mnt/runs/run.123"
    assert container_request.checkpoint_ref == "/mnt/runs/run.123/checkpoints/checkpoint.json"
    assert container_request.checkpoint_store_dir == "/mnt/runs"


def test_resume_protocol_mounts_request_file_refs_from_checkpoint(tmp_path: Path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    executor = DockerRuntimeExecutor(tmp_path / "executor")
    monkeypatch.setattr(executor, "ensure_image", lambda: None)

    run_root = tmp_path / "host" / "runs" / "run.123"
    run_root.mkdir(parents=True)
    checkpoint_dir = tmp_path / "external-checkpoints"
    checkpoint_dir.mkdir(parents=True)
    request_file = tmp_path / "Folder With Spaces" / "notes file.txt"
    request_file.parent.mkdir(parents=True, exist_ok=True)
    request_file.write_text("hello", encoding="utf-8")
    container_request_file_path = DockerRuntimeExecutor._container_request_file_mount_path(request_file.resolve())
    checkpoint_path = checkpoint_dir / "checkpoint.resume.request-files.json"
    checkpoint_path.write_text(
        json.dumps(
            model_dump(
                CheckpointEnvelope(
                    checkpoint_id="checkpoint.resume.request-files",
                    runtime_abi="agintor-runtime-abi-v5",
                    storage_schema_version="agintor-storage-v3",
                    runtime_hash="runtime-hash",
                    run_id="run.123",
                    run_root=str(run_root.resolve()),
                    request_id="resume.request-files",
                    plan_id="plan.resume.request-files",
                    task_id="task.resume.request-files",
                    seed=0,
                    plan_snapshot={
                        "file_ref_specs": [
                            model_dump(
                                RequestFileRef(
                                    file_ref_id="file.resume.request-files",
                                    source_path=str(request_file),
                                    runtime_path=container_request_file_path,
                                    path_root="host_absolute",
                                    host_path=str(request_file.resolve()),
                                )
                            )
                        ]
                    },
                    task_payload={},
                )
            ),
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    request = RuntimeResumeRequest(
        request_id="resume.request-files",
        run_ref="run.123",
        checkpoint_ref=str(checkpoint_path),
        run_id="run.123",
        run_root=str(run_root.resolve()),
        attempt_id="attempt_0001",
        runtime_backend="docker",
    )
    captured: dict[str, list[str]] = {}

    def fake_docker_run_argv(**kwargs):
        captured["mounts"] = list(kwargs["mounts"])
        return ["cmd"]

    def fake_subprocess_run(*args, **kwargs):
        output_mount = next(mount for mount in captured["mounts"] if mount.endswith(":/mnt/output"))
        output_dir = Path(output_mount.removesuffix(":/mnt/output"))
        output_path = output_dir / "resume_result.json"
        output_path.write_text(
            json.dumps(
                model_dump(
                    RuntimeSolveResponse(
                        request_id=request.request_id,
                        capability_exchange=_capability_exchange(),
                        solve_result=SolveResult(
                            request_id=request.request_id,
                            runtime_hash="hash",
                            run_root=str(run_root.resolve()),
                            artifact={},
                            status="best_effort",
                            verification_status="best_effort",
                            summary="ok",
                        ),
                    )
                ),
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(executor, "_docker_run_argv", fake_docker_run_argv)
    monkeypatch.setattr("agintor.container_runtime.subprocess.run", fake_subprocess_run)

    executor.resume_protocol(
        runtime_dir,
        request,
        provider=LocalDeterministicProvider(),
    )

    assert any(str(request_file.resolve()) in mount and "/mnt/request-files/" in mount for mount in captured["mounts"])


def test_docker_run_argv_places_all_runtime_flags_before_the_image(tmp_path: Path):
    executor = DockerRuntimeExecutor(tmp_path / "executor")

    argv = executor._docker_run_argv(
        image_tag="agintor-runtime:test",
        entrypoint_argv=[
            "python",
            "-m",
            "agintor_runtime.runtime_entry",
            "solve",
        ],
        mounts=[
            "/host/runtime:/mnt/runtime:ro",
            "/host/output:/mnt/output",
        ],
        env_vars={
            "PYTHONPATH": "/mnt/runtime/.bundle",
            "AGINTOR_RUNTIME_BACKEND": "docker",
        },
        network_none=True,
    )

    image_index = argv.index("agintor-runtime:test")
    assert argv[:4] == ["docker", "run", "--rm", "--init"]
    assert argv[4:6] == ["--network", "none"]
    assert "-e" in argv[:image_index]
    assert "-v" in argv[:image_index]
    assert argv[image_index + 1 :] == [
        "python",
        "-m",
        "agintor_runtime.runtime_entry",
        "solve",
    ]


def test_rewrite_solve_response_paths_restores_run_root_and_latest_checkpoint_ref(tmp_path: Path):
    runs_root = tmp_path / "host" / "runs"
    workspace_dir = tmp_path / "docker-workspace"
    runs_root.mkdir(parents=True)
    workspace_dir.mkdir(parents=True)
    response = RuntimeSolveResponse(
        request_id="solve.1",
        capability_exchange=_capability_exchange(),
        solve_result=SolveResult(
            request_id="solve.1",
            runtime_hash="hash",
            run_id="run.123",
            run_root="/mnt/runs/run.123",
            attempt_id="attempt_0001",
            latest_checkpoint_ref="/mnt/runs/run.123/checkpoints/latest.json",
            mode="user_request",
            artifact={"ok": True},
            status="best_effort",
            verification_status="best_effort",
            summary="ok",
            recoverability="checkpoint_available",
            faults={"hard_invalid": False},
            budget={},
            provider_usage={},
            checks=[],
            verified=False,
            best_effort=True,
            trace_ref="/mnt/runs/run.123/attempts/attempt_0001/workspace/traces/trace.json",
            checkpoint_ref="/mnt/runs/run.123/checkpoints/latest.json",
        ),
    )
    executor = DockerRuntimeExecutor(tmp_path / "executor")

    executor._rewrite_solve_response_paths(response, workspace_dir, run_mount_root=runs_root)

    assert response.solve_result.run_root == str((runs_root / "run.123").resolve())
    assert response.solve_result.latest_checkpoint_ref == str(
        (runs_root / "run.123" / "checkpoints" / "latest.json").resolve()
    )
    assert response.solve_result.checkpoint_ref == str(
        (runs_root / "run.123" / "checkpoints" / "latest.json").resolve()
    )
    assert response.solve_result.trace_ref == str(
        (runs_root / "run.123" / "attempts" / "attempt_0001" / "workspace" / "traces" / "trace.json").resolve()
    )


def test_rewrite_durable_run_paths_rewrites_only_typed_path_fields(tmp_path: Path):
    runs_root = tmp_path / "host" / "runs"
    checkpoint_store_dir = tmp_path / "host" / "checkpoints"
    run_root = runs_root / "run.123"
    attempt_dir = run_root / "attempts" / "attempt_0001"
    checkpoint_dir = run_root / "checkpoints"
    side_effect_dir = run_root / "side_effects"
    checkpoint_store_dir.mkdir(parents=True)
    attempt_dir.mkdir(parents=True)
    checkpoint_dir.mkdir(parents=True)
    side_effect_dir.mkdir(parents=True)

    run_manifest = RunManifest(
        run_id="run.123",
        run_root="/mnt/runs/run.123",
        latest_checkpoint_ref="/mnt/runs/run.123/checkpoints/LATEST.json",
        task_id="task.1",
        seed=0,
    )
    attempt_manifest = AttemptManifest(
        attempt_id="attempt_0001",
        run_id="run.123",
        run_root="/mnt/runs/run.123",
        sequence_no=1,
        launch_kind="solve",
        workspace_root="/mnt/runs/run.123/attempts/attempt_0001/workspace",
        latest_checkpoint_ref="/mnt/runs/run.123/checkpoints/LATEST.json",
    )
    checkpoint_ref = CheckpointReference(
        ref="/mnt/runs/run.123/checkpoints/checkpoint.run.123.0001.json",
        run_id="run.123",
        run_root="/mnt/runs/run.123",
        attempt_id="attempt_0001",
        task_id="task.1",
        seed=0,
        request_id="solve.1",
        plan_id="plan.1",
        checkpoint_id="checkpoint.run.123.0001",
        latest=True,
    )
    checkpoint_envelope = CheckpointEnvelope(
        checkpoint_id="checkpoint.run.123.0001",
        runtime_abi="agintor-runtime-abi-v5",
        storage_schema_version="agintor-storage-v3",
        runtime_hash="hash",
        run_id="run.123",
        run_root="/mnt/runs/run.123",
        attempt_id="attempt_0001",
        request_id="solve.1",
        plan_id="plan.1",
        task_id="task.1",
        seed=0,
        runtime_state_snapshot={
            "latest_checkpoint_ref": "/mnt/runs/run.123/checkpoints/LATEST.json",
        },
        shell_state_snapshot={
            "open_handles": [
                AsyncHandle(
                    handle_id="handle.1",
                    tool_name="dummy-tool",
                    sandbox_hash="sandbox-hash",
                    working_directory="/mnt/runs/run.123/attempts/attempt_0001/workspace",
                    launch_time=0.0,
                    timeout=60.0,
                    stdout_path="/mnt/runs/run.123/attempts/attempt_0001/workspace/stdout.txt",
                    stderr_path="/mnt/runs/run.123/attempts/attempt_0001/workspace/stderr.txt",
                    state="completed",
                    artifact_refs=[
                        "/mnt/runs/run.123/artifacts/result.json",
                        "/mnt/checkpoints/shared/handle-output.json",
                    ],
                )
            ]
        },
        attempt_snapshot={
            "run_id": "run.123",
            "run_root": "/mnt/runs/run.123",
            "attempt_id": "attempt_0001",
            "resumed_from_checkpoint_ref": "/mnt/checkpoints/shared/resume-source.json",
        },
        side_effect_ledger={
            "receipts": [
                SideEffectReceipt(
                    side_effect_id="provider-request.1",
                    action_fingerprint="provider-request.1",
                    idempotency_key="provider-request.1",
                    action_kind="provider_request",
                    request_digest="provider-request.1",
                    backend="docker",
                    result_ref={"opaque_path": "/mnt/checkpoints/shared/receipt-payload-should-stay.json"},
                )
            ]
        },
        working_state_summary={"opaque_path": "/mnt/checkpoints/shared/working-summary-should-stay.json"},
        trace_cursor={"opaque_path": "/mnt/runs/run.123/trace-cursor-should-stay.json"},
    )

    (run_root / "run_manifest.json").write_text(
        json.dumps(model_dump(run_manifest), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (attempt_dir / "attempt_manifest.json").write_text(
        json.dumps(model_dump(attempt_manifest), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (checkpoint_dir / "checkpoint.run.123.0001.json").write_text(
        json.dumps(model_dump(checkpoint_envelope), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (checkpoint_dir / "LATEST.json").write_text(
        json.dumps(model_dump(checkpoint_ref), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (checkpoint_dir / "index.json").write_text(
        json.dumps([model_dump(checkpoint_ref)], indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (side_effect_dir / "receipt.1.json").write_text(
        json.dumps(
            {"result_ref": "/mnt/checkpoints/shared/side-effect-file-should-stay.json"},
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    DockerRuntimeExecutor._rewrite_durable_run_paths(
        run_root,
        run_mount_root=runs_root,
        checkpoint_store_dir=checkpoint_store_dir,
    )

    rewritten_run_manifest = json.loads((run_root / "run_manifest.json").read_text(encoding="utf-8"))
    rewritten_attempt_manifest = json.loads((attempt_dir / "attempt_manifest.json").read_text(encoding="utf-8"))
    rewritten_checkpoint = json.loads((checkpoint_dir / "checkpoint.run.123.0001.json").read_text(encoding="utf-8"))
    rewritten_latest = json.loads((checkpoint_dir / "LATEST.json").read_text(encoding="utf-8"))
    rewritten_index = json.loads((checkpoint_dir / "index.json").read_text(encoding="utf-8"))
    untouched_side_effect = json.loads((side_effect_dir / "receipt.1.json").read_text(encoding="utf-8"))

    assert rewritten_run_manifest["run_root"] == str(run_root.resolve())
    assert rewritten_run_manifest["latest_checkpoint_ref"] == str((checkpoint_dir / "LATEST.json").resolve())
    assert rewritten_attempt_manifest["run_root"] == str(run_root.resolve())
    assert rewritten_attempt_manifest["workspace_root"] == str((attempt_dir / "workspace").resolve())
    assert rewritten_attempt_manifest["latest_checkpoint_ref"] == str((checkpoint_dir / "LATEST.json").resolve())
    assert rewritten_checkpoint["run_root"] == str(run_root.resolve())
    assert rewritten_checkpoint["runtime_state_snapshot"]["latest_checkpoint_ref"] == str(
        (checkpoint_dir / "LATEST.json").resolve()
    )
    assert rewritten_checkpoint["attempt_snapshot"]["run_root"] == str(run_root.resolve())
    assert rewritten_checkpoint["attempt_snapshot"]["resumed_from_checkpoint_ref"] == str(
        (checkpoint_store_dir / "shared" / "resume-source.json").resolve()
    )
    assert rewritten_checkpoint["shell_state_snapshot"]["open_handles"][0]["working_directory"] == str(
        (attempt_dir / "workspace").resolve()
    )
    assert rewritten_checkpoint["shell_state_snapshot"]["open_handles"][0]["stdout_path"] == str(
        (attempt_dir / "workspace" / "stdout.txt").resolve()
    )
    assert rewritten_checkpoint["shell_state_snapshot"]["open_handles"][0]["stderr_path"] == str(
        (attempt_dir / "workspace" / "stderr.txt").resolve()
    )
    assert rewritten_checkpoint["shell_state_snapshot"]["open_handles"][0]["artifact_refs"] == [
        str((run_root / "artifacts" / "result.json").resolve()),
        str((checkpoint_store_dir / "shared" / "handle-output.json").resolve()),
    ]
    assert rewritten_latest["ref"] == str((checkpoint_dir / "checkpoint.run.123.0001.json").resolve())
    assert rewritten_latest["run_root"] == str(run_root.resolve())
    assert rewritten_index[0]["ref"] == str((checkpoint_dir / "checkpoint.run.123.0001.json").resolve())
    assert rewritten_index[0]["run_root"] == str(run_root.resolve())
    assert (
        rewritten_checkpoint["side_effect_ledger"]["receipts"][0]["result_ref"]["opaque_path"]
        == "/mnt/checkpoints/shared/receipt-payload-should-stay.json"
    )
    assert rewritten_checkpoint["working_state_summary"]["opaque_path"] == (
        "/mnt/checkpoints/shared/working-summary-should-stay.json"
    )
    assert rewritten_checkpoint["trace_cursor"]["opaque_path"] == "/mnt/runs/run.123/trace-cursor-should-stay.json"
    assert untouched_side_effect["result_ref"] == "/mnt/checkpoints/shared/side-effect-file-should-stay.json"
