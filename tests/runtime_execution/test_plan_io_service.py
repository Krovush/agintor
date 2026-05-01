from __future__ import annotations

from .helpers import (
    json,
    pytest,
    ArtifactMode,
    HardInvalidation,
    PromptAdaptationError,
    init_runtime,
    ReplayProvider,
    PolicyContext,
    RuntimeBudget,
    RuntimeState,
    compile_execution_plan_from_solve_request,
    compile_execution_plan_from_task,
    execution_plan_requirements,
    execution_plan_requires_default_provider,
    load_solve_request,
    load_runtime,
    TaskRuntime,
    BenchmarkTask,
    OpenAITraceContext,
    OperationSpec,
    capability_scope_allows,
    capability_scope_service_transports,
    service_action_transport_compatibility,
    FixedShell,
    CapturingProvider,
    _make_service_action_task,
    _checkpoint_for_boundary,
    _pending_service_action_launch_envelope,
)


def test_compile_execution_plan_rejects_duplicate_output_keys(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    task = BenchmarkTask(
        task_id="duplicate.outputs",
        family="top",
        prompt="Exercise duplicate output validation.",
        task_type="structured_ops",
        symbolic_seeds=[],
        file_paths=[],
        allowed_tool_categories=["math/basic"],
        context_items=[],
        operations=[
            OperationSpec(op_id="a", kind="builtin", output_key="same", description="first", args={"numbers": [1, 2]}),
            OperationSpec(op_id="b", kind="builtin", output_key="same", description="second", args={"numbers": [3, 4]}),
        ],
        expected=None,
        verifier_type="none",
        verification_required=False,
        allow_best_effort=True,
    )

    with pytest.raises(ValueError, match="duplicate execution plan output_key"):
        compile_execution_plan_from_task(
            task,
            request_id="duplicate.outputs.request",
            seed=0,
            runtime_hash=runtime.runtime_hash,
            runtime_dir=str(runtime.runtime_dir),
        )

def test_compile_execution_plan_from_solve_request_preserves_user_request_origin_and_plan_constants(tmp_path):
    solve_request = load_solve_request(
        prompt="Return a greeting as JSON.",
    )
    solve_request.output_schema = {"type": "object", "properties": {"message": {"type": "string"}}}

    task, plan = compile_execution_plan_from_solve_request(
        solve_request,
        seed=7,
        runtime_hash="runtime-hash",
        runtime_dir=str(tmp_path / "runtime"),
    )

    assert task.prompt == solve_request.prompt
    assert plan.origin.origin_kind == "user_request"
    assert plan.origin.source_request_id == solve_request.request_id
    assert plan.plan_constants["respond.request_id"] == solve_request.request_id
    assert plan.plan_constants["respond.output_schema"] == solve_request.output_schema
    assert {binding.source_ref for binding in plan.nodes[0].input_bindings if binding.source_kind == "plan_constant"} == {
        "respond.request_id",
        "respond.output_schema",
    }

def test_compile_execution_plan_from_solve_request_enriches_runtime_identity(tmp_path):
    solve_request = load_solve_request(prompt="Return a greeting as JSON.")

    task, plan = compile_execution_plan_from_solve_request(
        solve_request,
        seed=7,
        runtime_hash="runtime-hash",
        runtime_dir=str(tmp_path / "runtime"),
        trace_context=OpenAITraceContext(
            session_id="session-1",
            build_id="build-1",
            request_id="stale-request",
            task_id="stale-task",
            seed=999,
        ),
    )

    assert plan.trace_context.session_id == "session-1"
    assert plan.trace_context.build_id == "build-1"
    assert plan.trace_context.provider_role == "runtime"
    assert plan.trace_context.request_id == solve_request.request_id
    assert plan.trace_context.task_id == task.task_id
    assert plan.trace_context.seed == 7
    assert plan.trace_context.runtime_hash == "runtime-hash"
    assert plan.trace_context.runtime_dir == str(tmp_path / "runtime")

def test_execution_plan_digest_ignores_trace_provenance(monkeypatch, tmp_path):
    solve_request = load_solve_request(prompt="Return a greeting as JSON.")

    monkeypatch.setenv("AGINTOR_OPENAI_TRACE_SESSION_ID", "session.digest-one")
    _, first_plan = compile_execution_plan_from_solve_request(
        solve_request,
        seed=7,
        runtime_hash="runtime-hash",
        runtime_dir=str(tmp_path / "runtime"),
    )

    monkeypatch.setenv("AGINTOR_OPENAI_TRACE_SESSION_ID", "session.digest-two")
    _, second_plan = compile_execution_plan_from_solve_request(
        solve_request,
        seed=7,
        runtime_hash="runtime-hash",
        runtime_dir=str(tmp_path / "other-runtime"),
    )

    assert first_plan.trace_context.session_id == "session.digest-one"
    assert second_plan.trace_context.session_id == "session.digest-two"
    assert first_plan.trace_context.runtime_dir != second_plan.trace_context.runtime_dir
    assert first_plan.plan_digest == second_plan.plan_digest

def test_compile_execution_plan_from_solve_request_builds_file_inspection_template(tmp_path):
    inspected_file = tmp_path / "Folder With Spaces" / "notes file.txt"
    inspected_file.parent.mkdir(parents=True, exist_ok=True)
    inspected_file.write_text("important runtime note\n", encoding="utf-8")
    solve_request = load_solve_request(prompt=f"Inspect {inspected_file} and summarize it.")

    task, plan = compile_execution_plan_from_solve_request(
        solve_request,
        seed=3,
        runtime_hash="runtime-hash",
        runtime_dir=str(tmp_path / "runtime"),
    )

    assert task.task_type == "file_inspection"
    assert plan.file_ref_specs[0].source_path == str(inspected_file)
    assert plan.file_ref_specs[0].runtime_path == str(inspected_file)
    read_node = next(node for node in plan.nodes if node.node_id == "read_file_0")
    respond_node = next(node for node in plan.nodes if node.node_id == "respond")
    assert read_node.node_kind == "tool_call"
    assert respond_node.node_kind == "direct_response"
    assert any(
        binding.source_kind == "request_file" and binding.source_ref == str(inspected_file)
        for binding in read_node.input_bindings
    )
    assert any(
        binding.source_kind == "upstream_output" and binding.source_ref == "read_file_0"
        for binding in respond_node.input_bindings
    )
    assert execution_plan_requires_default_provider(plan) is True

def test_file_inspection_prompt_reads_file_contents_before_direct_response(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    inspected_file = tmp_path / "Folder With Spaces" / "notes file.txt"
    inspected_file.parent.mkdir(parents=True, exist_ok=True)
    inspected_file.write_text("important runtime note\n", encoding="utf-8")
    solve_request = load_solve_request(prompt=f"Inspect {inspected_file} and summarize it.")
    task, plan = compile_execution_plan_from_solve_request(
        solve_request,
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    provider = CapturingProvider(response_text="inspection-complete")

    result = TaskRuntime(runtime, shell, provider).run_task(
        task,
        0,
        request_id=solve_request.request_id,
        plan=plan,
    )

    assert result.hard_invalid is False
    assert result.artifact == "inspection-complete"
    assert provider.prompts
    assert "important runtime note" in provider.prompts[0]
    assert str(inspected_file) in provider.prompts[0]

def test_file_inspection_prompt_accepts_filesystem_family_scope_and_executes_read_tool(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    inspected_file = tmp_path / "notes.txt"
    inspected_file.write_text("important runtime note\n", encoding="utf-8")
    solve_request = load_solve_request(prompt="Inspect the supplied file and summarize it.")
    solve_request.file_paths = [str(inspected_file)]
    solve_request.allowed_tool_categories = ["filesystem/*"]

    task, plan = compile_execution_plan_from_solve_request(
        solve_request,
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    provider = CapturingProvider(response_text="inspection-complete")

    result = TaskRuntime(runtime, shell, provider).run_task(
        task,
        0,
        request_id=solve_request.request_id,
        plan=plan,
    )

    read_node = next(node for node in plan.nodes if node.node_id == "read_file_0")
    assert task.allowed_tool_categories == ["filesystem/*"]
    assert any(
        binding.source_kind == "request_file" and binding.source_ref == str(inspected_file)
        for binding in read_node.input_bindings
    )
    assert result.hard_invalid is False
    assert result.artifact == "inspection-complete"
    assert "important runtime note" in provider.prompts[0]

def test_compile_execution_plan_from_solve_request_builds_repo_patch_template(tmp_path):
    target_file = tmp_path / "Folder With Spaces" / "app file.py"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("value = 'foo'\n", encoding="utf-8")
    solve_request = load_solve_request(prompt=f"Update {target_file} to replace foo with bar.")

    task, plan = compile_execution_plan_from_solve_request(
        solve_request,
        seed=5,
        runtime_hash="runtime-hash",
        runtime_dir=str(tmp_path / "runtime"),
    )

    assert task.task_type == "bounded_repo_patch"
    patch_node = next(node for node in plan.nodes if node.node_id == "apply_patch")
    assert patch_node.node_kind == "repo_patch"
    assert execution_plan_requires_default_provider(plan) is True

def test_compile_execution_plan_from_solve_request_builds_repo_patch_template_for_new_absolute_host_target(tmp_path):
    target_file = tmp_path / "Folder With Spaces" / "new file.py"
    solve_request = load_solve_request(prompt=f"Update {target_file} to add a hello world implementation.")

    task, plan = compile_execution_plan_from_solve_request(
        solve_request,
        seed=5,
        runtime_hash="runtime-hash",
        runtime_dir=str(tmp_path / "runtime"),
    )

    assert task.task_type == "bounded_repo_patch"
    assert plan.file_ref_specs[0].source_path == str(target_file)
    assert plan.file_ref_specs[0].runtime_path == str(target_file.resolve())
    assert plan.file_ref_specs[0].host_path == str(target_file.resolve())

def test_repo_patch_prompt_updates_target_file(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    target_file = tmp_path / "Folder With Spaces" / "app file.py"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("value = 'foo'\n", encoding="utf-8")
    solve_request = load_solve_request(prompt=f"Update {target_file} to replace foo with bar.")
    task, plan = compile_execution_plan_from_solve_request(
        solve_request,
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    provider = CapturingProvider(
        response_text=json.dumps(
            {
                "summary": "Updated the target file.",
                "files": [
                    {
                        "path": str(target_file),
                        "updated_content": "value = 'bar'\n",
                    }
                ],
            }
        )
    )

    result = TaskRuntime(runtime, shell, provider).run_task(
        task,
        0,
        request_id=solve_request.request_id,
        plan=plan,
    )

    assert result.hard_invalid is False
    assert target_file.read_text(encoding="utf-8") == "value = 'bar'\n"
    assert result.artifact["applied"] is True
    assert result.artifact["updated_files"][0]["path"] == str(target_file)
    assert "-value = 'foo'" in result.artifact["updated_files"][0]["diff"]
    assert "+value = 'bar'" in result.artifact["updated_files"][0]["diff"]

def test_repo_patch_prompt_can_create_new_absolute_host_target(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    target_file = tmp_path / "Folder With Spaces" / "new file.py"
    solve_request = load_solve_request(prompt=f"Update {target_file} to add hello world code.")
    task, plan = compile_execution_plan_from_solve_request(
        solve_request,
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    provider = CapturingProvider(
        response_text=json.dumps(
            {
                "summary": "Created the target file.",
                "files": [
                    {
                        "path": str(target_file.resolve()),
                        "updated_content": "print('hello world')\n",
                    }
                ],
            }
        )
    )

    result = TaskRuntime(runtime, shell, provider).run_task(
        task,
        0,
        request_id=solve_request.request_id,
        plan=plan,
    )

    assert result.hard_invalid is False
    assert target_file.read_text(encoding="utf-8") == "print('hello world')\n"
    assert result.artifact["applied"] is True

def test_repo_patch_publishes_prewrite_filesystem_checkpoint_and_completion_receipts(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    target_file = tmp_path / "app.py"
    target_file.write_text("value = 'foo'\n", encoding="utf-8")
    solve_request = load_solve_request(prompt=f"Update {target_file} to replace foo with bar.")
    task, plan = compile_execution_plan_from_solve_request(
        solve_request,
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    provider = CapturingProvider(
        response_text=json.dumps(
            {
                "summary": "Updated the target file.",
                "files": [
                    {
                        "path": str(target_file),
                        "updated_content": "value = 'bar'\n",
                    }
                ],
            }
        )
    )

    result = TaskRuntime(runtime, shell, provider).run_task(
        task,
        0,
        request_id=solve_request.request_id,
        plan=plan,
    )
    launch_envelope = _checkpoint_for_boundary(shell, solve_request.request_id, "before_filesystem_write")
    completion_envelope = _checkpoint_for_boundary(shell, solve_request.request_id, "after_filesystem_write")
    launch_receipts = [
        receipt
        for receipt in launch_envelope.side_effect_ledger["receipts"]
        if receipt.action_kind == "filesystem_write"
    ]
    completion_receipts = [
        receipt
        for receipt in completion_envelope.side_effect_ledger["receipts"]
        if receipt.action_kind == "filesystem_write"
    ]

    assert result.hard_invalid is False
    assert target_file.read_text(encoding="utf-8") == "value = 'bar'\n"
    assert [receipt.status for receipt in launch_receipts] == ["launched"]
    assert {receipt.status for receipt in completion_receipts} == {"launched", "completed"}
    assert completion_receipts[-1].result_ref["output"]["applied"] is True

def test_resume_from_before_filesystem_write_reuses_cached_patch_without_provider_reissue(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    target_file = tmp_path / "app.py"
    target_file.write_text("value = 'foo'\n", encoding="utf-8")
    solve_request = load_solve_request(prompt=f"Update {target_file} to replace foo with bar.")
    task, plan = compile_execution_plan_from_solve_request(
        solve_request,
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    first_provider = CapturingProvider(
        response_text=json.dumps(
            {
                "summary": "Updated the target file.",
                "files": [
                    {
                        "path": str(target_file),
                        "updated_content": "value = 'bar'\n",
                    }
                ],
            }
        )
    )
    first_run = TaskRuntime(runtime, shell, first_provider).run_task(
        task,
        0,
        request_id=solve_request.request_id,
        plan=plan,
    )
    launch_envelope = _checkpoint_for_boundary(shell, solve_request.request_id, "before_filesystem_write")
    target_file.write_text("value = 'foo'\n", encoding="utf-8")
    resume_provider = CapturingProvider(response_text="should-not-be-used")

    resumed_run = TaskRuntime(runtime, shell, resume_provider).resume_from_checkpoint(launch_envelope)

    assert first_run.hard_invalid is False
    assert resumed_run.hard_invalid is False
    assert not resume_provider.prompts
    assert target_file.read_text(encoding="utf-8") == "value = 'bar'\n"
    assert resumed_run.artifact["applied"] is True
    assert any(
        row.get("event") == "side_effect_reconciled"
        and row.get("reconciliation_status") == "filesystem_prewrite_state_intact"
        for row in resumed_run.trace_rows()
    )

def test_resume_strict_fails_closed_on_ambiguous_filesystem_write_launch_without_provider_reissue(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    target_file = tmp_path / "app.py"
    target_file.write_text("value = 'foo'\n", encoding="utf-8")
    solve_request = load_solve_request(prompt=f"Update {target_file} to replace foo with bar.")
    task, plan = compile_execution_plan_from_solve_request(
        solve_request,
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    first_provider = CapturingProvider(
        response_text=json.dumps(
            {
                "summary": "Updated the target file.",
                "files": [
                    {
                        "path": str(target_file),
                        "updated_content": "value = 'bar'\n",
                    }
                ],
            }
        )
    )
    first_run = TaskRuntime(runtime, shell, first_provider).run_task(
        task,
        0,
        request_id=solve_request.request_id,
        plan=plan,
    )
    launch_envelope = _checkpoint_for_boundary(shell, solve_request.request_id, "before_filesystem_write")
    target_file.write_text("value = 'partial'\n", encoding="utf-8")
    resume_provider = CapturingProvider(response_text="should-not-be-used")

    resumed_run = TaskRuntime(runtime, shell, resume_provider).resume_from_checkpoint(
        launch_envelope,
        reconciliation_policy="strict",
    )

    assert first_run.hard_invalid is False
    assert resumed_run.hard_invalid is True
    assert resumed_run.failure_kind == "receipt_reconciliation_failed"
    assert not resume_provider.prompts
    assert target_file.read_text(encoding="utf-8") == "value = 'partial'\n"

def test_request_file_relative_paths_resolve_against_runtime_workspace_not_process_cwd(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    runtime.deployment_contract.runtime_isolation_policy.workspace_root = "repo"
    workspace_file = shell.workspace / "repo" / "app.py"
    workspace_file.parent.mkdir(parents=True, exist_ok=True)
    workspace_file.write_text("workspace-version\n", encoding="utf-8")
    other_cwd = tmp_path / "other-cwd"
    other_file = other_cwd / "app.py"
    other_file.parent.mkdir(parents=True, exist_ok=True)
    other_file.write_text("cwd-version\n", encoding="utf-8")
    monkeypatch.chdir(other_cwd)

    solve_request = load_solve_request(prompt="Inspect the repo file and summarize it.")
    solve_request.file_paths = ["app.py"]
    solve_request.request_file_refs = []
    task, plan = compile_execution_plan_from_solve_request(
        solve_request,
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    provider = CapturingProvider(response_text="inspection-complete")

    result = TaskRuntime(runtime, shell, provider).run_task(
        task,
        0,
        request_id=solve_request.request_id,
        plan=plan,
    )

    assert result.hard_invalid is False
    assert "workspace-version" in provider.prompts[0]
    assert "cwd-version" not in provider.prompts[0]

def test_repo_patch_prompt_rejects_relative_path_escape_from_runtime_workspace(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    repo_dir = shell.workspace / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    target_file = repo_dir / "app.py"
    target_file.write_text("value = 'foo'\n", encoding="utf-8")
    solve_request = load_solve_request(prompt="Update the target file to replace foo with bar.")
    solve_request.file_paths = ["repo/app.py"]
    task, plan = compile_execution_plan_from_solve_request(
        solve_request,
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    provider = CapturingProvider(
        response_text=json.dumps(
            {
                "summary": "Attempted to escape the workspace.",
                "files": [
                    {
                        "path": "../escape.py",
                        "updated_content": "value = 'bad'\n",
                    }
                ],
            }
        )
    )

    result = TaskRuntime(runtime, shell, provider).run_task(
        task,
        0,
        request_id=solve_request.request_id,
        plan=plan,
    )

    assert result.hard_invalid is True
    assert target_file.read_text(encoding="utf-8") == "value = 'foo'\n"
    assert not (shell.workspace.parent / "escape.py").exists()

def test_compile_execution_plan_from_solve_request_builds_service_action_template(tmp_path):
    solve_request = load_solve_request(prompt="GET https://service.example.test/status")
    solve_request.allowed_tool_categories = ["service/*"]

    task, plan = compile_execution_plan_from_solve_request(
        solve_request,
        seed=1,
        runtime_hash="runtime-hash",
        runtime_dir=str(tmp_path / "runtime"),
    )

    requirements = execution_plan_requirements(plan)
    assert task.task_type == "bounded_service_action"
    assert task.allowed_tool_categories == ["service/*"]
    service_node = next(node for node in plan.nodes if node.node_id == "service_call")
    assert service_node.node_kind == "service_action"
    assert service_node.metadata["service_transport"] == "http"
    assert plan.plan_constants["service_call.service_transport"] == "http"
    assert requirements.required_tool_categories == ["service/http"]
    assert requirements.required_network_transports == ["http"]
    assert execution_plan_requires_default_provider(plan) is False

def test_compile_execution_plan_from_solve_request_rejects_service_action_when_service_tools_are_disallowed(tmp_path):
    solve_request = load_solve_request(prompt="GET https://service.example.test/status")
    solve_request.allowed_tool_categories = ["filesystem/read"]

    with pytest.raises(PromptAdaptationError, match="service/\\* allowed_tool_categories capability"):
        compile_execution_plan_from_solve_request(
            solve_request,
            seed=1,
            runtime_hash="runtime-hash",
            runtime_dir=str(tmp_path / "runtime"),
        )

def test_capability_scope_helpers_expand_family_scopes():
    assert capability_scope_allows(["filesystem/*"], "filesystem/read") is True
    assert capability_scope_allows(["filesystem/*"], "filesystem/patch") is True
    assert capability_scope_allows(["service/*"], "service/http") is True
    assert capability_scope_service_transports(["service/*"]) == ["http"]

    compatibility = service_action_transport_compatibility(
        url="https://service.example.test/status",
        allowed_tool_categories=["service/*"],
    )

    assert compatibility.transport == "http"
    assert compatibility.allowed_schemes == ("http", "https")

def test_compile_execution_plan_stamps_explicit_capability_intent_metadata(tmp_path):
    target_file = tmp_path / "app.py"
    target_file.write_text("value = 'foo'\n", encoding="utf-8")
    repo_request = load_solve_request(prompt=f"Update {target_file} to replace foo with bar.")
    _, repo_plan = compile_execution_plan_from_solve_request(
        repo_request,
        seed=5,
        runtime_hash="runtime-hash",
        runtime_dir=str(tmp_path / "runtime"),
    )
    service_request = load_solve_request(prompt="GET https://service.example.test/status")
    _, service_plan = compile_execution_plan_from_solve_request(
        service_request,
        seed=6,
        runtime_hash="runtime-hash",
        runtime_dir=str(tmp_path / "runtime"),
    )

    patch_node = next(node for node in repo_plan.nodes if node.node_id == "apply_patch")
    service_node = next(node for node in service_plan.nodes if node.node_id == "service_call")

    assert patch_node.metadata["capability_intent"]["requires_default_provider"] is True
    assert patch_node.metadata["capability_intent"]["requires_filesystem_write"] is True
    assert "filesystem/patch" in patch_node.metadata["capability_intent"]["required_tool_categories"]
    assert service_node.metadata["capability_intent"]["requires_network_access"] is True
    assert service_node.metadata["capability_intent"]["network_transports"] == ["http"]
    assert any(
        category.startswith("service/")
        for category in service_node.metadata["capability_intent"]["required_tool_categories"]
    )
    requirements = execution_plan_requirements(service_plan)
    assert requirements.requires_network_access is True
    assert requirements.required_network_transports == ["http"]
    assert requirements.network_transport_nodes == {"http": ["service_call"]}

def test_compile_execution_plan_from_solve_request_rejects_non_http_service_action_url(tmp_path):
    solve_request = load_solve_request(prompt="GET file:///tmp/secret.txt")

    with pytest.raises(PromptAdaptationError, match="only permits URL schemes"):
        compile_execution_plan_from_solve_request(
            solve_request,
            seed=1,
            runtime_hash="runtime-hash",
            runtime_dir=str(tmp_path / "runtime"),
        )

def test_compile_execution_plan_from_task_rejects_service_action_urls_outside_http_transport(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    task = _make_service_action_task(
        "service.invalid-scheme",
        url="file:///tmp/secret.txt",
    )

    with pytest.raises(ValueError, match="only permits URL schemes"):
        compile_execution_plan_from_task(
            task,
            request_id="service.invalid-scheme.request",
            seed=0,
            runtime_hash=runtime.runtime_hash,
            runtime_dir=str(runtime.runtime_dir),
        )

def test_service_action_node_executes_bounded_http_request(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    runtime.deployment_contract.network_policy = "open"
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    solve_request = load_solve_request(prompt="GET https://service.example.test/status")
    solve_request.allowed_tool_categories = ["service/*"]
    task, plan = compile_execution_plan_from_solve_request(
        solve_request,
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    captured: dict[str, object] = {}

    class _FakeResponse:
        def __init__(self):
            self.status = 200
            self.headers = {"Content-Type": "application/json"}

        def read(self):
            return b'{"ok": true}'

        def getcode(self):
            return self.status

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr("agintor.runtime.kernel.io.service_action.urllib_request.urlopen", fake_urlopen)

    result = TaskRuntime(runtime, shell, ReplayProvider([])).run_task(
        task,
        0,
        request_id=solve_request.request_id,
        plan=plan,
    )

    assert result.hard_invalid is False
    assert captured == {
        "url": "https://service.example.test/status",
        "method": "GET",
        "timeout": 10.0,
    }
    assert result.artifact["status_code"] == 200
    assert result.artifact["body"] == {"ok": True}
    assert result.provider_usage.get("calls", 0) == 0

def test_service_action_executor_rejects_non_http_scheme_before_dispatch(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    runtime.deployment_contract.network_policy = "open"
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_service_action_task("service.executor.scheme-guard")
    plan = compile_execution_plan_from_task(
        task,
        request_id="service.executor.scheme-guard.request",
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    runner = TaskRuntime(runtime, shell, ReplayProvider([]))
    state = RuntimeState(
        request_id=plan.request_id,
        plan_id=plan.plan_id,
        execution_state="running",
        visible_tool_names=sorted(shell.tool_registry.tools),
    )
    context = PolicyContext(
        runtime_dir=runtime.runtime_dir,
        shell=shell,
        task=task,
        request_id=plan.request_id,
        plan=plan,
        trace_context=plan.trace_context,
        provider=ReplayProvider([]),
        seed=0,
        state=state,
        budget=RuntimeBudget(),
        trace=[],
        objective=plan.objective,
        runtime_backend="local",
    )
    operation = next(node for node in plan.nodes if node.node_id == "service_call")
    dispatch_calls = {"count": 0}

    def fail_if_called(*args, **kwargs):
        dispatch_calls["count"] += 1
        raise AssertionError("urlopen should not be called for incompatible service_action schemes")

    monkeypatch.setattr("agintor.runtime.kernel.io.service_action.urllib_request.urlopen", fail_if_called)

    with pytest.raises(HardInvalidation, match="only permits URL schemes"):
        runner._execute_service_action_node(
            context,
            operation,
            {
                "url": "file:///tmp/secret.txt",
                "method": "GET",
                "headers": {},
                "body": None,
                "timeout_s": 10.0,
                "service_transport": "http",
            },
            plan.trace_context,
        )

    assert dispatch_calls["count"] == 0

def test_service_action_publishes_launch_receipt_before_dispatch_and_completion_after_success(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    runtime.deployment_contract.network_policy = "open"
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_service_action_task("service.receipts")
    plan = compile_execution_plan_from_task(
        task,
        request_id="service.receipts.request",
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )

    class _FakeResponse:
        def __init__(self):
            self.status = 200
            self.headers = {"Content-Type": "application/json"}

        def read(self):
            return b'{"ok": true}'

        def getcode(self):
            return self.status

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("agintor.runtime.kernel.io.service_action.urllib_request.urlopen", lambda request, timeout=0: _FakeResponse())

    result = TaskRuntime(runtime, shell, ReplayProvider([])).run_task(
        task,
        0,
        request_id=plan.request_id,
        plan=plan,
    )
    launch_envelope = _checkpoint_for_boundary(shell, plan.request_id, "after_service_action_launch")
    completion_envelope = _checkpoint_for_boundary(
        shell,
        plan.request_id,
        "after_service_action_completion",
    )
    launch_receipts = [
        receipt
        for receipt in launch_envelope.side_effect_ledger["receipts"]
        if receipt.action_kind == "service_action"
    ]
    completion_receipts = [
        receipt
        for receipt in completion_envelope.side_effect_ledger["receipts"]
        if receipt.action_kind == "service_action"
    ]

    assert result.hard_invalid is False
    assert [receipt.status for receipt in launch_receipts] == ["launched"]
    assert {receipt.status for receipt in completion_receipts} == {"launched", "completed"}
    assert any(receipt.result_ref.get("output", {}).get("status_code") == 200 for receipt in completion_receipts)

def test_resume_strict_fails_closed_on_unreconciled_service_action_launch_without_reissue(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    runtime.deployment_contract.network_policy = "open"
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    envelope = _pending_service_action_launch_envelope(
        runtime,
        shell,
        task_id="resume.strict-service-action-launch",
    )
    dispatch_calls = {"count": 0}

    def fail_if_called(*args, **kwargs):
        dispatch_calls["count"] += 1
        raise AssertionError("service action should not be reissued during strict resume")

    monkeypatch.setattr("agintor.runtime.kernel.io.service_action.urllib_request.urlopen", fail_if_called)

    resumed = TaskRuntime(runtime, shell, ReplayProvider([])).resume_from_checkpoint(
        envelope,
        reconciliation_policy="strict",
    )

    assert resumed.hard_invalid is True
    assert resumed.failure_kind == "receipt_reconciliation_failed"
    assert dispatch_calls["count"] == 0

def test_resume_best_effort_blocks_unreconciled_service_action_launch_without_reissue(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    runtime.deployment_contract.network_policy = "open"
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    envelope = _pending_service_action_launch_envelope(
        runtime,
        shell,
        task_id="resume.best-effort-service-action-launch",
    )
    dispatch_calls = {"count": 0}

    def fail_if_called(*args, **kwargs):
        dispatch_calls["count"] += 1
        raise AssertionError("service action should not be reissued during best-effort resume")

    monkeypatch.setattr("agintor.runtime.kernel.io.service_action.urllib_request.urlopen", fail_if_called)

    resumed = TaskRuntime(runtime, shell, ReplayProvider([])).resume_from_checkpoint(
        envelope,
        reconciliation_policy="best_effort",
    )

    assert resumed.hard_invalid is False
    assert resumed.artifact["error"] == "recovery_blocked"
    assert resumed.artifact["node_id"] == "service_call"
    assert dispatch_calls["count"] == 0
