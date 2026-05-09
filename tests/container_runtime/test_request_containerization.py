from __future__ import annotations

from pathlib import Path

from agintor.runtime.host.backends.docker.executor import DockerRuntimeExecutor
from agintor.runtime.api import load_solve_request, runtime_solve_request_for_user_request
from agintor.contracts import BenchmarkTask, RunResult, RuntimeBatchRequest, RuntimeBatchResponse, RuntimeTaskInvocation

from .helpers import _capability_exchange, _task


def test_containerize_solve_request_rewrites_durable_run_root(tmp_path: Path):
    run_root = tmp_path / "host" / "runs" / "run.123"
    run_root.mkdir(parents=True)
    request = runtime_solve_request_for_user_request(
        runtime_backend="docker",
        seed=0,
        solve_request=load_solve_request(prompt="Say hello."),
    ).model_copy(
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
    private_task = _task("one").model_copy(
        update={
            "expected": None,
            "private_expected": 7,
            "verifier_type": "number_exact",
        }
    )

    def fake_run_batch_protocol(runtime_dir, request, **kwargs):
        captured["request"] = request
        invocation = request.invocations[0]
        return RuntimeBatchResponse(
            request_id=request.request_id,
            capability_exchange=_capability_exchange(),
            run_results=[
                RunResult(
                    request_id=invocation.request_id,
                    run_id=invocation.run_id,
                    run_root=invocation.run_root,
                    attempt_id=invocation.attempt_id,
                    runtime_hash="runtime",
                    runtime_backend=invocation.runtime_backend,
                    task_id=invocation.task.task_id,
                    seed=invocation.seed,
                    artifact=7,
                    verifier_score=0.0,
                    cost=0.0,
                    latency=0.1,
                    faults=0,
                )
            ],
            provider_usage={},
        )

    monkeypatch.setattr(executor, "run_batch_protocol", fake_run_batch_protocol)

    runs = executor.run_batch(
        "dummy-runtime",
        [(private_task, 0)],
        provider=None,  # type: ignore[arg-type]
    )
    request = captured["request"]
    invocation = request.invocations[0]
    payload = request.model_dump(mode="json")

    assert runs[0].verifier_score == 1.0
    assert request.request_id.startswith("docker.")
    assert invocation.task.private_expected is None
    assert invocation.task.verifier_type == "none"
    assert invocation.authoritative_task is not None
    assert invocation.authoritative_task.private_expected == 7
    assert "authoritative_task" not in payload["invocations"][0]
    assert "private_expected" not in payload["invocations"][0]["task"]


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
    ).model_copy(
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


def test_containerize_solve_request_file_refs_preserves_payload_text_that_mentions_paths(tmp_path: Path):
    run_root = tmp_path / "host" / "runs" / "run.123"
    run_root.mkdir(parents=True)
    host_file = tmp_path / "input.txt"
    host_file.write_text("hello", encoding="utf-8")
    solve_request = load_solve_request(prompt=f"Inspect {host_file}.")
    solve_request.context_items = [
        {"file_path": str(host_file), "owner": "ops"},
        {"note": f"literal mention should stay {host_file}"},
    ]
    request = runtime_solve_request_for_user_request(
        runtime_backend="docker",
        seed=0,
        solve_request=solve_request,
    ).model_copy(
        update={
            "run_id": "run.123",
            "run_root": str(run_root),
            "attempt_id": "attempt_0001",
        }
    )

    container_request, _, _ = DockerRuntimeExecutor._containerize_solve_request_file_refs(
        request,
        run_mount_root=run_root.parent,
    )

    context_items = container_request.solve_request.context_items
    assert context_items[0]["file_path"].startswith("/mnt/request-files/")
    assert context_items[0]["owner"] == "ops"
    assert context_items[1]["note"] == f"literal mention should stay {host_file}"
    assert container_request.solve_request.prompt == f"Inspect {host_file}."


def test_containerize_task_file_refs_preserves_non_path_payload_strings(tmp_path: Path):
    run_root = tmp_path / "host" / "runs" / "run.123"
    run_root.mkdir(parents=True)
    host_file = tmp_path / "input.txt"
    host_file.write_text("hello", encoding="utf-8")
    task = BenchmarkTask(
        task_id="task.path-text",
        family="tool",
        prompt=f"Prompt mentions {host_file}",
        task_type="bounded_repo_patch",
        file_paths=[str(host_file)],
        context_items=[
            {"file_path": str(host_file), "owner": "api"},
            {"note": f"plain text {host_file}"},
        ],
        operations=[
            {
                "op_id": "patch",
                "kind": "repo_patch",
                "output_key": "patch_result",
                "description": f"Operation text mentions {host_file}",
                "args": {
                    "target_file_paths": [str(host_file)],
                    "comment": f"leave this text alone {host_file}",
                },
            }
        ],
        expected={"message": f"expected mentions {host_file}"},
        metadata={
            "input_binding_overrides": {
                "patch": [
                    {
                        "target_arg": "path",
                        "source_kind": "request_file",
                        "source_ref": str(host_file),
                        "required": True,
                    },
                    {
                        "target_arg": "upstream",
                        "source_kind": "upstream_output",
                        "source_ref": str(host_file),
                        "required": True,
                    },
                ]
            }
        },
    )

    rewritten_task, _, _ = DockerRuntimeExecutor._containerize_task_file_refs(
        task,
        run_mount_root=run_root.parent,
    )

    assert rewritten_task.file_paths[0].startswith("/mnt/request-files/")
    assert rewritten_task.context_items[0]["file_path"].startswith("/mnt/request-files/")
    assert rewritten_task.context_items[1]["note"] == f"plain text {host_file}"
    assert rewritten_task.operations[0].args["target_file_paths"][0].startswith("/mnt/request-files/")
    assert rewritten_task.operations[0].args["comment"] == f"leave this text alone {host_file}"
    assert rewritten_task.operations[0].description == f"Operation text mentions {host_file}"
    assert rewritten_task.prompt == f"Prompt mentions {host_file}"
    assert rewritten_task.expected == {"message": f"expected mentions {host_file}"}
    binding_overrides = rewritten_task.metadata["input_binding_overrides"]["patch"]
    assert binding_overrides[0]["source_ref"].startswith("/mnt/request-files/")
    assert binding_overrides[1]["source_ref"] == str(host_file)


def test_load_solve_request_trims_space_path_before_instruction_clause(tmp_path: Path):
    request_file = tmp_path / "request files" / "input data.txt"
    request_file.parent.mkdir()
    request_file.write_text("content", encoding="utf-8")

    request = load_solve_request(f"Update the file at {request_file} by appending a line.")

    assert request.file_paths == [str(request_file.resolve())]
    assert request.request_file_refs[0].host_path == str(request_file.resolve())


def test_load_solve_request_preserves_prompt_internal_whitespace(tmp_path: Path):
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("first line\n    indented line\n\nlast line\n", encoding="utf-8")

    request = load_solve_request(prompt_file=prompt_file)

    assert request.prompt == "first line\n    indented line\n\nlast line"
