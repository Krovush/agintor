from __future__ import annotations

from .helpers import (
    subprocess,
    sys,
    Event,
    pytest,
    ArtifactMode,
    ResumeRecoveryError,
    init_runtime,
    LocalDeterministicProvider,
    ReplayProvider,
    compile_execution_plan_from_task,
    load_runtime,
    load_runtime_profile,
    TaskRuntime,
    AsyncHandle,
    Checkpoint,
    CheckpointEnvelope,
    ChildSpec,
    QueuedFrameSnapshot,
    SideEffectReceipt,
    FixedShell,
    _AsyncProcessRecord,
    now_ts,
    ReconcilingReplayProvider,
    DelayingReplayProvider,
    _make_direct_response_task,
    _make_exact_direct_response_task,
    _make_parallel_direct_response_task,
    _build_mixed_frontier_context,
    _branch_result,
    _canonical_root_snapshot,
    _force_horizontal,
    _checkpoint_for_boundary,
    _branch_resume_checkpoint_envelope,
    _make_branch_cleanup_context,
)


def test_vertical_mode_executes_explicit_merge_and_verify_plan_nodes(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_parallel_direct_response_task("vertical.plan").model_copy(
        update={
            "verifier_type": "trace_event",
            "expected": "merge_completed",
            "verification_required": True,
        }
    )
    plan = compile_execution_plan_from_task(
        task,
        request_id="vertical.plan.request",
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    merge_node_ids = [node.node_id for node in plan.nodes if str(node.node_kind) == "merge"]
    verify_node_ids = [node.node_id for node in plan.nodes if str(node.node_kind) == "verify"]
    assert merge_node_ids
    assert verify_node_ids

    monkeypatch.setattr(runtime.topology, "select_mode", lambda ctx, frame, operations: "vertical" if len(operations) > 1 else "single")

    def _children(ctx, frame, operations):
        children = []
        for index, operation in enumerate(operations):
            children.append(
                ChildSpec(
                    child_id=f"child-{index}",
                    role="child",
                    instruction=operation.instruction,
                    tool_scope=[],
                    model_class="small",
                    required_capabilities=["plan"],
                    required_permissions=["local"],
                    dependency_ids=list(operation.dependencies),
                    comm_mode="summary_only",
                    resume_policy="checkpoint",
                    init_summary={"op_id": operation.node_id, "output_key": operation.output_key},
                )
            )
        return children

    monkeypatch.setattr(runtime.topology, "propose_children", _children)

    result = TaskRuntime(
        runtime,
        shell,
        ReplayProvider(
            [
                {"text": "branch-a", "model_name": "replay/small"},
                {"text": "branch-b", "model_name": "replay/small"},
            ]
        ),
    ).run_task(task, 0)

    completed_ids = {row.get("node_id") for row in result.trace_rows() if row.get("event") == "node_completed"}
    assert set(merge_node_ids).issubset(completed_ids)
    assert set(verify_node_ids).issubset(completed_ids)
    assert any(row.get("event") == "merge_completed" for row in result.trace_rows())
    assert result.verifier_score == 1.0

def test_root_empty_frontier_does_not_reschedule_completed_plan_nodes(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_parallel_direct_response_task("vertical.plan.empty-frontier").model_copy(
        update={
            "verifier_type": "trace_event",
            "expected": "merge_completed",
            "verification_required": True,
        }
    )
    plan = compile_execution_plan_from_task(
        task,
        request_id="vertical.plan.empty-frontier.request",
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    merge_node = next(node for node in plan.nodes if str(node.node_kind) == "merge")
    verify_node = next(node for node in plan.nodes if str(node.node_kind) == "verify")
    queued_frame = QueuedFrameSnapshot(
        frame_id="frame-root-continuation",
        request_id=plan.request_id,
        plan_id=plan.plan_id,
        objective=plan.objective,
        operation_ids=[node.node_id for node in plan.nodes],
        depth=0,
        role="root",
        trace_context=plan.trace_context,
        tool_scope=sorted(shell.tool_registry.tools),
        model_class="medium",
        metadata={"run_node_id": "root-run-node"},
        agent_snapshot=_canonical_root_snapshot(),
    )
    envelope = CheckpointEnvelope(
        checkpoint_id="checkpoint.vertical.plan.empty-frontier.0001",
        runtime_contract_version=runtime.kernel_manifest.runtime_contract_version,
        runtime_hash=runtime.runtime_hash,
        request_id=plan.request_id,
        plan_id=plan.plan_id,
        task_id=task.task_id,
        seed=0,
        sequence_no=1,
        boundary="before_terminal_result",
        created_at=now_ts(),
        plan_snapshot=(plan).model_dump(),
        task_payload=(task).model_dump(),
        runtime_state_snapshot={
            "request_id": plan.request_id,
            "plan_id": plan.plan_id,
            "execution_state": "running",
            "checkpoint_sequence_no": 1,
            "queued_frames": [(queued_frame).model_dump()],
            "visible_tool_names": sorted(shell.tool_registry.tools),
            "plan_node_status": {node.node_id: "completed" for node in plan.nodes},
            "artifacts": {
                "response_a": "branch-a",
                "response_b": "branch-b",
                verify_node.output_key: {"verifier_score": 1.0, "verified": True},
            },
            "branch_states": {},
            "branch_publications": [],
        },
        shell_state_snapshot=(shell.snapshot_checkpoint_shell_state()).model_dump(),
    )

    select_mode_calls = []
    propose_children_calls = []

    def _unexpected_select_mode(ctx, frame, operations):
        select_mode_calls.append([node.node_id for node in operations])
        raise AssertionError("empty frontier should terminate before topology selection")

    def _unexpected_propose_children(ctx, frame, operations):
        propose_children_calls.append([node.node_id for node in operations])
        raise AssertionError("completed plan nodes must not respawn children")

    monkeypatch.setattr(runtime.topology, "select_mode", _unexpected_select_mode)
    monkeypatch.setattr(runtime.topology, "propose_children", _unexpected_propose_children)

    result = TaskRuntime(runtime, shell, ReplayProvider([])).resume_from_checkpoint(envelope)
    trace_rows = result.trace_rows()

    assert result.hard_invalid is False
    assert result.run_lifecycle_state == "completed"
    assert result.lifecycle_state == "completed"
    assert result.artifact == {"response_a": "branch-a", "response_b": "branch-b"}
    assert result.verifier_score == 1.0
    assert select_mode_calls == []
    assert propose_children_calls == []
    assert [row["event"] for row in trace_rows if row.get("event") in {"terminal_emitted", "run_failed"}] == [
        "terminal_emitted"
    ]
    assert sum(
        1
        for row in trace_rows
        if row.get("event") == "node_reused_from_checkpoint" and row.get("node_id") == merge_node.node_id
    ) == 0
    assert sum(
        1
        for row in trace_rows
        if row.get("event") == "node_reused_from_checkpoint" and row.get("node_id") == verify_node.node_id
    ) == 0

def test_horizontal_mode_executes_explicit_verify_node_after_merge(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_parallel_direct_response_task("horizontal.plan.verify").model_copy(
        update={
            "expected": {"response_a": "left", "response_b": "right"},
            "verifier_type": "json_exact",
            "verification_required": True,
            "allow_best_effort": False,
        }
    )
    _force_horizontal(monkeypatch, runtime, ["w0", "w1"])
    plan = compile_execution_plan_from_task(
        task,
        request_id="horizontal.plan.verify.request",
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    verify_node = next(node for node in plan.nodes if str(node.node_kind) == "verify")

    result = TaskRuntime(
        runtime,
        shell,
        ReplayProvider(
            [
                {"text": "left", "model_name": "replay/small"},
                {"text": "right", "model_name": "replay/small"},
                {"text": "unused", "model_name": "replay/small"},
                {"text": "unused", "model_name": "replay/small"},
            ]
        ),
        budget_overrides={"M_max": 4, "Q_max": 1},
    ).run_task(task, 0, plan=plan)
    trace_rows = result.trace_rows()

    assert result.hard_invalid is False
    assert any(
        row.get("event") == "node_completed" and row.get("node_id") == verify_node.node_id
        for row in trace_rows
    )
    verify_start_index = next(
        index
        for index, row in enumerate(trace_rows)
        if row.get("event") == "node_started" and row.get("node_id") == verify_node.node_id
    )
    merge_index = next(
        index
        for index, row in enumerate(trace_rows)
        if row.get("event") == "merge_completed"
    )
    assert merge_index < verify_start_index

def test_merge_ensemble_prefers_verifier_support_before_merge_priority(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    merged = runtime.topology.merge_ensemble(
        None,
        [
            {
                "branch_id": "w0",
                "merge_priority": 0,
                "verifier_support": 0.25,
                "predicted_solve": 0.95,
                "unresolved_critical": 0,
                "artifact": {"winner": "priority-first"},
            },
            {
                "branch_id": "w1",
                "merge_priority": 2,
                "verifier_support": 1.0,
                "predicted_solve": 0.10,
                "unresolved_critical": 0,
                "artifact": {"winner": "verified"},
            },
        ],
    )
    assert merged == {"winner": "verified"}

def test_horizontal_branch_reservations_respect_remaining_model_calls(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    _force_horizontal(monkeypatch, runtime, ["w0", "w1", "w2"])
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_parallel_direct_response_task("horizontal.budget")
    runner = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([{"text": "same"}, {"text": "same"}]),
        budget_overrides={"M_max": 2, "Q_max": 1},
    )
    result = runner.run_task(task, 0)
    envelope = _checkpoint_for_boundary(shell, result.request_id, "after_branch_completion")

    assert result.hard_invalid is False
    assert result.model_calls == 2
    assert (
        sum(
            int((branch_state).model_dump()["reserved_budget"]["model_calls_max"])
            for branch_state in envelope.runtime_state_snapshot.branch_states.values()
        )
        <= 2
    )

def test_horizontal_branch_admission_skips_fanout_when_latency_floor_is_infeasible(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    _force_horizontal(monkeypatch, runtime, ["w0", "w1", "w2"])
    profile = load_runtime_profile(runtime_dir)
    profile.execution.branch_latency_floor_s = 1.0
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_parallel_direct_response_task("horizontal.latency")
    runner = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([{"text": "left"}, {"text": "right"}]),
        budget_overrides={"L_max": 1.5, "M_max": 8, "Q_max": 1},
        runtime_profile=profile,
    )

    result = runner.run_task(task, 0)
    branch_skip_events = [row for row in result.trace_rows() if row.get("event") == "branch_skipped"]

    assert result.hard_invalid is False
    assert result.artifact == {"response_a": "left", "response_b": "right"}
    assert len(branch_skip_events) == 1
    assert branch_skip_events[0]["reason"] == "joint_budget_infeasible"
    assert not any(row.get("event") == "branch_started" for row in result.trace_rows())
    assert not any(row.get("event") == "merge_started" for row in result.trace_rows())

def test_horizontal_replay_branch_allocation_is_stable_under_completion_order_variation(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_parallel_direct_response_task("horizontal.replay.order")
    plan = compile_execution_plan_from_task(
        task,
        request_id="horizontal.replay.order.request",
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )

    monkeypatch.setattr(runtime.topology, "select_mode", lambda ctx, frame, operations: "horizontal")
    monkeypatch.setattr(
        runtime.topology,
        "select_workers",
        lambda ctx, frame, operations: [
            {
                "worker_id": "w0",
                "instruction": "worker-w0",
                "op_ids": ["respond_a"],
                "predicted_solve": 0.9,
                "tool_scope": ctx.state.visible_tool_names,
                "agent_id": "root",
            },
            {
                "worker_id": "w1",
                "instruction": "worker-w1",
                "op_ids": ["respond_b"],
                "predicted_solve": 0.8,
                "tool_scope": ctx.state.visible_tool_names,
                "agent_id": "root",
            },
        ],
    )
    monkeypatch.setattr(
        runtime.topology,
        "merge_ensemble",
        lambda ctx, worker_outputs: {
            "response_a": next(item["artifact"] for item in worker_outputs if item["branch_id"] == "w0"),
            "response_b": next(item["artifact"] for item in worker_outputs if item["branch_id"] == "w1"),
        },
    )

    def run_with_delays(delays_by_worker):
        provider = DelayingReplayProvider(
            [
                {"text": "branch-a", "model_name": "replay/small"},
                {"text": "branch-b", "model_name": "replay/small"},
            ],
            delays_by_worker=delays_by_worker,
        )
        return TaskRuntime(runtime, shell, provider, budget_overrides={"M_max": 2, "Q_max": 1}).run_task(
            task,
            0,
            plan=plan,
        )

    slower_left = run_with_delays({"w0": 0.05, "w1": 0.0})
    slower_right = run_with_delays({"w0": 0.0, "w1": 0.05})

    assert slower_left.hard_invalid is False
    assert slower_right.hard_invalid is False
    assert slower_left.artifact == {"response_a": "branch-a", "response_b": "branch-b"}
    assert slower_right.artifact == {"response_a": "branch-a", "response_b": "branch-b"}

def test_horizontal_branch_run_reports_sum_of_branch_provider_usage(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    _force_horizontal(monkeypatch, runtime, ["w0", "w1"])
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_parallel_direct_response_task("horizontal.usage")
    runner = TaskRuntime(
        runtime,
        shell,
        ReplayProvider(
            [
                {"text": "branch-a", "model_name": "replay/small", "input_tokens": 3, "output_tokens": 2, "token_estimate": 5, "dollar_cost": 0.01},
                {"text": "branch-b", "model_name": "replay/small", "input_tokens": 4, "output_tokens": 1, "token_estimate": 5, "dollar_cost": 0.02},
                {"text": "branch-c", "model_name": "replay/small", "input_tokens": 5, "output_tokens": 3, "token_estimate": 8, "dollar_cost": 0.03},
                {"text": "branch-d", "model_name": "replay/small", "input_tokens": 6, "output_tokens": 2, "token_estimate": 8, "dollar_cost": 0.04},
            ]
        ),
        budget_overrides={"M_max": 4, "Q_max": 1},
    )

    result = runner.run_task(task, 0)

    assert result.provider_usage["calls"] == 4
    assert result.provider_usage["input_tokens"] == 18
    assert result.provider_usage["output_tokens"] == 8
    assert result.provider_usage["total_tokens"] == 26
    assert result.provider_usage["dollar_cost"] == pytest.approx(0.10)

def test_after_branch_completion_checkpoint_carries_branch_receipts(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    _force_horizontal(monkeypatch, runtime, ["w0", "w1"])
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_parallel_direct_response_task("horizontal.receipts")
    runner = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([{"text": "same"}] * 4),
        budget_overrides={"M_max": 4, "Q_max": 1},
    )
    result = runner.run_task(task, 0)
    envelope = _checkpoint_for_boundary(shell, result.request_id, "after_branch_completion")

    completion_receipts = [
        receipt
        for receipt in envelope.side_effect_ledger["receipts"]
        if receipt.action_kind == "provider_completion"
    ]
    assert {receipt.branch_id for receipt in completion_receipts} == {"w0", "w1"}
    assert {receipt.result_ref.get("text") for receipt in completion_receipts} == {"same"}

def test_resume_from_after_branch_completion_reuses_saved_branch_frontier(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    _force_horizontal(monkeypatch, runtime, ["w0", "w1"])
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_parallel_direct_response_task("horizontal.resume")
    first_runner = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([{"text": "same"}] * 4),
        budget_overrides={"M_max": 4, "Q_max": 1},
    )
    first_run = first_runner.run_task(task, 0)
    envelope = _checkpoint_for_boundary(shell, first_run.request_id, "after_branch_completion")

    resume_runner = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([]),
        budget_overrides={"M_max": 4, "Q_max": 1},
    )
    resumed_run = resume_runner.resume_from_checkpoint(envelope)

    assert resumed_run.hard_invalid is False
    assert resumed_run.model_calls == first_run.model_calls
    assert resumed_run.artifact == first_run.artifact

def test_resume_from_branch_side_effect_checkpoint_reconstructs_in_flight_branch_workers(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    monkeypatch.setattr(runtime.topology, "select_mode", lambda ctx, frame, operations: "horizontal")
    monkeypatch.setattr(
        runtime.topology,
        "merge_ensemble",
        lambda ctx, worker_outputs: {
            "response_a": next(item["artifact"] for item in worker_outputs if item["branch_id"] == "w0"),
            "response_b": next(item["artifact"] for item in worker_outputs if item["branch_id"] == "w1"),
        },
    )
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    envelope, receipt_keys = _branch_resume_checkpoint_envelope(
        runtime,
        shell,
        task_id="horizontal.resume.branch-side-effect",
        left_snapshot_kind="provider_launch",
        right_snapshot_kind="node_completed",
    )
    provider = ReconcilingReplayProvider(
        [],
        reconciled={
            receipt_keys["w0"]: {
                "text": "w0-value",
                "model_name": "replay/small",
                "input_tokens": 3,
                "output_tokens": 2,
                "token_estimate": 5,
            }
        },
    )
    monkeypatch.setattr("agintor.runtime.kernel.branches.providers.clone_provider", lambda provider, provider_profile=None: provider)

    resumed_run = TaskRuntime(runtime, shell, provider).resume_from_checkpoint(envelope)

    assert resumed_run.hard_invalid is False
    assert resumed_run.artifact == {"response_a": "w0-value", "response_b": "w1-value"}
    assert provider.generate_calls == 0
    assert provider.reconcile_calls == [receipt_keys["w0"]]

def test_resume_from_branch_node_checkpoint_reconstructs_without_refanout(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    monkeypatch.setattr(runtime.topology, "select_mode", lambda ctx, frame, operations: "horizontal")
    monkeypatch.setattr(
        runtime.topology,
        "merge_ensemble",
        lambda ctx, worker_outputs: {
            "response_a": next(item["artifact"] for item in worker_outputs if item["branch_id"] == "w0"),
            "response_b": next(item["artifact"] for item in worker_outputs if item["branch_id"] == "w1"),
        },
    )
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    envelope, _ = _branch_resume_checkpoint_envelope(
        runtime,
        shell,
        task_id="horizontal.resume.branch-node",
        left_snapshot_kind="node_completed",
        right_snapshot_kind="pending",
    )
    provider = ReplayProvider([{"text": "w1-value", "model_name": "replay/small"}])
    monkeypatch.setattr("agintor.runtime.kernel.branches.providers.clone_provider", lambda provider, provider_profile=None: provider)

    resumed_run = TaskRuntime(runtime, shell, provider).resume_from_checkpoint(envelope)

    assert resumed_run.hard_invalid is False
    assert resumed_run.artifact == {"response_a": "w0-value", "response_b": "w1-value"}
    assert resumed_run.provider_usage["calls"] == 1

def test_failed_branch_completion_checkpoint_is_not_advertised_as_resumable(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    _force_horizontal(monkeypatch, runtime, ["w0", "w1"])
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_parallel_direct_response_task("horizontal.failed.checkpoint")
    runner = TaskRuntime(
        runtime,
        shell,
        LocalDeterministicProvider(),
        budget_overrides={"M_max": 4, "Q_max": 1},
    )
    sibling_started = Event()

    def fake_run_branch_plan(self, parent_context, task, plan, branch_plan, cancellation_event, persist_lock, *args, **kwargs):
        if branch_plan.branch_id == "w1":
            sibling_started.set()
            if cancellation_event.wait(1.0):
                return _branch_result(
                    branch_plan,
                    status="cancelled",
                    cancellation_reason=str(getattr(cancellation_event, "reason", "fatal_branch_fault")),
                )
            return _branch_result(
                branch_plan,
                status="completed",
                artifact={"response_a": "unexpected", "response_b": "unexpected"},
            )
        assert sibling_started.wait(1.0)
        return _branch_result(branch_plan, status="failed", failure_kind="protocol_failure")

    monkeypatch.setattr(TaskRuntime, "_run_branch_plan", fake_run_branch_plan)

    result = runner.run_task(task, 0)
    after_branch_completion = _checkpoint_for_boundary(shell, result.request_id, "after_branch_completion")
    advertised_checkpoint = shell.load_checkpoint_envelope(checkpoint_ref=result.checkpoint_ref)

    assert result.hard_invalid is True
    assert after_branch_completion.resume_eligible is False
    assert after_branch_completion.resume_ineligibility_reason == "failed_branch_group"
    assert advertised_checkpoint.resume_eligible is True
    assert advertised_checkpoint.checkpoint_id != after_branch_completion.checkpoint_id

def test_tool_executor_cancel_async_handle_terminates_process_and_marks_handle(tmp_path):
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    handle = AsyncHandle(
        handle_id="handle.cancel.1",
        tool_name="dummy-tool",
        sandbox_hash="sandbox-hash",
        working_directory=str(tmp_path),
        launch_time=now_ts(),
        timeout=60,
        stdout_path=str(tmp_path / "stdout.txt"),
        stderr_path=str(tmp_path / "stderr.txt"),
        state="running",
        artifact_refs=[str(tmp_path / "result.json")],
        process_pid=process.pid,
    )
    shell.open_handles.add(handle)
    shell.tool_executor._async_processes[handle.handle_id] = _AsyncProcessRecord(
        process=process,
        state={"stdout": "", "stderr": "", "output": None, "artifact_refs": []},
    )

    cancelled = shell.tool_executor.cancel_async_handle(handle.handle_id, shell.open_handles)

    assert cancelled["state"] == "cancelled"
    assert shell.open_handles.get(handle.handle_id).state == "cancelled"
    assert handle.handle_id not in shell.tool_executor._async_processes
    assert process.poll() is not None

def test_cancelled_branch_cleanup_emits_only_cleanup_and_reconciliation_records(tmp_path, monkeypatch):
    runner, branch_plan, branch_context = _make_branch_cleanup_context(tmp_path)
    branch_context.state.branch_publications.append(
        {
            "publication_id": "publication.old",
            "publication_kind": "candidate_artifact",
            "logical_key": "w0.old",
            "sequence_no": 0,
            "accepted": True,
            "branch_id": "w0",
            "payload": {"artifact": {"stale": True}},
        }
    )
    handle = AsyncHandle(
        handle_id="handle.branch.cancel",
        tool_name="dummy-tool",
        sandbox_hash="sandbox-hash",
        working_directory=str(tmp_path),
        launch_time=now_ts(),
        timeout=60,
        state="running",
        artifact_refs=[],
    )
    branch_context.shell.open_handles.add(handle)
    branch_context.state.open_handle_ids.append(handle.handle_id)
    branch_context.state.side_effect_receipts.append(
        (SideEffectReceipt(
                side_effect_id="tool-launch.branch",
                action_fingerprint="tool-launch.branch",
                idempotency_key="tool-launch.branch",
                action_kind="tool_launch",
                request_id=branch_context.request_id,
                plan_id=branch_context.plan.plan_id,
                frame_id=branch_context.active_frame.frame_id,
                node_id="respond_a",
                branch_id="w0",
                trace_context=branch_context.trace_context,
                request_digest="tool-launch.branch",
                backend="local",
                status="launched",
                result_ref={"tool_name": handle.tool_name, "launch_mode": "async", "handle_id": handle.handle_id},
                created_at=now_ts(),
            )).model_dump()
    )

    def fake_cancel(handle_id, handle_table):
        handle_table.update_state(handle_id, "cancelled")
        return {"handle_id": handle_id, "state": "cancelled"}

    monkeypatch.setattr(branch_context.shell.tool_executor, "cancel_async_handle", fake_cancel)

    result = runner._cancelled_branch_result(
        branch_plan,
        branch_context,
        unresolved_critical=1,
        reason="parent_stop_policy",
        details={},
    )

    assert branch_context.shell.open_handles.get(handle.handle_id).state == "cancelled"
    assert all(
        publication.publication_kind in {"cleanup_record", "reconciliation_record"}
        for publication in result.branch_state.publications
    )
    assert {receipt.status for receipt in result.side_effect_receipts} == {"abandoned"}

def test_cancelled_branch_cleanup_fails_closed_when_handle_cannot_be_cleaned_up(tmp_path, monkeypatch):
    runner, branch_plan, branch_context = _make_branch_cleanup_context(tmp_path)
    handle = AsyncHandle(
        handle_id="handle.branch.failure",
        tool_name="dummy-tool",
        sandbox_hash="sandbox-hash",
        working_directory=str(tmp_path),
        launch_time=now_ts(),
        timeout=60,
        state="running",
        artifact_refs=[],
    )
    branch_context.shell.open_handles.add(handle)
    branch_context.state.open_handle_ids.append(handle.handle_id)
    branch_context.state.side_effect_receipts.append(
        (SideEffectReceipt(
                side_effect_id="tool-launch.failure",
                action_fingerprint="tool-launch.failure",
                idempotency_key="tool-launch.failure",
                action_kind="tool_launch",
                request_id=branch_context.request_id,
                plan_id=branch_context.plan.plan_id,
                frame_id=branch_context.active_frame.frame_id,
                node_id="respond_a",
                branch_id="w0",
                trace_context=branch_context.trace_context,
                request_digest="tool-launch.failure",
                backend="local",
                status="launched",
                result_ref={"tool_name": handle.tool_name, "launch_mode": "async", "handle_id": handle.handle_id},
                created_at=now_ts(),
            )).model_dump()
    )

    monkeypatch.setattr(
        branch_context.shell.tool_executor,
        "cancel_async_handle",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
    )

    with pytest.raises(Exception, match="failed to clean up handle"):
        runner._cancelled_branch_result(
            branch_plan,
            branch_context,
            unresolved_critical=1,
            reason="parent_stop_policy",
            details={},
        )

def test_cancelled_branch_fails_closed_on_unresolved_sync_tool_launch(tmp_path):
    runner, branch_plan, branch_context = _make_branch_cleanup_context(tmp_path)
    branch_context.state.side_effect_receipts.append(
        (SideEffectReceipt(
                side_effect_id="tool-launch.sync",
                action_fingerprint="tool-launch.sync",
                idempotency_key="tool-launch.sync",
                action_kind="tool_launch",
                request_id=branch_context.request_id,
                plan_id=branch_context.plan.plan_id,
                frame_id=branch_context.active_frame.frame_id,
                node_id="respond_a",
                branch_id="w0",
                trace_context=branch_context.trace_context,
                request_digest="tool-launch.sync",
                backend="local",
                status="launched",
                result_ref={"tool_name": "math/basic/sum_numbers", "launch_mode": "sync"},
                created_at=now_ts(),
            )).model_dump()
    )

    with pytest.raises(Exception, match="unresolved sync tool launch"):
        runner._cancelled_branch_result(
            branch_plan,
            branch_context,
            unresolved_critical=1,
            reason="parent_stop_policy",
            details={},
        )

def test_emit_branch_publication_starts_unaccepted(tmp_path):
    runner, branch_plan, branch_context = _make_branch_cleanup_context(tmp_path)

    publication = runner._emit_branch_publication(
        branch_context,
        publication_kind="candidate_artifact",
        logical_key="w0.artifact",
        payload={"artifact": {"ok": True}},
    )

    assert publication is not None
    assert publication.accepted is False
    assert branch_context.state.branch_publications[0]["accepted"] is False

def test_horizontal_branch_overspend_becomes_failed_branch_result(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    _force_horizontal(monkeypatch, runtime, ["w0"])
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_parallel_direct_response_task("horizontal.overspend")
    runner = TaskRuntime(runtime, shell, LocalDeterministicProvider(), budget_overrides={"M_max": 4, "Q_max": 1})
    original_execute_isolated_frame = TaskRuntime._execute_isolated_frame

    def fake_execute_isolated_frame(self, branch_context, frame, operations, isolate_runtime_state=False):
        if frame.worker_id:
            branch_context.budget.calls = branch_context.budget.M_max + 1
            checkpoint = Checkpoint(
                summary=runner.runtime.topology.make_checkpoint(branch_context, frame, {}, [], []).summary,
                artifact_refs=[],
                open_handles=[],
                unresolved_goals=["response_a"],
                budget_state=branch_context.budget.normalized(),
                verifier_state={},
                resume_constraints={},
            )
            return {"response_a": "too-much"}, 0, checkpoint
        return original_execute_isolated_frame(self, branch_context, frame, operations, isolate_runtime_state=isolate_runtime_state)

    monkeypatch.setattr(TaskRuntime, "_execute_isolated_frame", fake_execute_isolated_frame)

    result = runner.run_task(task, 0)
    envelope = _checkpoint_for_boundary(shell, result.request_id, "after_branch_completion")

    assert result.hard_invalid is True
    assert envelope.runtime_state_snapshot.branch_states["w0"].status == "failed"
    assert envelope.runtime_state_snapshot.branch_states["w0"].failure_kind == "reservation_exceeded"

def test_stop_policy_cancellation_with_unresolved_work_finishes_cancelled(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_direct_response_task("stop.cancelled")
    runner = TaskRuntime(runtime, shell, ReplayProvider([]))

    monkeypatch.setattr(runtime.control, "stop_policy", lambda *args, **kwargs: True)
    monkeypatch.setattr(runner, "_run_root_frame", lambda *args, **kwargs: (None, 0, 0.0, False))

    result = runner.run_task(task, 0)
    terminal_events = [
        row
        for row in result.trace_rows()
        if row.get("event") in {"run_cancelled", "terminal_emitted", "run_failed"}
    ]

    assert result.run_lifecycle_state == "cancelled"
    assert result.lifecycle_state == "cancelled"
    assert [row["event"] for row in terminal_events] == ["run_cancelled"]
    assert terminal_events[0]["reason"] == "parent_stop_policy"

def test_stop_policy_completion_path_with_terminal_artifact_stays_completed(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_direct_response_task("stop.completed")
    runner = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([{"text": "hello"}]),
    )

    monkeypatch.setattr(runtime.control, "stop_policy", lambda *args, **kwargs: True)

    result = runner.run_task(task, 0)
    terminal_events = [
        row
        for row in result.trace_rows()
        if row.get("event") in {"run_cancelled", "terminal_emitted", "run_failed"}
    ]

    assert result.run_lifecycle_state == "completed"
    assert result.lifecycle_state == "completed"
    assert [row["event"] for row in terminal_events] == ["terminal_emitted"]

def test_active_runnable_frontier_prefers_earlier_singleton(tmp_path):
    runner, plan, context, _ = _build_mixed_frontier_context(tmp_path)

    frontier = runner._active_runnable_frontier(context, plan)

    assert [node.node_id for node in frontier] == ["a"]

def test_active_runnable_frontier_advances_to_group_after_singleton_completion(tmp_path):
    runner, plan, context, _ = _build_mixed_frontier_context(tmp_path)
    context.state.plan_node_status["a"] = "completed"

    frontier = runner._active_runnable_frontier(context, plan)

    assert [node.node_id for node in frontier] == ["b", "c"]

def test_active_runnable_frontier_honors_explicit_branch_group_override(tmp_path):
    runner, plan, context, grouped_frontier_id = _build_mixed_frontier_context(tmp_path)

    frontier = runner._active_runnable_frontier(context, plan, branch_group_id=grouped_frontier_id)

    assert [node.node_id for node in frontier] == ["b", "c"]

def test_single_output_number_exact_verifies_raw_artifact(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_exact_direct_response_task(
        "verify.number-exact",
        expected=42,
        verifier_type="number_exact",
    )
    plan = compile_execution_plan_from_task(
        task,
        request_id="verify.number-exact.request",
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )

    result = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([{"text": "42", "model_name": "replay/small"}]),
    ).run_task(task, 0, plan=plan)

    assert result.verifier_score == 1.0
    assert result.artifact == 42

def test_single_output_string_exact_verifies_raw_artifact(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_exact_direct_response_task(
        "verify.string-exact",
        expected="hello",
        verifier_type="string_exact",
    )
    plan = compile_execution_plan_from_task(
        task,
        request_id="verify.string-exact.request",
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )

    result = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([{"text": "hello", "model_name": "replay/small"}]),
    ).run_task(task, 0, plan=plan)

    assert result.verifier_score == 1.0
    assert result.artifact == "hello"

def test_multi_output_verification_keeps_mapping_artifact(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_parallel_direct_response_task("verify.multi-output").model_copy(
        update={
            "expected": {"response_a": "left", "response_b": "right"},
            "verifier_type": "json_exact",
            "verification_required": True,
            "allow_best_effort": False,
        }
    )
    plan = compile_execution_plan_from_task(
        task,
        request_id="verify.multi-output.request",
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    monkeypatch.setattr(runtime.topology, "select_mode", lambda ctx, frame, operations: "single")

    result = TaskRuntime(
        runtime,
        shell,
        ReplayProvider(
            [
                {"text": "left", "model_name": "replay/small"},
                {"text": "right", "model_name": "replay/small"},
            ]
        ),
    ).run_task(task, 0, plan=plan)

    assert result.verifier_score == 1.0
    assert result.artifact == {"response_a": "left", "response_b": "right"}
    assert isinstance(result.artifact, dict)

def test_failed_branch_result_cancels_running_sibling(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    _force_horizontal(monkeypatch, runtime, ["w0", "w1"])
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_parallel_direct_response_task("horizontal.failed-running-sibling")
    runner = TaskRuntime(
        runtime,
        shell,
        LocalDeterministicProvider(),
        budget_overrides={"M_max": 4, "Q_max": 1},
    )
    sibling_started = Event()

    def fake_run_branch_plan(self, parent_context, task, plan, branch_plan, cancellation_event, persist_lock):
        if branch_plan.branch_id == "w1":
            sibling_started.set()
            if cancellation_event.wait(1.0):
                return _branch_result(
                    branch_plan,
                    status="cancelled",
                    cancellation_reason=str(getattr(cancellation_event, "reason", "fatal_branch_fault")),
                )
            return _branch_result(
                branch_plan,
                status="completed",
                artifact={"response_a": "unexpected", "response_b": "unexpected"},
            )
        assert sibling_started.wait(1.0)
        return _branch_result(branch_plan, status="failed", failure_kind="protocol_failure")

    monkeypatch.setattr(TaskRuntime, "_run_branch_plan", fake_run_branch_plan)

    result = runner.run_task(task, 0)
    envelope = _checkpoint_for_boundary(shell, result.request_id, "after_branch_completion")

    assert result.hard_invalid is True
    assert envelope.runtime_state_snapshot.branch_states["w0"].status == "failed"
    assert envelope.runtime_state_snapshot.branch_states["w1"].status == "cancelled"

@pytest.mark.parametrize(
    ("failure_kind", "expected_reason"),
    [
        ("verification_failure", "verification_failure"),
        ("reservation_exceeded", "budget_exhaustion"),
    ],
)
def test_failed_branch_kind_maps_to_sibling_cancellation_reason(tmp_path, monkeypatch, failure_kind, expected_reason):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    _force_horizontal(monkeypatch, runtime, ["w0", "w1"])
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_parallel_direct_response_task(f"horizontal.reason.{failure_kind}")
    runner = TaskRuntime(
        runtime,
        shell,
        LocalDeterministicProvider(),
        budget_overrides={"M_max": 4, "Q_max": 1},
    )
    sibling_started = Event()

    def fake_run_branch_plan(self, parent_context, task, plan, branch_plan, cancellation_event, persist_lock):
        if branch_plan.branch_id == "w1":
            sibling_started.set()
            if cancellation_event.wait(1.0):
                return _branch_result(
                    branch_plan,
                    status="cancelled",
                    cancellation_reason=str(getattr(cancellation_event, "reason", "parent_stop_policy")),
                )
            return _branch_result(
                branch_plan,
                status="completed",
                artifact={"response_a": "unexpected", "response_b": "unexpected"},
            )
        assert sibling_started.wait(1.0)
        return _branch_result(branch_plan, status="failed", failure_kind=failure_kind)

    monkeypatch.setattr(TaskRuntime, "_run_branch_plan", fake_run_branch_plan)

    result = runner.run_task(task, 0)
    envelope = _checkpoint_for_boundary(shell, result.request_id, "after_branch_completion")

    assert result.hard_invalid is True
    assert envelope.runtime_state_snapshot.branch_states["w1"].status == "cancelled"
    assert envelope.runtime_state_snapshot.branch_states["w1"].cancellation_record.reason == expected_reason

def test_resume_recovery_error_still_cancels_siblings(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    _force_horizontal(monkeypatch, runtime, ["w0", "w1"])
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_parallel_direct_response_task("horizontal.resume-recovery")
    runner = TaskRuntime(
        runtime,
        shell,
        LocalDeterministicProvider(),
        budget_overrides={"M_max": 4, "Q_max": 1},
    )
    sibling_started = Event()

    def fake_run_branch_plan(self, parent_context, task, plan, branch_plan, cancellation_event, persist_lock):
        if branch_plan.branch_id == "w1":
            sibling_started.set()
            if cancellation_event.wait(1.0):
                return _branch_result(
                    branch_plan,
                    status="cancelled",
                    cancellation_reason=str(getattr(cancellation_event, "reason", "fatal_branch_fault")),
                )
            return _branch_result(
                branch_plan,
                status="completed",
                artifact={"response_a": "unexpected", "response_b": "unexpected"},
            )
        assert sibling_started.wait(1.0)
        raise ResumeRecoveryError("receipt_reconciliation_failed", "resume reconciliation failed")

    monkeypatch.setattr(TaskRuntime, "_run_branch_plan", fake_run_branch_plan)

    result = runner.run_task(task, 0)
    envelope = _checkpoint_for_boundary(shell, result.request_id, "after_branch_completion")

    assert result.hard_invalid is True
    assert result.failure_kind == "receipt_reconciliation_failed"
    assert envelope.runtime_state_snapshot.branch_states["w1"].status == "cancelled"
