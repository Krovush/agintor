from __future__ import annotations

import json
from pathlib import Path

import pytest

from agintor.storage import state_store
from agintor.runtime.host.backends.docker.executor import DockerRuntimeExecutor
from agintor.contracts import (
    AsyncHandle,
    AttemptManifest,
    CheckpointEnvelope,
    CheckpointReference,
    RunManifest,
    SideEffectReceipt,
)
from agintor.core.versioning import RUNTIME_CONTRACT_VERSION


def test_rewrite_durable_run_paths_rewrites_metadata_and_preserves_runtime_payloads(tmp_path: Path):
    runs_root = tmp_path / "host" / "runs"
    checkpoint_store_dir = tmp_path / "host" / "checkpoints"
    runtime_path = tmp_path / "host" / "runtime"
    run_root = runs_root / "run.123"
    attempt_dir = run_root / "attempts" / "attempt_0001"
    checkpoint_dir = run_root / "checkpoints"
    request_dir = run_root / "request"
    side_effect_dir = run_root / "side_effects"
    event_dir = run_root / "events"
    trace_dir = run_root / "traces"
    state_root = run_root / "state"
    checkpoint_store_dir.mkdir(parents=True)
    runtime_path.mkdir(parents=True)
    attempt_dir.mkdir(parents=True)
    checkpoint_dir.mkdir(parents=True)
    request_dir.mkdir(parents=True)
    side_effect_dir.mkdir(parents=True)
    event_dir.mkdir(parents=True)
    trace_dir.mkdir(parents=True)
    (state_root / "working_memory").mkdir(parents=True)
    (state_root / "recovery" / "fingerprints").mkdir(parents=True)
    (state_root / "short_term").mkdir(parents=True)
    (state_root / "long_term" / "writes").mkdir(parents=True)
    host_request_file = (tmp_path / "host files" / "input file.txt").resolve()
    host_request_file.parent.mkdir(parents=True)
    host_request_file.write_text("input", encoding="utf-8")
    container_request_file = "/mnt/request-files/abc123/input file.txt"
    provider_text = f"provider mentioned /mnt/runtime and {container_request_file}"
    request_file_reverse_map = {container_request_file: str(host_request_file)}
    filesystem_write_ref = {
        "output": {
            "updated_files": [{"path": container_request_file, "diff": f"--- {container_request_file}"}],
            "applied": True,
        },
        "writes": [
            {
                "path": container_request_file,
                "before_exists": True,
                "before_digest": "before",
                "after_exists": True,
                "after_digest": "after",
            }
        ],
        "path": container_request_file,
        "failed_path": container_request_file,
        "runtime_dir": "/mnt/runtime",
    }

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
        runtime_contract_version=RUNTIME_CONTRACT_VERSION,
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
            "artifacts": {
                "patch_result": {
                    "updated_files": [{"path": container_request_file, "diff": "stub"}],
                    "target": container_request_file,
                }
            },
            "branch_resume_snapshots": {
                "w0": {
                    "branch_plan": {
                        "branch_id": "w0",
                        "parent_frame_id": "frame.root",
                        "request_id": "solve.1",
                    },
                    "artifacts": {
                        "branch_output": {
                            "path": container_request_file,
                            "text": f"branch saw {container_request_file}",
                        }
                    },
                    "side_effect_receipts": [
                        SideEffectReceipt(
                            side_effect_id="provider-completion.branch",
                            action_fingerprint="provider-completion.branch",
                            idempotency_key="provider-completion.branch",
                            action_kind="provider_completion",
                            request_digest="provider-completion.branch",
                            backend="docker",
                            branch_id="w0",
                            status="completed",
                            result_ref={"text": provider_text, "model_name": "test/model"},
                        ).model_dump()
                    ],
                    "shell_state_snapshot": {
                        "open_handles": [
                            AsyncHandle(
                                handle_id="branch.handle.1",
                                tool_name="branch-tool",
                                sandbox_hash="sandbox-hash",
                                working_directory="/mnt/runs/run.123/attempts/attempt_0001/workspace/branches/w0",
                                launch_time=0.0,
                                timeout=60.0,
                                stdout_path="/mnt/runs/run.123/attempts/attempt_0001/workspace/branches/w0/stdout.txt",
                                stderr_path="/mnt/runs/run.123/attempts/attempt_0001/workspace/branches/w0/stderr.txt",
                                state="completed",
                                artifact_refs=[
                                    "/mnt/runs/run.123/artifacts/branch-result.json",
                                    "/mnt/checkpoints/shared/branch-handle-output.json",
                                ],
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
                    side_effect_id="filesystem-write.1",
                    action_fingerprint="filesystem-write.1",
                    idempotency_key="filesystem-write.1",
                    action_kind="filesystem_write",
                    request_digest="filesystem-write.1",
                    backend="docker",
                    result_ref={
                        **filesystem_write_ref,
                        "opaque_path": "/mnt/checkpoints/shared/receipt-payload-should-stay.json",
                    },
                ),
                SideEffectReceipt(
                    side_effect_id="provider-completion.1",
                    action_fingerprint="provider-completion.1",
                    idempotency_key="provider-completion.1",
                    action_kind="provider_completion",
                    request_digest="provider-completion.1",
                    backend="docker",
                    status="completed",
                    result_ref={"text": provider_text, "model_name": "test/model"},
                ),
            ]
        },
        working_state={
            "current_objective": "/mnt/checkpoints/shared/working-summary-should-stay.json",
            "accepted_constraints": [container_request_file],
            "selected_checkpoint_refs": [
                "/mnt/runs/run.123/checkpoints/LATEST.json",
                "/mnt/checkpoints/shared/resume-source.json",
            ],
        },
        trace_cursor={"materialization_state_ref": "/mnt/runs/run.123/trace-cursor-should-stay.json"},
    )

    (run_root / "run_manifest.json").write_text(
        json.dumps((run_manifest).model_dump(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (attempt_dir / "attempt_manifest.json").write_text(
        json.dumps((attempt_manifest).model_dump(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (checkpoint_dir / "checkpoint.run.123.0001.json").write_text(
        json.dumps((checkpoint_envelope).model_dump(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (checkpoint_dir / "LATEST.json").write_text(
        json.dumps((checkpoint_ref).model_dump(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (checkpoint_dir / "index.json").write_text(
        json.dumps([(checkpoint_ref).model_dump()], indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (request_dir / "request.json").write_text(
        json.dumps({"request_kind": "runtime_solve_request", "payload": {"path": container_request_file}}, indent=2),
        encoding="utf-8",
    )
    (request_dir / "plan.json").write_text(
        json.dumps({"file_refs": [container_request_file]}, indent=2),
        encoding="utf-8",
    )
    (request_dir / "task.json").write_text(
        json.dumps({"file_paths": [container_request_file]}, indent=2),
        encoding="utf-8",
    )
    standalone_receipt = SideEffectReceipt(
        side_effect_id="filesystem-write.standalone",
        action_fingerprint="filesystem-write.standalone",
        idempotency_key="filesystem-write.standalone",
        action_kind="filesystem_write",
        request_digest="filesystem-write.standalone",
        backend="docker",
        status="completed",
        result_ref=filesystem_write_ref,
    )
    (side_effect_dir / "receipt.1.json").write_text(
        json.dumps((standalone_receipt).model_dump(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (event_dir / "000001.tool_operation.json").write_text(
        json.dumps(
            {
                "event_id": "event.1",
                "request_id": "solve.1",
                "plan_id": "plan.1",
                "sequence_no": 1,
                "event": "tool_operation",
                "payload": {
                    "path": container_request_file,
                    "checkpoint_ref": "/mnt/runs/run.123/checkpoints/LATEST.json",
                    "runtime_dir": "/mnt/runtime",
                },
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (trace_dir / "trace.json").write_text(
        json.dumps(
            {
                "input_path": container_request_file,
                "message": f"read {container_request_file}",
                "checkpoint_ref": "/mnt/runs/run.123/checkpoints/LATEST.json",
                "runtime_dir": "/mnt/runtime",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (state_root / "working_memory" / "checkpoint.run.123.0001.json").write_text(
        json.dumps(
            {
                "accepted_constraints": [container_request_file],
                "selected_checkpoint_refs": ["/mnt/runs/run.123/checkpoints/LATEST.json"],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (state_root / "recovery" / "recovery.1.json").write_text(
        json.dumps(
            {
                "recovery_attempt_id": "recovery.1",
                "selected_checkpoint_ref": "/mnt/runs/run.123/checkpoints/LATEST.json",
                "source_checkpoint_ref": "/mnt/checkpoints/shared/resume-source.json",
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (state_root / "recovery" / "fingerprints" / "fingerprint.1.json").write_text(
        json.dumps(
            {"fingerprint_id": "fingerprint.1", "source_checkpoint_ref": "/mnt/runs/run.123/checkpoints/LATEST.json"},
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (state_root / "long_term" / "writes" / "checkpoint.run.123.0001.jsonl").write_text(
        json.dumps({"write_id": "write.1", "payload_ref": container_request_file}) + "\n",
        encoding="utf-8",
    )
    (state_root / "short_term" / "checkpoint.run.123.0001.json").write_text(
        json.dumps({"node": {"content": provider_text, "artifact_ref": container_request_file}}, indent=2),
        encoding="utf-8",
    )

    DockerRuntimeExecutor._rewrite_durable_run_paths(
        run_root,
        runtime_path=runtime_path,
        run_mount_root=runs_root,
        checkpoint_store_dir=checkpoint_store_dir,
        request_file_reverse_map=request_file_reverse_map,
    )

    rewritten_run_manifest = json.loads((run_root / "run_manifest.json").read_text(encoding="utf-8"))
    rewritten_attempt_manifest = json.loads((attempt_dir / "attempt_manifest.json").read_text(encoding="utf-8"))
    rewritten_checkpoint = json.loads((checkpoint_dir / "checkpoint.run.123.0001.json").read_text(encoding="utf-8"))
    rewritten_latest = json.loads((checkpoint_dir / "LATEST.json").read_text(encoding="utf-8"))
    rewritten_index = json.loads((checkpoint_dir / "index.json").read_text(encoding="utf-8"))
    rewritten_request = json.loads((request_dir / "request.json").read_text(encoding="utf-8"))
    rewritten_plan = json.loads((request_dir / "plan.json").read_text(encoding="utf-8"))
    rewritten_task = json.loads((request_dir / "task.json").read_text(encoding="utf-8"))
    rewritten_side_effect = json.loads((side_effect_dir / "receipt.1.json").read_text(encoding="utf-8"))
    rewritten_event = json.loads((event_dir / "000001.tool_operation.json").read_text(encoding="utf-8"))
    rewritten_trace = json.loads((trace_dir / "trace.json").read_text(encoding="utf-8"))
    rewritten_working_memory = json.loads(
        (state_root / "working_memory" / "checkpoint.run.123.0001.json").read_text(encoding="utf-8")
    )
    rewritten_recovery = json.loads((state_root / "recovery" / "recovery.1.json").read_text(encoding="utf-8"))
    rewritten_fingerprint = json.loads(
        (state_root / "recovery" / "fingerprints" / "fingerprint.1.json").read_text(encoding="utf-8")
    )
    rewritten_write_log = json.loads(
        (state_root / "long_term" / "writes" / "checkpoint.run.123.0001.jsonl").read_text(encoding="utf-8")
    )
    rewritten_short_term = json.loads(
        (state_root / "short_term" / "checkpoint.run.123.0001.json").read_text(encoding="utf-8")
    )

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
    rewritten_handles = rewritten_checkpoint["shell_state_snapshot"]["open_handles"]["handles"]
    assert rewritten_handles[0]["working_directory"] == str((attempt_dir / "workspace").resolve())
    assert rewritten_handles[0]["stdout_path"] == str((attempt_dir / "workspace" / "stdout.txt").resolve())
    assert rewritten_handles[0]["stderr_path"] == str((attempt_dir / "workspace" / "stderr.txt").resolve())
    assert rewritten_handles[0]["artifact_refs"] == [
        str((run_root / "artifacts" / "result.json").resolve()),
        str((checkpoint_store_dir / "shared" / "handle-output.json").resolve()),
    ]
    assert rewritten_latest["ref"] == str((checkpoint_dir / "checkpoint.run.123.0001.json").resolve())
    assert rewritten_latest["run_root"] == str(run_root.resolve())
    assert rewritten_index[0]["ref"] == str((checkpoint_dir / "checkpoint.run.123.0001.json").resolve())
    assert rewritten_index[0]["run_root"] == str(run_root.resolve())
    assert rewritten_checkpoint["runtime_state_snapshot"]["artifacts"]["patch_result"]["target"] == container_request_file
    assert rewritten_checkpoint["runtime_state_snapshot"]["artifacts"]["patch_result"]["updated_files"][0]["path"] == (
        container_request_file
    )
    branch_snapshot = rewritten_checkpoint["runtime_state_snapshot"]["branch_resume_snapshots"]["w0"]
    assert branch_snapshot["artifacts"]["branch_output"]["path"] == container_request_file
    assert branch_snapshot["artifacts"]["branch_output"]["text"] == f"branch saw {container_request_file}"
    assert branch_snapshot["side_effect_receipts"][0]["result_ref"]["text"] == provider_text
    branch_handles = branch_snapshot["shell_state_snapshot"]["open_handles"]["handles"]
    assert branch_handles[0]["working_directory"] == str((attempt_dir / "workspace" / "branches" / "w0").resolve())
    assert branch_handles[0]["stdout_path"] == str(
        (attempt_dir / "workspace" / "branches" / "w0" / "stdout.txt").resolve()
    )
    assert branch_handles[0]["stderr_path"] == str(
        (attempt_dir / "workspace" / "branches" / "w0" / "stderr.txt").resolve()
    )
    assert branch_handles[0]["artifact_refs"] == [
        str((run_root / "artifacts" / "branch-result.json").resolve()),
        str((checkpoint_store_dir / "shared" / "branch-handle-output.json").resolve()),
    ]
    checkpoint_receipt_ref = rewritten_checkpoint["side_effect_ledger"]["receipts"][0]["result_ref"]
    assert checkpoint_receipt_ref["path"] == container_request_file
    assert checkpoint_receipt_ref["failed_path"] == container_request_file
    assert checkpoint_receipt_ref["runtime_dir"] == "/mnt/runtime"
    assert checkpoint_receipt_ref["writes"][0]["path"] == container_request_file
    assert checkpoint_receipt_ref["output"]["updated_files"][0]["path"] == container_request_file
    assert checkpoint_receipt_ref["output"]["updated_files"][0]["diff"] == f"--- {container_request_file}"
    assert checkpoint_receipt_ref["opaque_path"] == "/mnt/checkpoints/shared/receipt-payload-should-stay.json"
    assert rewritten_checkpoint["side_effect_ledger"]["receipts"][1]["result_ref"]["text"] == provider_text
    assert rewritten_checkpoint["working_state"]["accepted_constraints"] == [container_request_file]
    assert rewritten_checkpoint["working_state"]["selected_checkpoint_refs"] == [
        str((checkpoint_dir / "LATEST.json").resolve()),
        str((checkpoint_store_dir / "shared" / "resume-source.json").resolve()),
    ]
    assert rewritten_checkpoint["working_state"]["current_objective"] == "/mnt/checkpoints/shared/working-summary-should-stay.json"
    assert rewritten_checkpoint["trace_cursor"]["materialization_state_ref"] == "/mnt/runs/run.123/trace-cursor-should-stay.json"
    assert rewritten_request["payload"]["path"] == container_request_file
    assert rewritten_plan["file_refs"] == [container_request_file]
    assert rewritten_task["file_paths"] == [container_request_file]
    assert rewritten_side_effect["result_ref"]["path"] == container_request_file
    assert rewritten_side_effect["result_ref"]["runtime_dir"] == "/mnt/runtime"
    assert rewritten_side_effect["result_ref"]["writes"][0]["path"] == container_request_file
    assert rewritten_event["payload"]["path"] == container_request_file
    assert rewritten_event["payload"]["checkpoint_ref"] == "/mnt/runs/run.123/checkpoints/LATEST.json"
    assert rewritten_event["payload"]["runtime_dir"] == "/mnt/runtime"
    assert rewritten_trace["input_path"] == container_request_file
    assert rewritten_trace["message"] == f"read {container_request_file}"
    assert rewritten_trace["checkpoint_ref"] == "/mnt/runs/run.123/checkpoints/LATEST.json"
    assert rewritten_trace["runtime_dir"] == "/mnt/runtime"
    assert rewritten_working_memory["accepted_constraints"] == [container_request_file]
    assert rewritten_working_memory["selected_checkpoint_refs"] == [str((checkpoint_dir / "LATEST.json").resolve())]
    assert rewritten_recovery["selected_checkpoint_ref"] == str((checkpoint_dir / "LATEST.json").resolve())
    assert rewritten_recovery["source_checkpoint_ref"] == str(
        (checkpoint_store_dir / "shared" / "resume-source.json").resolve()
    )
    assert rewritten_fingerprint["source_checkpoint_ref"] == str((checkpoint_dir / "LATEST.json").resolve())
    assert rewritten_write_log["payload_ref"] == container_request_file
    assert rewritten_short_term["node"]["content"] == provider_text
    assert rewritten_short_term["node"]["artifact_ref"] == container_request_file
    store = state_store.open_state_store(run_root)
    with store._connection() as conn:
        receipt_row = conn.execute(
            "SELECT result_ref_json FROM receipts WHERE side_effect_id = ?",
            ("filesystem-write.standalone",),
        ).fetchone()
        artifact_row = conn.execute(
            "SELECT artifact_ref FROM artifacts WHERE receipt_id = ? AND artifact_kind = ?",
            ("filesystem-write.standalone", "path"),
        ).fetchone()
        event_row = conn.execute(
            "SELECT payload_json FROM runtime_events WHERE event_id = ?",
            ("event.1",),
        ).fetchone()
        working_memory_rows = conn.execute(
            "SELECT canonical_ref, payload_json FROM working_memory_snapshots WHERE checkpoint_id = ?",
            ("checkpoint.run.123.0001",),
        ).fetchall()
    assert receipt_row is not None
    assert json.loads(receipt_row["result_ref_json"])["path"] == container_request_file
    assert artifact_row is not None
    assert artifact_row["artifact_ref"] == container_request_file
    assert event_row is not None
    assert json.loads(event_row["payload_json"])["payload"]["path"] == container_request_file
    standalone_working_rows = [
        row
        for row in working_memory_rows
        if row["canonical_ref"] == "state/working_memory/checkpoint.run.123.0001.json"
    ]
    assert standalone_working_rows
    assert json.loads(standalone_working_rows[0]["payload_json"])["selected_checkpoint_refs"] == [
        str((checkpoint_dir / "LATEST.json").resolve())
    ]
    fully_rewritten_payloads = [
        run_root / "run_manifest.json",
        attempt_dir / "attempt_manifest.json",
        checkpoint_dir / "LATEST.json",
        checkpoint_dir / "index.json",
        state_root / "recovery" / "recovery.1.json",
        state_root / "recovery" / "fingerprints" / "fingerprint.1.json",
    ]
    for path in fully_rewritten_payloads:
        text = path.read_text(encoding="utf-8")
        assert "/mnt/request-files" not in text
        assert "/mnt/runs" not in text
        assert "/mnt/checkpoints" not in text
        assert "/mnt/runtime" not in text


def test_rewrite_durable_run_paths_fails_closed_on_invalid_checkpoint_payload(tmp_path: Path):
    runs_root = tmp_path / "runs"
    run_root = runs_root / "run.partial"
    checkpoint_dir = run_root / "checkpoints"
    checkpoint_dir.mkdir(parents=True)
    manifest = RunManifest(
        run_id="run.partial",
        run_root="/mnt/runs/run.partial",
        request_id="solve.partial",
        evaluation_unit_id="solve.partial",
        request_mode="benchmark",
        runtime_backend="docker",
    )
    manifest_path = run_root / "run_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest.model_dump(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (checkpoint_dir / "checkpoint.invalid.json").write_text(
        json.dumps({"not": "a checkpoint"}),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="failed to rewrite durable run path payload"):
        DockerRuntimeExecutor._rewrite_durable_run_paths(
            run_root,
            runtime_path=tmp_path / "runtime",
            run_mount_root=runs_root,
            checkpoint_store_dir=tmp_path / "checkpoint-store",
        )

    assert json.loads(manifest_path.read_text(encoding="utf-8"))["run_root"] == "/mnt/runs/run.partial"


@pytest.mark.parametrize(
    "relative_payload_path",
    [
        Path("state") / "working_memory" / "checkpoint.partial.json",
        Path("state") / "recovery" / "recovery.partial.json",
    ],
)
def test_rewrite_durable_run_paths_fails_closed_on_invalid_state_payload(
    tmp_path: Path,
    relative_payload_path: Path,
):
    runs_root = tmp_path / "runs"
    run_root = runs_root / "run.partial"
    payload_path = run_root / relative_payload_path
    payload_path.parent.mkdir(parents=True)
    manifest = RunManifest(
        run_id="run.partial",
        run_root="/mnt/runs/run.partial",
        request_id="solve.partial",
        evaluation_unit_id="solve.partial",
        request_mode="benchmark",
        runtime_backend="docker",
    )
    manifest_path = run_root / "run_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest.model_dump(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    payload_path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(RuntimeError, match="failed to rewrite durable run path payload"):
        DockerRuntimeExecutor._rewrite_durable_run_paths(
            run_root,
            runtime_path=tmp_path / "runtime",
            run_mount_root=runs_root,
            checkpoint_store_dir=tmp_path / "checkpoint-store",
        )

    assert json.loads(manifest_path.read_text(encoding="utf-8"))["run_root"] == "/mnt/runs/run.partial"
