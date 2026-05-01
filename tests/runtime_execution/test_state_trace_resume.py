from __future__ import annotations

from .helpers import (
    json,
    Path,
    SimpleNamespace,
    ArtifactMode,
    state_store,
    init_runtime,
    LocalDeterministicProvider,
    ReplayProvider,
    PolicyContext,
    RuntimeBudget,
    RuntimeState,
    batch_evaluation_unit_key,
    compile_execution_plan_from_task,
    reduce_grouped_run_results,
    solve_request_from_resume_checkpoint,
    RunStore,
    load_runtime,
    TaskRuntime,
    BenchmarkTask,
    CheckpointEnvelope,
    ExecutionUnitRequestEnvelope,
    OpenAITraceContext,
    ModelResponse,
    QueuedFrameSnapshot,
    RunResult,
    RuntimeTaskInvocation,
    FixedShell,
    now_ts,
    RUNTIME_CONTRACT_VERSION,
    ReconcilingReplayProvider,
    _make_direct_response_task,
    _make_builtin_sum_task,
    _canonical_root_snapshot,
    _checkpoint_for_boundary,
    _pending_provider_launch_envelope,
)


def test_runtime_bundle_includes_run_store_module(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")

    bundled_run_store = runtime_dir / "runtime_sdk" / "agintor_runtime" / "storage" / "run_store.py"
    bundled_state_store = runtime_dir / "runtime_sdk" / "agintor_runtime" / "storage" / "state_store" / "store.py"

    assert bundled_run_store.exists()
    assert bundled_state_store.exists()

def test_run_store_canonicalizes_relative_workspace_run_roots_and_checkpoint_refs(tmp_path, monkeypatch):
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    store = RunStore("relative-workspace")
    manifest = store.create_run(
        request_id="req.relative",
        evaluation_unit_id="req.relative",
        request_mode="user_request",
        runtime_backend="local",
    )
    envelope = CheckpointEnvelope(
        checkpoint_id="checkpoint.req.relative.0001",
        runtime_contract_version=RUNTIME_CONTRACT_VERSION,
        runtime_hash="runtime-hash",
        run_id=manifest.run_id,
        run_root=manifest.run_root,
        request_id="req.relative",
        plan_id="plan.relative",
        task_id="task.relative",
        seed=0,
    )

    checkpoint_ref = store.write_checkpoint(envelope)
    reloaded_manifest = store.load_run_manifest(manifest.run_id)

    assert store.workspace == (workdir / "relative-workspace").resolve()
    assert Path(reloaded_manifest.run_root).is_absolute()
    assert reloaded_manifest.run_root == str((store.workspace / "runs" / manifest.run_id).resolve())
    assert checkpoint_ref.ref == str(Path(checkpoint_ref.ref).resolve())
    assert store.latest_checkpoint_ref(manifest.run_id) == checkpoint_ref.ref
    indexed_latest = state_store.open_state_store(reloaded_manifest.run_root).latest_usable_checkpoint(
        run_id=manifest.run_id
    )
    assert indexed_latest is not None
    assert indexed_latest["checkpoint_id"] == envelope.checkpoint_id

def test_fixed_shell_checkpoint_lookup_can_resume_from_external_store_with_container_refs(tmp_path):
    store_shell = FixedShell(tmp_path / "store-workspace", artifact_mode=ArtifactMode.ALWAYS)
    envelope_one = CheckpointEnvelope(
        checkpoint_id="checkpoint.req.0001",
        runtime_contract_version=RUNTIME_CONTRACT_VERSION,
        runtime_hash="runtime-hash",
        request_id="req",
        plan_id="plan",
        task_id="task",
        seed=1,
        sequence_no=1,
        boundary="after_provider_completion",
        created_at=1.0,
    )
    envelope_two = envelope_one.model_copy(
        update={
            "checkpoint_id": "checkpoint.req.0002",
            "sequence_no": 2,
            "boundary": "after_branch_completion",
            "created_at": 2.0,
        }
    )

    ref_one = store_shell.save_checkpoint_envelope(envelope_one)
    ref_two = store_shell.save_checkpoint_envelope(envelope_two)

    request_dir = tmp_path / "store-workspace" / "checkpoints" / "req"
    index_path = request_dir / "index.json"
    latest_path = request_dir / "LATEST.json"
    index_rows = json.loads(index_path.read_text(encoding="utf-8"))
    for row in index_rows:
        row["checkpoint_ref"] = f"/mnt/workspace/seed_1/checkpoints/req/{row['checkpoint_id']}.json"
    index_path.write_text(json.dumps(index_rows, indent=2, sort_keys=True), encoding="utf-8")
    latest_payload = json.loads(latest_path.read_text(encoding="utf-8"))
    latest_payload["checkpoint_ref"] = f"/mnt/workspace/seed_1/checkpoints/req/{latest_payload['checkpoint_id']}.json"
    latest_path.write_text(json.dumps(latest_payload, indent=2, sort_keys=True), encoding="utf-8")

    resume_shell = FixedShell(tmp_path / "resume-workspace", artifact_mode=ArtifactMode.ALWAYS)
    resume_shell.configure_resume_checkpoint_store(str((tmp_path / "store-workspace" / "checkpoints").resolve()))

    assert ref_one.sequence_no == 1
    assert ref_two.sequence_no == 2
    assert resume_shell.latest_checkpoint_ref("req") == ref_two.ref
    restored = resume_shell.load_checkpoint_envelope(request_id="req")
    assert restored.checkpoint_id == "checkpoint.req.0002"

def test_fixed_shell_no_longer_exposes_save_checkpoints():
    assert not hasattr(FixedShell, "save_checkpoints")

def test_batch_evaluation_unit_key_scopes_transfer_runs_by_episode_and_seed():
    invocations = [
        RuntimeTaskInvocation(
            request_id="benchmark.episode.step1.seed_1",
            seed=1,
            task=BenchmarkTask(
                task_id="episode.step1",
                family="top",
                prompt="Step one",
                task_type="structured_ops",
                operations=[],
                expected={},
                transfer_scored=True,
                episode_id="episode-alpha",
                episode_order=0,
            ),
        ),
        RuntimeTaskInvocation(
            request_id="benchmark.episode.step2.seed_1",
            seed=1,
            task=BenchmarkTask(
                task_id="episode.step2",
                family="top",
                prompt="Step two",
                task_type="structured_ops",
                operations=[],
                expected={},
                transfer_scored=True,
                episode_id="episode-alpha",
                episode_order=1,
            ),
        ),
        RuntimeTaskInvocation(
            request_id="benchmark.episode.step1.seed_2",
            seed=2,
            task=BenchmarkTask(
                task_id="episode.step1",
                family="top",
                prompt="Step one",
                task_type="structured_ops",
                operations=[],
                expected={},
                transfer_scored=True,
                episode_id="episode-alpha",
                episode_order=0,
            ),
        ),
    ]

    assert batch_evaluation_unit_key(invocations[0]) == batch_evaluation_unit_key(invocations[1])
    assert batch_evaluation_unit_key(invocations[0]) != batch_evaluation_unit_key(invocations[2])

def test_batch_evaluation_unit_key_keeps_benchmark_duplicates_separate_even_if_transport_is_shared():
    task = _make_direct_response_task("duplicate.transport")
    first = RuntimeTaskInvocation(
        request_id="benchmark.duplicate.transport.seed_1",
        evaluation_unit_id="shared.transport.unit",
        seed=1,
        task=task,
    )
    second = RuntimeTaskInvocation(
        request_id="benchmark.duplicate.transport.seed_1.dup_01",
        evaluation_unit_id="shared.transport.unit",
        seed=1,
        task=task,
    )

    assert first.episode_kind is None
    assert second.episode_kind is None
    assert batch_evaluation_unit_key(first) == first.request_id
    assert batch_evaluation_unit_key(second) == second.request_id
    assert batch_evaluation_unit_key(first) != batch_evaluation_unit_key(second)

def test_compile_transfer_plan_fills_missing_episode_step_from_task_order():
    task = BenchmarkTask(
        task_id="episode.partial-trace.step3",
        family="top",
        prompt="Step three",
        task_type="structured_ops",
        operations=[],
        expected={},
        transfer_scored=True,
        episode_id="episode-partial-trace",
        episode_order=3,
    )

    plan = compile_execution_plan_from_task(
        task,
        request_id="benchmark.episode.partial-trace.step3.seed_5",
        seed=5,
        runtime_hash="runtime-hash",
        runtime_dir="runtime-dir",
        trace_context=OpenAITraceContext(session_id="session.partial"),
    )

    assert plan.trace_context.episode_kind == "transfer_episode"
    assert plan.trace_context.episode_step_index == 3

def test_state_store_indexes_execution_unit_members_from_request_envelope(tmp_path):
    store = RunStore(tmp_path / "runs")
    manifest = store.create_run(
        request_id="episode.ledger.seed_9",
        evaluation_unit_id="episode.ledger.seed_9",
        request_mode="batch",
        runtime_backend="local",
        runtime_contract_version=RUNTIME_CONTRACT_VERSION,
    )
    first_task = BenchmarkTask(
        task_id="episode.ledger.step1",
        family="top",
        prompt="Step one",
        task_type="structured_ops",
        operations=[],
        expected={},
        transfer_scored=True,
        episode_id="episode-ledger",
        episode_order=0,
    )
    second_task = BenchmarkTask(
        task_id="episode.ledger.step2",
        family="top",
        prompt="Step two",
        task_type="structured_ops",
        operations=[],
        expected={},
        transfer_scored=True,
        episode_id="episode-ledger",
        episode_order=1,
    )
    invocations = [
        RuntimeTaskInvocation(
            request_id="benchmark.episode.ledger.step1.seed_9",
            evaluation_unit_id=manifest.evaluation_unit_id,
            episode_kind="transfer_episode",
            episode_step_index=0,
            runtime_backend="local",
            seed=9,
            task=first_task,
            trace_context=OpenAITraceContext(
                request_id="benchmark.episode.ledger.step1.seed_9",
                evaluation_unit_id=manifest.evaluation_unit_id,
                episode_kind="transfer_episode",
                episode_step_index=0,
            ),
        ),
        RuntimeTaskInvocation(
            request_id="benchmark.episode.ledger.step2.seed_9",
            evaluation_unit_id=manifest.evaluation_unit_id,
            episode_kind="transfer_episode",
            episode_step_index=1,
            runtime_backend="local",
            seed=9,
            task=second_task,
            trace_context=OpenAITraceContext(
                request_id="benchmark.episode.ledger.step2.seed_9",
                evaluation_unit_id=manifest.evaluation_unit_id,
                episode_kind="transfer_episode",
                episode_step_index=1,
            ),
        ),
    ]
    envelope = ExecutionUnitRequestEnvelope(
        request_kind="runtime_task_invocation_group",
        request_mode="batch",
        request_id=manifest.request_id,
        evaluation_unit_id=manifest.evaluation_unit_id,
        payload=(invocations[0]).model_dump(),
        member_invocations=invocations,
    )

    store.write_request_bundle(manifest, request_envelope=(envelope).model_dump())

    with state_store.open_state_store(manifest.run_root)._connection() as conn:
        episodes = conn.execute(
            "SELECT request_id, episode_kind, episode_step_index, task_id FROM episodes ORDER BY episode_step_index"
        ).fetchall()
        tasks = conn.execute("SELECT task_id, request_id, evaluation_unit_id FROM tasks ORDER BY task_id").fetchall()

    assert [dict(row) for row in episodes] == [
        {
            "request_id": "benchmark.episode.ledger.step1.seed_9",
            "episode_kind": "transfer_episode",
            "episode_step_index": 0,
            "task_id": "episode.ledger.step1",
        },
        {
            "request_id": "benchmark.episode.ledger.step2.seed_9",
            "episode_kind": "transfer_episode",
            "episode_step_index": 1,
            "task_id": "episode.ledger.step2",
        },
    ]
    assert {row["task_id"] for row in tasks} == {"episode.ledger.step1", "episode.ledger.step2"}
    assert {row["evaluation_unit_id"] for row in tasks} == {manifest.evaluation_unit_id}

def test_trace_cursor_links_persisted_model_call_ids(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_direct_response_task("trace.call.cursor")
    plan = compile_execution_plan_from_task(
        task,
        request_id="benchmark.trace.call.cursor.seed_0",
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    context = PolicyContext(
        runtime_dir=runtime.runtime_dir,
        shell=shell,
        task=task,
        request_id=plan.request_id,
        plan=plan,
        trace_context=plan.trace_context,
        provider=LocalDeterministicProvider(),
        seed=0,
        state=RuntimeState(request_id=plan.request_id, plan_id=plan.plan_id),
        budget=RuntimeBudget(),
        trace=[],
        objective=plan.objective,
    )
    response = ModelResponse(
        text="ok",
        model_name="hosted/small",
        trace_call_id="20260424T000000Z__pid1__call0001__user_request__create__model",
    )

    context.consume_model_response(response, purpose="user_request")
    cursor = TaskRuntime(
        runtime,
        shell,
        LocalDeterministicProvider(),
    )._build_trace_cursor_snapshot(context, task, 0)

    assert cursor.linked_call_ids == ["20260424T000000Z__pid1__call0001__user_request__create__model"]

def test_default_trace_session_is_persisted_to_checkpoint_cursor(tmp_path, monkeypatch):
    monkeypatch.setenv("AGINTOR_OPENAI_TRACE_SESSION_ID", "session.default-checkpoint")
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_direct_response_task("trace.default-session.cursor")

    result = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([{"text": "hello with default trace session", "model_name": "replay/small"}]),
    ).run_task(task, 0)
    envelope = shell.load_checkpoint_envelope(checkpoint_ref=result.checkpoint_ref)

    assert result.trace_context.session_id == "session.default-checkpoint"
    assert envelope.plan_snapshot["trace_context"]["session_id"] == "session.default-checkpoint"
    assert envelope.trace_cursor.last_session_id == "session.default-checkpoint"
    assert envelope.trace_cursor.last_runtime_task_key == f"{task.task_id}|seed_0|{runtime.runtime_hash}|{result.request_id}"
    assert envelope.trace_cursor.materialization_state_ref == (
        "openai_api_traces/sessions/session.default-checkpoint/materialization_state.json"
    )

def test_trace_cursor_materialization_ref_uses_sanitized_session_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AGINTOR_OPENAI_TRACE_SESSION_ID", "session default/checkpoint")
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_direct_response_task("trace.sanitized-session.cursor")

    result = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([{"text": "hello with unsafe trace session", "model_name": "replay/small"}]),
    ).run_task(task, 0)
    envelope = shell.load_checkpoint_envelope(checkpoint_ref=result.checkpoint_ref)

    assert result.trace_context.session_id == "session default/checkpoint"
    assert envelope.trace_cursor.last_session_id == "session default/checkpoint"
    assert envelope.trace_cursor.materialization_state_ref == (
        "openai_api_traces/sessions/session_default_checkpoint/materialization_state.json"
    )

def test_resume_from_checkpoint_restores_budget_state_and_reuses_completed_receipts(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    first_provider = ReplayProvider(
        [
            {
                "text": "hello from ws2",
                "model_name": "replay/small",
                "input_tokens": 5,
                "output_tokens": 4,
                "token_estimate": 9,
                "latency_s": 0.25,
                "dollar_cost": 0.01,
            }
        ]
    )
    task = _make_direct_response_task("resume.direct-response")
    first_runner = TaskRuntime(runtime, shell, first_provider)
    first_run = first_runner.run_task(task, 0)

    assert first_run.model_calls == 1
    assert first_run.checkpoint_ref
    envelope = shell.load_checkpoint_envelope(checkpoint_ref=first_run.checkpoint_ref)
    assert envelope.runtime_state_snapshot.budget_totals.calls == 1

    resume_provider = ReplayProvider([])
    resume_runner = TaskRuntime(runtime, shell, resume_provider)
    resumed_run = resume_runner.resume_from_checkpoint(envelope)

    assert resumed_run.hard_invalid is False
    assert resumed_run.model_calls == 1
    assert resumed_run.artifact == first_run.artifact

def test_provider_receipt_replay_ignores_resume_trace_provenance(tmp_path):
    task = _make_direct_response_task("receipt.trace-provenance")
    plan = compile_execution_plan_from_task(
        task,
        request_id="request.original",
        seed=0,
        runtime_hash="runtime.hash",
        runtime_dir="/mnt/runtime",
    )
    base_trace_context = plan.trace_context.model_copy(update={"op_id": "respond", "run_node_id": "node.original"})

    def make_context(
        trace_context: OpenAITraceContext,
        provider: ReplayProvider,
        receipts: list[dict[str, object]] | None = None,
    ) -> PolicyContext:
        return PolicyContext(
            runtime_dir=Path(trace_context.runtime_dir or tmp_path),
            shell=SimpleNamespace(),
            task=task,
            request_id=trace_context.request_id or plan.request_id,
            plan=plan,
            trace_context=trace_context,
            provider=provider,
            seed=0,
            state=RuntimeState(
                request_id=trace_context.request_id or plan.request_id,
                plan_id=plan.plan_id,
                side_effect_receipts=list(receipts or []),
            ),
            budget=RuntimeBudget(),
            trace=[],
            objective=plan.objective,
            runtime_backend="docker",
        )

    first_provider = ReconcilingReplayProvider([{"text": "cached answer", "model_name": "replay/small"}])
    first_context = make_context(base_trace_context, first_provider)
    first_response = first_context.run_model_request(
        instructions="instructions",
        prompt="prompt",
        model_class="medium",
        purpose="direct_response",
        payload={"node": "respond"},
    )
    resumed_trace_context = base_trace_context.model_copy(
        update={
            "request_id": "request.resume",
            "runtime_dir": str((tmp_path / "host-runtime").resolve()),
            "session_id": "session.resume",
            "run_node_id": "node.resume",
        }
    )
    resume_provider = ReconcilingReplayProvider([])
    resume_context = make_context(
        resumed_trace_context,
        resume_provider,
        receipts=first_context.state.side_effect_receipts,
    )

    replayed_response = resume_context.run_model_request(
        instructions="instructions",
        prompt="prompt",
        model_class="medium",
        purpose="direct_response",
        payload={"node": "respond"},
    )

    assert first_response.text == "cached answer"
    assert first_provider.generate_calls == 1
    assert resume_provider.generate_calls == 0
    assert replayed_response.text == "cached answer"
    assert replayed_response.raw["replayed_from_receipt"].startswith("provider-completion.")

def test_direct_resume_uses_loaded_checkpoint_ref_as_source_for_followup_checkpoints(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    envelope = _pending_provider_launch_envelope(
        runtime,
        shell,
        task_id="resume.direct-loaded-source",
    ).model_copy(
        update={
            "checkpoint_id": "checkpoint.resume.direct-loaded-source.0001",
            "boundary": "before_provider_launch",
            "side_effect_ledger": {"receipts": []},
        },
        deep=True,
    )
    checkpoint_ref = shell.save_checkpoint_envelope(envelope).ref
    loaded_envelope = shell.load_checkpoint_envelope(checkpoint_ref=checkpoint_ref)

    resumed_run = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([{"text": "hello after direct resume", "model_name": "replay/small"}]),
    ).resume_from_checkpoint(loaded_envelope)
    resumed_launch_checkpoint = _checkpoint_for_boundary(
        shell,
        loaded_envelope.request_id,
        "after_provider_launch",
    )
    resumed_checkpoint = _checkpoint_for_boundary(
        shell,
        loaded_envelope.request_id,
        "after_provider_completion",
    )

    assert Path(loaded_envelope.source_checkpoint_ref).resolve() == Path(checkpoint_ref).resolve()
    assert Path(resumed_launch_checkpoint.source_checkpoint_ref).resolve() == Path(checkpoint_ref).resolve()
    assert Path(resumed_launch_checkpoint.working_state.selected_checkpoint_refs[0]).resolve() == Path(checkpoint_ref).resolve()
    assert Path(resumed_checkpoint.source_checkpoint_ref).resolve() == Path(checkpoint_ref).resolve()
    assert resumed_run.hard_invalid is False

def test_repeated_resume_uses_selected_checkpoint_ref_instead_of_prior_lineage(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    prior_lineage_ref = "checkpoint://older-resume-source"
    envelope = _pending_provider_launch_envelope(
        runtime,
        shell,
        task_id="resume.direct-selected-source",
    ).model_copy(
        update={
            "checkpoint_id": "checkpoint.resume.direct-selected-source.0001",
            "boundary": "before_provider_launch",
            "source_checkpoint_ref": prior_lineage_ref,
            "side_effect_ledger": {"receipts": []},
        },
        deep=True,
    )
    checkpoint_ref = shell.save_checkpoint_envelope(envelope).ref
    loaded_envelope = shell.load_checkpoint_envelope(checkpoint_ref=checkpoint_ref)

    resumed_run = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([{"text": "hello after repeated resume", "model_name": "replay/small"}]),
    ).resume_from_checkpoint(loaded_envelope)
    resumed_checkpoint = _checkpoint_for_boundary(
        shell,
        loaded_envelope.request_id,
        "after_provider_completion",
    )

    assert loaded_envelope.source_checkpoint_ref == prior_lineage_ref
    assert Path(loaded_envelope.selected_checkpoint_ref).resolve() == Path(checkpoint_ref).resolve()
    assert Path(resumed_checkpoint.source_checkpoint_ref).resolve() == Path(checkpoint_ref).resolve()
    assert resumed_run.hard_invalid is False

def test_reduce_grouped_run_results_treats_paused_run_as_non_terminal():
    checkpoint_ref = "checkpoint://external/ref.json"
    reduction = reduce_grouped_run_results(
        [
            RunResult(
                request_id="group.paused",
                plan_id="plan.paused",
                run_id="run.paused",
                run_root="root",
                attempt_id="attempt_0001",
                runtime_hash="hash",
                task_id="task.paused",
                seed=0,
                artifact={"status": "waiting"},
                verifier_score=0.0,
                cost=0.0,
                latency=0.0,
                faults=0,
                hard_invalid=False,
                checkpoint_ref=checkpoint_ref,
                latest_checkpoint_ref=checkpoint_ref,
                run_lifecycle_state="paused",
                lifecycle_state="paused",
            )
        ]
    )

    assert reduction["lifecycle_state"] == "paused"
    assert reduction["latest_checkpoint_ref"] == checkpoint_ref
    assert reduction["resumable"] is True
    assert reduction["prune_eligible"] is False

def test_reduce_grouped_run_results_does_not_treat_payload_error_field_as_failure():
    reduction = reduce_grouped_run_results(
        [
            RunResult(
                request_id="group.payload-error",
                plan_id="plan.payload-error",
                run_id="run.payload-error",
                run_root="root",
                attempt_id="attempt_0001",
                runtime_hash="hash",
                task_id="task.payload-error",
                seed=0,
                artifact={"error": "none", "status": "ok"},
                verifier_score=1.0,
                cost=0.0,
                latency=0.0,
                faults=0,
                lifecycle_state="completed",
            )
        ]
    )

    assert reduction["lifecycle_state"] == "completed"
    assert reduction["failure_kind"] is None

def test_root_side_effect_receipts_are_not_duplicated_in_checkpoint_envelopes(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_direct_response_task("receipts.root.dedupe")
    run = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([{"text": "hello", "model_name": "replay/small"}]),
    ).run_task(task, 0)
    envelope = shell.load_checkpoint_envelope(checkpoint_ref=run.checkpoint_ref)

    request_receipts = [
        receipt
        for receipt in envelope.side_effect_ledger["receipts"]
        if receipt.action_kind == "provider_request"
    ]
    completion_receipts = [
        receipt
        for receipt in envelope.side_effect_ledger["receipts"]
        if receipt.action_kind == "provider_completion"
    ]

    assert len(request_receipts) == 1
    assert len(completion_receipts) == 1
    assert len({receipt.side_effect_id for receipt in envelope.side_effect_ledger["receipts"]}) == len(
        envelope.side_effect_ledger["receipts"]
    )

def test_sync_tool_path_publishes_launch_and_completion_receipts(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_builtin_sum_task("receipts.sync-tool")

    run = TaskRuntime(runtime, shell, ReplayProvider([])).run_task(task, 0)
    envelope = shell.load_checkpoint_envelope(checkpoint_ref=run.checkpoint_ref)

    launch_receipts = [
        receipt for receipt in envelope.side_effect_ledger["receipts"] if receipt.action_kind == "tool_launch"
    ]
    completion_receipts = [
        receipt for receipt in envelope.side_effect_ledger["receipts"] if receipt.action_kind == "tool_completion"
    ]

    assert len(launch_receipts) == 1
    assert len(completion_receipts) == 1
    assert launch_receipts[0].idempotency_key == completion_receipts[0].idempotency_key
    assert launch_receipts[0].result_ref["launch_mode"] == "sync"
    assert completion_receipts[0].result_ref["launch_mode"] == "sync"
    assert len(
        {
            (receipt.action_kind, receipt.idempotency_key)
            for receipt in envelope.side_effect_ledger["receipts"]
            if receipt.action_kind in {"tool_launch", "tool_completion"}
        }
    ) == 2

def test_resume_from_after_provider_completion_restores_completed_root_node_without_restart(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_direct_response_task("resume.after-provider-completion")
    first_run = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([{"text": "hello from checkpoint", "model_name": "replay/small"}]),
    ).run_task(task, 0)
    envelope = _checkpoint_for_boundary(shell, first_run.request_id, "after_provider_completion")

    assert envelope.runtime_state_snapshot.artifacts == {}
    assert envelope.runtime_state_snapshot.plan_node_status["respond"] == "running"

    resume_provider = ReconcilingReplayProvider([])
    resumed_run = TaskRuntime(runtime, shell, resume_provider).resume_from_checkpoint(envelope)
    trace_rows = resumed_run.trace_rows()

    assert resume_provider.generate_calls == 0
    assert resumed_run.hard_invalid is False
    assert resumed_run.artifact == first_run.artifact
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

def test_resume_from_after_tool_completion_restores_completed_root_node_without_restart(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_builtin_sum_task("resume.after-tool-completion")
    run_tool_calls = 0
    original_run_tool = shell.tool_executor.run_tool

    def counting_run_tool(tool_name, args, task_id):
        nonlocal run_tool_calls
        run_tool_calls += 1
        return original_run_tool(tool_name, args, task_id)

    monkeypatch.setattr(shell.tool_executor, "run_tool", counting_run_tool)

    first_run = TaskRuntime(runtime, shell, ReplayProvider([])).run_task(task, 0)
    envelope = _checkpoint_for_boundary(shell, first_run.request_id, "after_tool_completion")

    assert envelope.runtime_state_snapshot.artifacts == {}
    assert envelope.runtime_state_snapshot.plan_node_status["sum"] == "running"

    resumed_run = TaskRuntime(runtime, shell, ReplayProvider([])).resume_from_checkpoint(envelope)
    trace_rows = resumed_run.trace_rows()
    launch_receipts = [
        receipt for receipt in envelope.side_effect_ledger["receipts"] if receipt.action_kind == "tool_launch"
    ]
    completion_receipts = [
        receipt for receipt in envelope.side_effect_ledger["receipts"] if receipt.action_kind == "tool_completion"
    ]

    assert resumed_run.hard_invalid is False
    assert resumed_run.artifact == first_run.artifact
    assert run_tool_calls == 1
    assert len(launch_receipts) == 1
    assert len(completion_receipts) == 1
    assert launch_receipts[0].idempotency_key == completion_receipts[0].idempotency_key
    assert launch_receipts[0].result_ref["launch_mode"] == "sync"
    assert completion_receipts[0].result_ref["launch_mode"] == "sync"
    assert sum(
        1
        for row in trace_rows
        if row.get("event") == "node_started" and row.get("node_id") == "sum"
    ) == 0
    assert sum(
        1
        for row in trace_rows
        if row.get("event") == "node_reused_from_checkpoint" and row.get("node_id") == "sum"
    ) == 0
    assert [row["event"] for row in trace_rows if row.get("event") in {"terminal_emitted", "run_failed"}] == [
        "terminal_emitted"
    ]

def test_run_store_resolve_resume_target_accepts_external_checkpoint_ref(tmp_path):
    store = RunStore(tmp_path / "store")
    manifest = store.create_run(
        request_id="resume.external",
        evaluation_unit_id="resume.external",
        request_mode="user_request",
        runtime_backend="local",
    )
    checkpoint_dir = tmp_path / "external-checkpoints"
    checkpoint_dir.mkdir(parents=True)
    checkpoint_path = checkpoint_dir / "checkpoint.resume.external.json"
    checkpoint_envelope = CheckpointEnvelope(
        checkpoint_id="checkpoint.resume.external",
        runtime_contract_version=RUNTIME_CONTRACT_VERSION,
        runtime_hash="runtime-hash",
        run_id=manifest.run_id,
        run_root=manifest.run_root,
        request_id="resume.external",
        plan_id="plan.external",
        task_id="task.external",
        seed=0,
    )
    checkpoint_path.write_text(json.dumps((checkpoint_envelope).model_dump(), indent=2, sort_keys=True), encoding="utf-8")

    target = store.resolve_resume_target(checkpoint_ref=str(checkpoint_path))

    assert target.run_manifest.run_id == manifest.run_id
    assert target.checkpoint_path == checkpoint_path.resolve()
    assert target.checkpoint_store_dir == checkpoint_dir.resolve()

def test_run_store_resolve_resume_target_uses_manifest_external_latest_checkpoint_ref_for_run_ref(tmp_path):
    store = RunStore(tmp_path / "store")
    manifest = store.create_run(
        request_id="resume.manifest.latest-external",
        evaluation_unit_id="resume.manifest.latest-external",
        request_mode="user_request",
        runtime_backend="local",
    )
    checkpoint_dir = tmp_path / "external-checkpoints"
    checkpoint_dir.mkdir(parents=True)
    checkpoint_path = checkpoint_dir / "checkpoint.resume.manifest.latest-external.json"
    checkpoint_envelope = CheckpointEnvelope(
        checkpoint_id="checkpoint.resume.manifest.latest-external",
        runtime_contract_version=RUNTIME_CONTRACT_VERSION,
        runtime_hash="runtime-hash",
        run_id=manifest.run_id,
        run_root=manifest.run_root,
        request_id="resume.manifest.latest-external",
        plan_id="plan.manifest.latest-external",
        task_id="task.manifest.latest-external",
        seed=0,
    )
    checkpoint_path.write_text(json.dumps((checkpoint_envelope).model_dump(), indent=2, sort_keys=True), encoding="utf-8")
    store.write_run_manifest(
        manifest.model_copy(update={"latest_checkpoint_ref": str(checkpoint_path.resolve()), "resumable": True})
    )

    assert store.latest_usable_checkpoint_ref(manifest.run_id) == str(checkpoint_path.resolve())
    target = store.resolve_resume_target(run_ref=manifest.run_id)

    assert target.run_manifest.run_id == manifest.run_id
    assert target.checkpoint_path == checkpoint_path.resolve()
    assert target.checkpoint_store_dir == checkpoint_dir.resolve()

def test_run_store_resolve_resume_target_falls_back_from_stale_run_root_to_run_id(tmp_path):
    store = RunStore(tmp_path / "store")
    manifest = store.create_run(
        request_id="resume.external.stale-root",
        evaluation_unit_id="resume.external.stale-root",
        request_mode="user_request",
        runtime_backend="local",
    )
    checkpoint_dir = tmp_path / "external-checkpoints"
    checkpoint_dir.mkdir(parents=True)
    checkpoint_path = checkpoint_dir / "checkpoint.resume.external.stale-root.json"
    checkpoint_envelope = CheckpointEnvelope(
        checkpoint_id="checkpoint.resume.external.stale-root",
        runtime_contract_version=RUNTIME_CONTRACT_VERSION,
        runtime_hash="runtime-hash",
        run_id=manifest.run_id,
        run_root="/mnt/runs/run.resume.external.stale-root",
        request_id="resume.external.stale-root",
        plan_id="plan.external.stale-root",
        task_id="task.external.stale-root",
        seed=0,
    )
    checkpoint_path.write_text(json.dumps((checkpoint_envelope).model_dump(), indent=2, sort_keys=True), encoding="utf-8")

    target = store.resolve_resume_target(checkpoint_ref=str(checkpoint_path))

    assert target.run_manifest.run_id == manifest.run_id
    assert target.checkpoint_path == checkpoint_path.resolve()
    assert target.checkpoint_store_dir == checkpoint_dir.resolve()

def test_run_store_resolve_resume_target_prefers_matching_run_id_over_mismatched_local_run_root(tmp_path):
    store = RunStore(tmp_path / "store")
    expected_manifest = store.create_run(
        request_id="resume.external.correct-manifest",
        evaluation_unit_id="resume.external.correct-manifest",
        request_mode="user_request",
        runtime_backend="local",
    )
    mismatched_manifest = store.create_run(
        request_id="resume.external.wrong-manifest",
        evaluation_unit_id="resume.external.wrong-manifest",
        request_mode="user_request",
        runtime_backend="local",
    )
    checkpoint_dir = tmp_path / "external-checkpoints"
    checkpoint_dir.mkdir(parents=True)
    checkpoint_path = checkpoint_dir / "checkpoint.resume.external.mismatched-root.json"
    checkpoint_envelope = CheckpointEnvelope(
        checkpoint_id="checkpoint.resume.external.mismatched-root",
        runtime_contract_version=RUNTIME_CONTRACT_VERSION,
        runtime_hash="runtime-hash",
        run_id=expected_manifest.run_id,
        run_root=mismatched_manifest.run_root,
        request_id="resume.external.mismatched-root",
        plan_id="plan.external.mismatched-root",
        task_id="task.external.mismatched-root",
        seed=0,
    )
    checkpoint_path.write_text(json.dumps((checkpoint_envelope).model_dump(), indent=2, sort_keys=True), encoding="utf-8")

    target = store.resolve_resume_target(checkpoint_ref=str(checkpoint_path))

    assert target.run_manifest.run_id == expected_manifest.run_id
    assert target.run_manifest.run_root == expected_manifest.run_root
    assert target.checkpoint_path == checkpoint_path.resolve()
    assert target.checkpoint_store_dir == checkpoint_dir.resolve()

def test_resume_rebinds_request_identity_and_carries_forward_resume_provenance(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_direct_response_task("resume.rebind.identity")
    plan = compile_execution_plan_from_task(
        task,
        request_id="benchmark.resume.rebind.identity.seed_0",
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    queued_frame = QueuedFrameSnapshot(
        frame_id="frame-root",
        request_id=plan.request_id,
        plan_id=plan.plan_id,
        objective=plan.objective,
        operation_ids=["respond"],
        depth=0,
        role="root",
        tool_scope=sorted(shell.tool_registry.tools),
        model_class="medium",
        trace_context=plan.trace_context.model_copy(update={"op_id": "checkpoint-op", "run_node_id": "checkpoint-node"}),
        agent_snapshot=_canonical_root_snapshot(),
    )
    checkpoint_envelope = CheckpointEnvelope(
        checkpoint_id="checkpoint.resume.rebind.identity.0001",
        runtime_contract_version=runtime.kernel_manifest.runtime_contract_version,
        runtime_hash=runtime.runtime_hash,
        run_id="run.resume.rebind.identity",
        run_root=str(shell.workspace.resolve()),
        attempt_id="attempt_0001",
        runtime_backend="local",
        request_id=plan.request_id,
        plan_id=plan.plan_id,
        task_id=task.task_id,
        seed=0,
        sequence_no=1,
        boundary="before_resume",
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
            "plan_node_status": {},
            "branch_states": {},
            "branch_publications": [],
            "latest_checkpoint_ref": "checkpoint://run-latest",
        },
        working_state={"current_objective": task.prompt, "selected_checkpoint_refs": [plan.request_id]},
        trace_cursor={"last_solve_request_id": plan.request_id, "latest_runtime_event_sequence_no": 0},
    )

    solve_request, rebound_envelope, effective_request_id = solve_request_from_resume_checkpoint(
        checkpoint_envelope,
        request_id_override="resume.identity.override",
        source_checkpoint_ref="checkpoint://resume-source",
        trace_context=OpenAITraceContext(
            request_id="ignored-by-rebind",
            runtime_dir=str((tmp_path / "active-runtime").resolve()),
            runtime_session_id="sess.active",
            runtime_message_id="msg.active",
            runtime_message_index=3,
            op_id="override-op",
            run_node_id="override-node",
        ),
    )

    assert solve_request.request_id == "resume.identity.override"
    assert effective_request_id == "resume.identity.override"
    assert rebound_envelope.request_id == effective_request_id
    assert rebound_envelope.plan_snapshot["request_id"] == effective_request_id
    assert rebound_envelope.plan_snapshot["trace_context"]["request_id"] == effective_request_id
    assert rebound_envelope.plan_snapshot["trace_context"]["runtime_dir"] == str((tmp_path / "active-runtime").resolve())
    assert rebound_envelope.plan_snapshot["trace_context"]["runtime_session_id"] == "sess.active"
    assert rebound_envelope.plan_snapshot["trace_context"]["runtime_message_id"] == "msg.active"
    assert rebound_envelope.plan_snapshot["trace_context"]["runtime_message_index"] == 3
    assert rebound_envelope.runtime_state_snapshot.request_id == effective_request_id
    assert rebound_envelope.runtime_state_snapshot.latest_checkpoint_ref == "checkpoint://resume-source"
    assert rebound_envelope.selected_checkpoint_ref == "checkpoint://resume-source"
    assert rebound_envelope.runtime_state_snapshot.queued_frames[0].request_id == effective_request_id
    assert rebound_envelope.runtime_state_snapshot.queued_frames[0].trace_context.request_id == effective_request_id
    assert rebound_envelope.runtime_state_snapshot.queued_frames[0].trace_context.runtime_dir == str(
        (tmp_path / "active-runtime").resolve()
    )
    assert rebound_envelope.runtime_state_snapshot.queued_frames[0].trace_context.runtime_session_id == "sess.active"
    assert rebound_envelope.runtime_state_snapshot.queued_frames[0].trace_context.runtime_message_id == "msg.active"
    assert rebound_envelope.runtime_state_snapshot.queued_frames[0].trace_context.runtime_message_index == 3
    assert rebound_envelope.runtime_state_snapshot.queued_frames[0].trace_context.op_id == "checkpoint-op"
    assert rebound_envelope.runtime_state_snapshot.queued_frames[0].trace_context.run_node_id == "checkpoint-node"
    assert rebound_envelope.working_state.selected_checkpoint_refs == [plan.request_id]
    assert rebound_envelope.trace_cursor.last_solve_request_id == effective_request_id
    assert rebound_envelope.origin_request_id == plan.request_id
    assert rebound_envelope.source_checkpoint_ref == "checkpoint://resume-source"
    assert rebound_envelope.plan_id == plan.plan_id
    assert rebound_envelope.plan_snapshot["plan_digest"] == plan.plan_digest

    resumed_run = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([{"text": "hello after resume", "model_name": "replay/small"}]),
    ).resume_from_checkpoint(rebound_envelope)

    assert resumed_run.request_id == effective_request_id
    assert {row.get("request_id") for row in resumed_run.trace_rows() if row.get("request_id")} == {
        effective_request_id
    }
    resumed_checkpoint = shell.load_checkpoint_envelope(
        checkpoint_ref=resumed_run.latest_checkpoint_ref or resumed_run.checkpoint_ref
    )
    assert resumed_checkpoint.request_id == effective_request_id
    assert resumed_checkpoint.origin_request_id == plan.request_id
    assert resumed_checkpoint.source_checkpoint_ref == "checkpoint://resume-source"
    assert resumed_checkpoint.plan_id == plan.plan_id
    assert resumed_checkpoint.plan_snapshot["plan_digest"] == plan.plan_digest
    assert resumed_checkpoint.runtime_hash == runtime.runtime_hash
