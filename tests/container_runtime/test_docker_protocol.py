from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agintor.runtime.host.backends.docker.executor import DockerRuntimeExecutor
from agintor.providers import LocalDeterministicProvider
from agintor.runtime.project import init_runtime
from agintor.contracts import (
    CheckpointEnvelope,
    RequestFileRef,
    RuntimeResumeRequest,
    RuntimeSolveResponse,
    SolveResult,
)
from agintor.core.versioning import RUNTIME_CONTRACT_VERSION

from .helpers import _capability_exchange


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
            (CheckpointEnvelope(
                    checkpoint_id="checkpoint.resume.request-files",
                    runtime_contract_version=RUNTIME_CONTRACT_VERSION,
                    runtime_hash="runtime-hash",
                    run_id="run.123",
                    run_root=str(run_root.resolve()),
                    request_id="resume.request-files",
                    plan_id="plan.resume.request-files",
                    task_id="task.resume.request-files",
                    seed=0,
                    plan_snapshot={
                        "file_ref_specs": [
                            (RequestFileRef(
                                    file_ref_id="file.resume.request-files",
                                    source_path=str(request_file),
                                    runtime_path=container_request_file_path,
                                    path_root="host_absolute",
                                    host_path=str(request_file.resolve()),
                                )).model_dump()
                        ]
                    },
                    task_payload={},
                )).model_dump(),
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
                (RuntimeSolveResponse(
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
                    )).model_dump(),
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(executor, "_docker_run_argv", fake_docker_run_argv)
    monkeypatch.setattr("agintor.runtime.host.backends.docker.executor.subprocess.run", fake_subprocess_run)

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
