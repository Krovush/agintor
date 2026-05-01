from __future__ import annotations

import json
from pathlib import Path

from agintor.runtime.host.backends.docker.executor import DockerRuntimeExecutor
from agintor.storage.run_store import RunStore
from agintor.contracts import (
    AsyncHandle,
    CheckpointEnvelope,
    CheckpointReference,
    OpenAITraceContext,
    RunManifest,
    RuntimeResumeRequest,
)
from agintor.core.versioning import RUNTIME_CONTRACT_VERSION


def test_container_resume_request_prefers_run_mount_for_checkpoint_paths(tmp_path: Path):
    run_root = tmp_path / "host" / "runs" / "run.123"
    checkpoint_path = run_root / "checkpoints" / "checkpoint.json"
    runtime_dir = (tmp_path / "host" / "runtime").resolve()
    runtime_dir.mkdir(parents=True)
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
        trace_context=OpenAITraceContext(runtime_dir=str(tmp_path / "stale-host-runtime"), op_id="do-not-copy"),
    )

    container_request, checkpoint_store_dir, mount_root = DockerRuntimeExecutor._container_resume_request(
        request,
        runtime_path=runtime_dir,
    )

    assert mount_root == run_root.parent
    assert checkpoint_store_dir == run_root.parent
    assert container_request.run_root == "/mnt/runs/run.123"
    assert container_request.checkpoint_ref == "/mnt/runs/run.123/checkpoints/checkpoint.json"
    assert container_request.checkpoint_store_dir == "/mnt/runs"
    assert container_request.trace_context.runtime_dir == "/mnt/runtime"
    assert container_request.trace_context.op_id == "do-not-copy"


def test_container_resume_request_seeds_container_runtime_dir_when_trace_context_is_missing(tmp_path: Path):
    run_root = tmp_path / "host" / "runs" / "run.123"
    checkpoint_path = run_root / "checkpoints" / "checkpoint.json"
    runtime_dir = (tmp_path / "host" / "runtime").resolve()
    runtime_dir.mkdir(parents=True)
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

    container_request, _, _ = DockerRuntimeExecutor._container_resume_request(
        request,
        runtime_path=runtime_dir,
    )

    assert container_request.trace_context is not None
    assert container_request.trace_context.runtime_dir == "/mnt/runtime"


def test_container_resume_request_resolves_run_ref_only_before_containerizing(tmp_path: Path):
    run_store_workspace = tmp_path / "host"
    run_root = (run_store_workspace / "runs" / "run.123").resolve()
    checkpoint_path = run_root / "checkpoints" / "checkpoint.run.123.0001.json"
    checkpoint_path.parent.mkdir(parents=True)
    checkpoint = CheckpointEnvelope(
        checkpoint_id="checkpoint.run.123.0001",
        runtime_contract_version=RUNTIME_CONTRACT_VERSION,
        runtime_hash="runtime-hash",
        run_id="run.123",
        run_root=str(run_root),
        request_id="request.1",
        plan_id="plan.1",
        task_id="task.1",
        seed=0,
        resume_eligible=True,
    )
    checkpoint_path.write_text(
        json.dumps((checkpoint).model_dump(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    run_manifest = RunManifest(
        run_id="run.123",
        run_root=str(run_root),
        latest_checkpoint_ref=str(checkpoint_path),
        current_attempt_id="attempt_0001",
        runtime_backend="docker",
        resumable=True,
    )
    (run_root / "run_manifest.json").write_text(
        json.dumps((run_manifest).model_dump(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (checkpoint_path.parent / "LATEST.json").write_text(
        json.dumps(
            (CheckpointReference(
                ref=str(checkpoint_path),
                run_id="run.123",
                run_root=str(run_root),
                attempt_id="attempt_0001",
                request_id="request.1",
                plan_id="plan.1",
                checkpoint_id="checkpoint.run.123.0001",
                latest=True,
                resume_eligible=True,
            )).model_dump(),
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    request = RuntimeResumeRequest(
        request_id="resume.1",
        run_ref="run.123",
        run_id="run.123",
        attempt_id="attempt_0002",
        runtime_backend="docker",
    )
    executor = DockerRuntimeExecutor(tmp_path / "executor", run_store_workspace=run_store_workspace)

    resolved_request = executor._resolve_resume_checkpoint_request(request)
    container_request, checkpoint_store_dir, mount_root = DockerRuntimeExecutor._container_resume_request(
        resolved_request
    )

    assert resolved_request.checkpoint_ref == str(checkpoint_path)
    assert mount_root == run_root.parent
    assert checkpoint_store_dir == run_root.parent
    assert container_request.checkpoint_ref == "/mnt/runs/run.123/checkpoints/checkpoint.run.123.0001.json"


def test_container_resume_request_resolves_checkpoint_ref_only_before_containerizing(tmp_path: Path):
    run_store_workspace = tmp_path / "host"
    run_root = (run_store_workspace / "runs" / "run.123").resolve()
    checkpoint_path = run_root / "checkpoints" / "checkpoint.run.123.0001.json"
    checkpoint_path.parent.mkdir(parents=True)
    checkpoint = CheckpointEnvelope(
        checkpoint_id="checkpoint.run.123.0001",
        runtime_contract_version=RUNTIME_CONTRACT_VERSION,
        runtime_hash="runtime-hash",
        run_id="run.123",
        run_root=str(run_root),
        request_id="request.1",
        plan_id="plan.1",
        task_id="task.1",
        seed=0,
        resume_eligible=True,
    )
    checkpoint_path.write_text(
        json.dumps((checkpoint).model_dump(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    run_manifest = RunManifest(
        run_id="run.123",
        run_root=str(run_root),
        latest_checkpoint_ref=str(checkpoint_path),
        current_attempt_id="attempt_0001",
        runtime_backend="docker",
        resumable=True,
    )
    (run_root / "run_manifest.json").write_text(
        json.dumps((run_manifest).model_dump(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    request = RuntimeResumeRequest(
        request_id="resume.1",
        checkpoint_ref=str(checkpoint_path),
        attempt_id="attempt_0002",
        runtime_backend="docker",
    )
    executor = DockerRuntimeExecutor(tmp_path / "executor", run_store_workspace=run_store_workspace)

    resolved_request = executor._resolve_resume_checkpoint_request(request)
    container_request, checkpoint_store_dir, mount_root = DockerRuntimeExecutor._container_resume_request(
        resolved_request
    )

    assert resolved_request.run_root == str(run_root)
    assert resolved_request.run_id == "run.123"
    assert mount_root == run_root.parent
    assert checkpoint_store_dir == run_root.parent
    assert container_request.checkpoint_ref == "/mnt/runs/run.123/checkpoints/checkpoint.run.123.0001.json"


def test_container_resume_request_enriches_partial_checkpoint_and_run_root_identity(tmp_path: Path):
    run_store_workspace = tmp_path / "host"
    run_root = (run_store_workspace / "runs" / "run.123").resolve()
    checkpoint_path = run_root / "checkpoints" / "checkpoint.run.123.0001.json"
    checkpoint_path.parent.mkdir(parents=True)
    checkpoint = CheckpointEnvelope(
        checkpoint_id="checkpoint.run.123.0001",
        runtime_contract_version=RUNTIME_CONTRACT_VERSION,
        runtime_hash="runtime-hash",
        run_id="run.123",
        run_root=str(run_root),
        request_id="request.1",
        plan_id="plan.1",
        task_id="task.1",
        seed=0,
        resume_eligible=True,
    )
    checkpoint_path.write_text(
        json.dumps((checkpoint).model_dump(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    run_manifest = RunManifest(
        run_id="run.123",
        run_root=str(run_root),
        latest_checkpoint_ref=str(checkpoint_path),
        current_attempt_id="attempt_0001",
        runtime_backend="docker",
        resumable=True,
    )
    (run_root / "run_manifest.json").write_text(
        json.dumps((run_manifest).model_dump(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    request = RuntimeResumeRequest(
        request_id="resume.1",
        checkpoint_ref=str(checkpoint_path),
        run_root=str(run_root),
        attempt_id="attempt_0002",
        runtime_backend="docker",
    )
    executor = DockerRuntimeExecutor(tmp_path / "executor", run_store_workspace=run_store_workspace)

    resolved_request = executor._resolve_resume_checkpoint_request(request)

    assert resolved_request.run_id == "run.123"
    assert resolved_request.run_root == str(run_root)
    assert resolved_request.checkpoint_ref == str(checkpoint_path)
    assert resolved_request.checkpoint_store_dir == str(checkpoint_path.parent.resolve())


def test_run_store_from_mounted_run_root_accepts_host_serialized_run_root(tmp_path: Path):
    run_root = (tmp_path / "mounted" / "runs" / "run.123").resolve()
    run_root.mkdir(parents=True)
    (run_root / "run_manifest.json").write_text("{}", encoding="utf-8")
    store = RunStore.from_run_root(run_root)

    resolved = store.resolve_run_root("C:\\host\\runs\\run.123")

    assert resolved == run_root
def test_containerize_checkpoint_envelope_restores_docker_open_handle_paths(tmp_path: Path):
    runs_root = tmp_path / "runs"
    run_root = runs_root / "run.123"
    checkpoint_store_dir = tmp_path / "checkpoints"
    attempt_workspace = run_root / "attempts" / "attempt_0001" / "workspace"
    checkpoint_ref = checkpoint_store_dir / "run.123" / "checkpoint.json"
    envelope = CheckpointEnvelope(
        checkpoint_id="checkpoint.run.123.0001",
        runtime_contract_version=RUNTIME_CONTRACT_VERSION,
        runtime_hash="hash",
        run_id="run.123",
        run_root=str(run_root),
        attempt_id="attempt_0001",
        request_id="solve.1",
        plan_id="plan.1",
        task_id="task.1",
        seed=0,
        source_checkpoint_ref=str(checkpoint_ref),
        selected_checkpoint_ref=str(checkpoint_ref),
        runtime_state_snapshot={
            "latest_checkpoint_ref": str(checkpoint_ref),
            "branch_resume_snapshots": {
                "w0": {
                    "branch_plan": {
                        "branch_id": "w0",
                        "parent_frame_id": "frame.root",
                        "request_id": "solve.1",
                    },
                    "shell_state_snapshot": {
                        "open_handles": [
                            AsyncHandle(
                                handle_id="branch.handle",
                                tool_name="branch-tool",
                                sandbox_hash="sandbox",
                                working_directory=str(attempt_workspace / "branches" / "w0"),
                                launch_time=0.0,
                                timeout=60.0,
                                stdout_path=str(attempt_workspace / "branches" / "w0" / "stdout.txt"),
                                stderr_path=str(attempt_workspace / "branches" / "w0" / "stderr.txt"),
                                state="completed",
                                artifact_refs=[str(checkpoint_store_dir / "shared" / "branch-output.json")],
                            ).model_dump()
                        ]
                    },
                }
            },
        },
        shell_state_snapshot={
            "open_handles": [
                AsyncHandle(
                    handle_id="handle.1",
                    tool_name="dummy-tool",
                    sandbox_hash="sandbox",
                    working_directory=str(attempt_workspace),
                    launch_time=0.0,
                    timeout=60.0,
                    stdout_path=str(attempt_workspace / "stdout.txt"),
                    stderr_path=str(attempt_workspace / "stderr.txt"),
                    state="completed",
                    artifact_refs=[
                        str(run_root / "artifacts" / "result.json"),
                        str(checkpoint_store_dir / "shared" / "handle-output.json"),
                    ],
                ).model_dump()
            ]
        },
        attempt_snapshot={
            "run_id": "run.123",
            "run_root": str(run_root),
            "attempt_id": "attempt_0001",
            "resumed_from_checkpoint_ref": str(checkpoint_ref),
        },
        working_state={"selected_checkpoint_refs": [str(checkpoint_ref)]},
    )

    containerized = DockerRuntimeExecutor._containerize_checkpoint_envelope_paths(
        envelope,
        run_mount_root=runs_root,
        checkpoint_store_dir=checkpoint_store_dir,
    ).model_dump()

    assert containerized["run_root"] == "/mnt/runs/run.123"
    assert containerized["selected_checkpoint_ref"] == "/mnt/checkpoints/run.123/checkpoint.json"
    root_handle = containerized["shell_state_snapshot"]["open_handles"]["handles"][0]
    assert root_handle["working_directory"] == "/mnt/runs/run.123/attempts/attempt_0001/workspace"
    assert root_handle["stdout_path"] == "/mnt/runs/run.123/attempts/attempt_0001/workspace/stdout.txt"
    assert root_handle["stderr_path"] == "/mnt/runs/run.123/attempts/attempt_0001/workspace/stderr.txt"
    assert root_handle["artifact_refs"] == [
        "/mnt/runs/run.123/artifacts/result.json",
        "/mnt/checkpoints/shared/handle-output.json",
    ]
    branch_handle = (
        containerized["runtime_state_snapshot"]["branch_resume_snapshots"]["w0"]["shell_state_snapshot"]["open_handles"]["handles"][0]
    )
    assert branch_handle["working_directory"] == "/mnt/runs/run.123/attempts/attempt_0001/workspace/branches/w0"
    assert branch_handle["artifact_refs"] == ["/mnt/checkpoints/shared/branch-output.json"]
    assert containerized["working_state"]["selected_checkpoint_refs"] == ["/mnt/checkpoints/run.123/checkpoint.json"]


def test_materialized_container_resume_keeps_original_checkpoint_store_for_rewrite(tmp_path: Path):
    runs_root = tmp_path / "runs"
    run_root = runs_root / "run.123"
    checkpoint_store_dir = tmp_path / "checkpoints"
    checkpoint_ref = checkpoint_store_dir / "run.123" / "checkpoint.json"
    checkpoint_ref.parent.mkdir(parents=True)
    envelope = CheckpointEnvelope(
        checkpoint_id="checkpoint.run.123.0001",
        runtime_contract_version=RUNTIME_CONTRACT_VERSION,
        runtime_hash="hash",
        run_id="run.123",
        run_root=str(run_root),
        attempt_id="attempt_0001",
        request_id="solve.1",
        plan_id="plan.1",
        task_id="task.1",
        seed=0,
        source_checkpoint_ref=str(checkpoint_ref),
        selected_checkpoint_ref=str(checkpoint_ref),
        runtime_state_snapshot={"latest_checkpoint_ref": str(checkpoint_ref)},
        attempt_snapshot={
            "run_id": "run.123",
            "run_root": str(run_root),
            "attempt_id": "attempt_0001",
            "resumed_from_checkpoint_ref": str(checkpoint_ref),
        },
        working_state={"selected_checkpoint_refs": [str(checkpoint_ref)]},
    )
    checkpoint_ref.write_text(json.dumps((envelope).model_dump(), indent=2, sort_keys=True), encoding="utf-8")
    host_request = RuntimeResumeRequest(
        runtime_backend="docker",
        checkpoint_ref=str(checkpoint_ref),
        checkpoint_store_dir=str(checkpoint_store_dir),
        run_root=str(run_root),
    )
    container_request = RuntimeResumeRequest(
        runtime_backend="docker",
        checkpoint_ref="/mnt/checkpoints/run.123/checkpoint.json",
        checkpoint_store_dir="/mnt/checkpoints",
        run_root="/mnt/runs/run.123",
    )

    materialized_request, mount_dir, rewrite_dir = DockerRuntimeExecutor._materialize_container_resume_checkpoint(
        container_request,
        host_request,
        run_dir=tmp_path / "docker-run",
        run_mount_root=runs_root,
        checkpoint_store_dir=checkpoint_store_dir,
    )

    assert materialized_request.checkpoint_ref == "/mnt/checkpoints/run.123/checkpoint.json"
    assert mount_dir == tmp_path / "docker-run" / "checkpoint_store"
    assert rewrite_dir == checkpoint_store_dir.resolve()
    materialized_checkpoint = mount_dir / "run.123" / "checkpoint.json"
    assert materialized_checkpoint.exists()
    materialized_payload = json.loads(materialized_checkpoint.read_text(encoding="utf-8"))
    assert materialized_payload["source_checkpoint_ref"] == "/mnt/checkpoints/run.123/checkpoint.json"
    rewritten = DockerRuntimeExecutor._rewrite_checkpoint_envelope_paths(
        (CheckpointEnvelope).model_validate(materialized_payload),
        run_mount_root=runs_root,
        checkpoint_store_dir=rewrite_dir,
    )
    assert rewritten.source_checkpoint_ref == str(checkpoint_ref.resolve())
