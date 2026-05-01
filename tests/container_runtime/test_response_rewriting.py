from __future__ import annotations

from pathlib import Path

from agintor.runtime.host.backends.docker.executor import DockerRuntimeExecutor
from agintor.contracts import (
    OpenAITraceContext,
    RunResult,
    RuntimeBatchResponse,
    RuntimeSolveResponse,
    SolveResult,
)

from .helpers import _capability_exchange


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
                "updated_files": [{"path": container_path, "diff": f"--- {container_path}"}],
                "target": container_path,
                "ref": container_path,
                "text": f"provider mentioned {container_path}",
            },
            status="best_effort",
            verification_status="best_effort",
            summary="ok",
            post_message_short_term_export=[
                {
                    "kind": "artifact_path",
                    "path": container_path,
                    "summary": f"provider mentioned {container_path}",
                }
            ],
        ),
    )
    executor = DockerRuntimeExecutor(tmp_path / "executor")

    executor._rewrite_solve_response_paths(
        response,
        workspace_dir,
        request_file_reverse_map={container_path: str(host_file)},
    )

    assert response.solve_result.artifact["updated_files"][0]["path"] == str(host_file)
    assert response.solve_result.artifact["updated_files"][0]["diff"] == f"--- {container_path}"
    assert response.solve_result.artifact["target"] == container_path
    assert response.solve_result.artifact["ref"] == container_path
    assert response.solve_result.artifact["text"] == f"provider mentioned {container_path}"
    assert response.solve_result.post_message_short_term_export[0]["path"] == str(host_file)
    assert (
        response.solve_result.post_message_short_term_export[0]["summary"]
        == f"provider mentioned {container_path}"
    )
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


def test_rewrite_solve_response_paths_restores_inline_trace_ref_rows(tmp_path: Path):
    runtime_path = (tmp_path / "host" / "runtime").resolve()
    runs_root = (tmp_path / "host" / "runs").resolve()
    host_file = (tmp_path / "host files" / "input.txt").resolve()
    container_file = "/mnt/request-files/abc123/input.txt"
    workspace_dir = tmp_path / "docker-workspace"
    runtime_path.mkdir(parents=True)
    runs_root.mkdir(parents=True)
    host_file.parent.mkdir(parents=True)
    host_file.write_text("input", encoding="utf-8")
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
            mode="user_request",
            artifact={"ok": True},
            status="best_effort",
            verification_status="best_effort",
            summary="ok",
            trace_ref=RunResult.encode_trace_ref(
                [
                    {
                        "event": "tool_operation",
                        "trace_context": {"runtime_dir": "/mnt/runtime"},
                        "payload": {
                            "path": "/mnt/runs/run.123/artifacts/result.json",
                            "input_path": container_file,
                            "text": "mention /mnt/runtime",
                        },
                    }
                ]
            ),
        ),
    )
    executor = DockerRuntimeExecutor(tmp_path / "executor")

    executor._rewrite_solve_response_paths(
        response,
        workspace_dir,
        runtime_path=runtime_path,
        run_mount_root=runs_root,
        request_file_reverse_map={container_file: str(host_file)},
    )

    trace_rows = RunResult.decode_trace_ref(response.solve_result.trace_ref)
    assert trace_rows[0]["trace_context"]["runtime_dir"] == str(runtime_path)
    assert trace_rows[0]["payload"]["path"] == str((runs_root / "run.123" / "artifacts" / "result.json").resolve())
    assert trace_rows[0]["payload"]["input_path"] == str(host_file)
    assert trace_rows[0]["payload"]["text"] == "mention /mnt/runtime"


def test_rewrite_batch_response_paths_restores_trace_context_runtime_dir(tmp_path: Path):
    runtime_path = (tmp_path / "host" / "runtime").resolve()
    runs_root = (tmp_path / "host" / "runs").resolve()
    host_file = (tmp_path / "host files" / "input.txt").resolve()
    container_file = "/mnt/request-files/abc123/input.txt"
    workspace_dir = tmp_path / "docker-workspace"
    runtime_path.mkdir(parents=True)
    runs_root.mkdir(parents=True)
    host_file.parent.mkdir(parents=True)
    host_file.write_text("input", encoding="utf-8")
    workspace_dir.mkdir(parents=True)
    response = RuntimeBatchResponse(
        request_id="batch.1",
        capability_exchange=_capability_exchange(),
        run_results=[
            RunResult(
                request_id="request.1",
                run_id="run.123",
                run_root="/mnt/runs/run.123",
                task_id="task.1",
                seed=0,
                artifact={"path": "/mnt/runtime/policy.py", "text": "mention /mnt/runtime"},
                verifier_score=0.0,
                cost=0.0,
                latency=0.0,
                faults=0,
                trace=[
                    {
                        "event": "tool_operation",
                        "trace_context": {"runtime_dir": "/mnt/runtime"},
                        "payload": {
                            "path": "/mnt/runs/run.123/artifacts/result.json",
                            "input_path": container_file,
                            "text": "mention /mnt/runtime",
                        },
                    }
                ],
                trace_context=OpenAITraceContext(runtime_dir="/mnt/runtime"),
            )
        ],
        provider_usage={},
    )
    executor = DockerRuntimeExecutor(tmp_path / "executor")

    executor._rewrite_response_paths(
        response,
        workspace_dir,
        runtime_path=runtime_path,
        run_mount_root=runs_root,
        request_file_reverse_map={container_file: str(host_file)},
    )

    run = response.run_results[0]
    assert run.run_root == str((runs_root / "run.123").resolve())
    assert run.trace_context.runtime_dir == str(runtime_path)
    assert run.artifact["path"] == str((runtime_path / "policy.py").resolve())
    assert run.artifact["text"] == "mention /mnt/runtime"
    assert run.trace[0]["trace_context"]["runtime_dir"] == str(runtime_path)
    assert run.trace[0]["payload"]["path"] == str((runs_root / "run.123" / "artifacts" / "result.json").resolve())
    assert run.trace[0]["payload"]["input_path"] == str(host_file)
    assert run.trace[0]["payload"]["text"] == "mention /mnt/runtime"
