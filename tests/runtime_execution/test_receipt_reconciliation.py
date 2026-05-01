from __future__ import annotations

from .helpers import (
    pytest,
    ArtifactMode,
    init_runtime,
    ReplayProvider,
    load_runtime,
    TaskRuntime,
    FixedShell,
    ReconcilingReplayProvider,
    _make_direct_response_task,
    _pending_provider_launch_envelope,
    _pending_sync_tool_launch_envelope,
    _pending_async_tool_launch_envelope,
    _branch_owned_terminal_provider_receipt_envelope,
)


def test_direct_response_run_reports_execution_scoped_provider_usage_and_selected_backend(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    runtime.deployment_contract.supported_backends = ["docker", "local"]
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    provider = ReplayProvider(
        [
            {
                "text": "hello from usage",
                "model_name": "replay/small",
                "input_tokens": 7,
                "output_tokens": 5,
                "token_estimate": 12,
                "latency_s": 0.2,
                "dollar_cost": 0.02,
            }
        ]
    )

    result = TaskRuntime(runtime, shell, provider, runtime_backend="local").run_task(
        _make_direct_response_task("usage.direct"),
        0,
    )
    envelope = shell.load_checkpoint_envelope(checkpoint_ref=result.checkpoint_ref)

    assert result.provider_usage["calls"] == 1
    assert result.provider_usage["input_tokens"] == 7
    assert result.provider_usage["output_tokens"] == 5
    assert result.provider_usage["total_tokens"] == 12
    assert result.provider_usage["dollar_cost"] == pytest.approx(0.02)
    assert result.runtime_backend == "local"
    assert envelope.runtime_backend == "local"
    assert {receipt.backend for receipt in envelope.side_effect_ledger["receipts"]} == {"local"}
    assert {row["runtime_backend"] for row in result.trace_rows() if "runtime_backend" in row} == {"local"}

def test_resume_strict_fails_closed_on_unreconciled_provider_launch_without_reissue(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    envelope = _pending_provider_launch_envelope(
        runtime,
        shell,
        task_id="resume.strict-provider-launch",
    )

    provider = ReconcilingReplayProvider([])
    resumed = TaskRuntime(runtime, shell, provider).resume_from_checkpoint(envelope, reconciliation_policy="strict")

    assert resumed.hard_invalid is True
    assert resumed.failure_kind == "receipt_reconciliation_failed"
    assert provider.generate_calls == 0

def test_resume_strict_fails_closed_on_unreconciled_sync_tool_launch_without_reissue(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    envelope = _pending_sync_tool_launch_envelope(
        runtime,
        shell,
        task_id="resume.strict-sync-tool-launch",
    )

    run_tool_calls = 0
    original_run_tool = shell.tool_executor.run_tool

    def counting_run_tool(tool_name, args, task_id):
        nonlocal run_tool_calls
        run_tool_calls += 1
        return original_run_tool(tool_name, args, task_id)

    monkeypatch.setattr(shell.tool_executor, "run_tool", counting_run_tool)

    resumed = TaskRuntime(runtime, shell, ReplayProvider([])).resume_from_checkpoint(envelope, reconciliation_policy="strict")

    assert resumed.hard_invalid is True
    assert resumed.failure_kind == "receipt_reconciliation_failed"
    assert run_tool_calls == 0

def test_resume_best_effort_blocks_unreconciled_provider_launch_without_reissue(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    envelope = _pending_provider_launch_envelope(
        runtime,
        shell,
        task_id="resume.best-effort-provider-launch",
    )

    provider = ReconcilingReplayProvider([])
    resumed = TaskRuntime(runtime, shell, provider).resume_from_checkpoint(envelope, reconciliation_policy="best_effort")

    assert provider.generate_calls == 0
    assert resumed.hard_invalid is False
    assert resumed.artifact["error"] == "recovery_blocked"

def test_resume_best_effort_blocks_unreconciled_sync_tool_launch_without_reissue(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    envelope = _pending_sync_tool_launch_envelope(
        runtime,
        shell,
        task_id="resume.best-effort-sync-tool-launch",
    )

    run_tool_calls = 0
    original_run_tool = shell.tool_executor.run_tool

    def counting_run_tool(tool_name, args, task_id):
        nonlocal run_tool_calls
        run_tool_calls += 1
        return original_run_tool(tool_name, args, task_id)

    monkeypatch.setattr(shell.tool_executor, "run_tool", counting_run_tool)

    resumed = TaskRuntime(runtime, shell, ReplayProvider([])).resume_from_checkpoint(envelope, reconciliation_policy="best_effort")

    assert run_tool_calls == 0
    assert resumed.hard_invalid is False
    assert resumed.artifact["error"] == "recovery_blocked"
    assert resumed.artifact["node_id"] == "sum"
    assert any(
        row.get("event") == "node_recovery_blocked" and row.get("node_id") == "sum"
        for row in resumed.trace_rows()
    )

def test_resume_strict_fails_closed_on_unreconciled_async_tool_launch_without_fabricated_failure(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    envelope = _pending_async_tool_launch_envelope(
        runtime,
        shell,
        task_id="resume.strict-async-tool-launch",
    )
    handle_id = envelope.runtime_state_snapshot.open_handle_ids[0]

    resumed = TaskRuntime(runtime, shell, ReplayProvider([])).resume_from_checkpoint(envelope, reconciliation_policy="strict")

    assert resumed.hard_invalid is True
    assert resumed.failure_kind == "receipt_reconciliation_failed"
    assert shell.open_handles.get(handle_id).state == "running"

def test_resume_best_effort_blocks_unreconciled_async_tool_launch_without_terminalizing_handle(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    envelope = _pending_async_tool_launch_envelope(
        runtime,
        shell,
        task_id="resume.best-effort-async-tool-launch",
    )
    handle_id = envelope.runtime_state_snapshot.open_handle_ids[0]

    resumed = TaskRuntime(runtime, shell, ReplayProvider([])).resume_from_checkpoint(envelope, reconciliation_policy="best_effort")

    assert resumed.hard_invalid is False
    assert resumed.artifact["error"] == "recovery_blocked"
    assert shell.open_handles.get(handle_id).state == "running"
    assert resumed.checkpoint_ref is None

def test_reconciled_provider_launch_restores_completed_root_node_without_restart(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    envelope = _pending_provider_launch_envelope(
        runtime,
        shell,
        task_id="resume.reconciled-provider-launch",
    )

    provider = ReconcilingReplayProvider(
        [],
        reconciled={
            "provider-request.pending": {
                "text": "hello from reconcile",
                "model_name": "replay/small",
                "input_tokens": 5,
                "output_tokens": 4,
                "token_estimate": 9,
            }
        },
    )
    resumed = TaskRuntime(runtime, shell, provider).resume_from_checkpoint(envelope)
    trace_rows = resumed.trace_rows()

    assert provider.reconcile_calls == ["provider-request.pending"]
    assert provider.generate_calls == 0
    assert resumed.hard_invalid is False
    assert resumed.artifact == "hello from reconcile"
    assert sum(
        1
        for row in trace_rows
        if row.get("event") == "node_started" and row.get("node_id") == "respond"
    ) == 0
    assert sum(
        1
        for row in trace_rows
        if row.get("event") == "node_reused_from_checkpoint" and row.get("node_id") == "respond"
    ) == 0
    assert [row["event"] for row in trace_rows if row.get("event") in {"terminal_emitted", "run_failed"}] == [
        "terminal_emitted"
    ]

def test_branch_owned_terminal_receipt_is_not_projected_into_parent_artifacts(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    envelope = _branch_owned_terminal_provider_receipt_envelope(
        runtime,
        shell,
        task_id="resume.branch-owned-guard",
    )

    resumed = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([{"text": "root-owned", "model_name": "replay/small"}]),
    ).resume_from_checkpoint(envelope)
    trace_rows = resumed.trace_rows()

    assert envelope.side_effect_ledger["receipts"][0].branch_id == "w0"
    assert resumed.hard_invalid is False
    assert resumed.artifact == "root-owned"
    assert sum(
        1
        for row in trace_rows
        if row.get("event") == "node_started" and row.get("node_id") == "respond"
    ) == 1
    assert sum(
        1
        for row in trace_rows
        if row.get("event") == "node_reused_from_checkpoint" and row.get("node_id") == "respond"
    ) == 0
