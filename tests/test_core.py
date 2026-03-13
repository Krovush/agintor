from __future__ import annotations

import json
from pathlib import Path

import pytest

import agintor.container_runtime as container_runtime_module

from agintor.archive import QualityDiversityArchive, ScopeScheduler
from agintor.benchmarks import BenchmarkSuite, build_demo_suite
from agintor.container_runtime import DockerRuntimeExecutor
from agintor.evaluator import RuntimeEvaluator
from agintor.exceptions import HardInvalidation, PatchApplyError, SafetyViolation, ValidationError
from agintor.memory_graph import LongTermGraph, ShortTermGraph
from agintor.patches import apply_patch_to_text, build_patch
from agintor.providers import LocalDeterministicProvider, MiniMaxProvider, OpenAIProvider, build_provider
from agintor.project import init_runtime
from agintor.runtime_api import AgentFrame, PolicyContext, RuntimeBudget, RuntimeState
from agintor.runtime_loader import load_runtime
from agintor.runtime_profile import HostedProviderProfile
from agintor.runner import TaskRuntime
from agintor.schemas import (
    AgentTemplate,
    ArchiveEntry,
    AsyncHandle,
    BenchmarkTask,
    Checkpoint,
    ChildSpec,
    EdgeType,
    LongTermNodeType,
    MemoryNode,
    ModelRequest,
    ModelResponse,
    MutationCandidate,
    NodeType,
    OperationSpec,
    RunResult,
    SuiteEvaluation,
    SummaryRecord,
    ToolSpec,
)
from agintor.shell import AgentPool, FixedShell
from agintor.tool_runtime import SafetyGuard, SandboxManager, validate_expression_tool, validate_tool_candidate


def test_patch_roundtrip_exact_replace() -> None:
    source = "alpha\nbeta\ngamma\n"
    patch = build_patch("beta", "delta")
    updated = apply_patch_to_text(source, patch)
    assert updated == "alpha\ndelta\ngamma\n"



def test_short_term_summary_preserves_backlinks() -> None:
    graph = ShortTermGraph()
    first = graph.add_node("RawBlob", "first", {"text": "hello"})
    second = graph.add_node("RawBlob", "second", {"text": "world"})
    summary = SummaryRecord(objective="obj", evidence=["hello world"], artifacts=[], unresolved=[], open_handles=[], next_actions=[], symbols=[], verifier_state={}, provenance={})
    summary_id = graph.summary_replace([first, second], summary)
    assert summary_id in graph.nodes
    backlinked_targets = [edge.dst for edge in graph.edges if edge.src == summary_id and edge.type == "BACKLINKS_TO"]
    assert set(backlinked_targets) == {first, second}



def test_long_term_exact_symbol_dominates_similarity() -> None:
    graph = LongTermGraph()
    exact = MemoryNode(node_id="n1", type="Symbol", label="ALPHA_7", content="17", embedding=[], symbol_set=["ALPHA_7"], file_paths=[], source_task_id="t", verifier_support=1.0, timestamps={"created": 0.0}, provenance={"source": "task_context"}, tombstoned=False)
    similar = MemoryNode(node_id="n2", type="TaskNote", label="alpha seven maybe", content="value around 17", embedding=[], symbol_set=[], file_paths=[], source_task_id="t", verifier_support=0.2, timestamps={"created": 0.0}, provenance={"source": "other"}, tombstoned=False)
    graph.upsert(exact)
    graph.upsert(similar)
    candidates = graph.retrieve_candidates("What value does ALPHA_7 map to?", ["ALPHA_7"], [])
    assert candidates[0].node_id == "n1"



def test_validate_expression_tool_and_execute() -> None:
    source, executor = validate_expression_tool("sum(x*x for x in numbers) % modulus", [{"input": {"numbers": [1, 2, 3], "modulus": 7}, "expected": 0}], SafetyGuard())
    assert "def run" in source
    assert executor(numbers=[1, 2, 3], modulus=7) == 0


def test_validate_expression_tool_allows_safe_builtin_arg_names() -> None:
    _, executor = validate_expression_tool("sum + x", [{"input": {"sum": 5, "x": 2}, "expected": 7}], SafetyGuard())
    assert executor(sum=5, x=2) == 7
    _, math_executor = validate_expression_tool("math + 1", [{"input": {"math": 4}, "expected": 5}], SafetyGuard())
    assert math_executor(math=4) == 5


def test_validate_tool_candidate_replays_deterministically() -> None:
    source, _ = validate_expression_tool("a+b", [{"input": {"a": 2, "b": 3}, "expected": 5}], SafetyGuard())
    spec = ToolSpec(name="generated/local/validate", category_path=["generated", "local"], signature="(a,b)->value", description="test", runtime="python", deps=[], permissions=[], tests=[{"input": {"a": 2, "b": 3}, "expected": 5}], backgroundable=False, state_schema={}, source_digest="x", build_cmd="python -m py_compile tool.py", run_cmd="python tool.py", timeout_s=10, determinism_class="stable")
    result = validate_tool_candidate(spec, source, SafetyGuard())
    assert result["checked_tests"] == 1
    assert result["deterministic"] is True


def test_validate_tool_candidate_rejects_signature_mismatch() -> None:
    spec = ToolSpec(name="generated/local/bad_sig", category_path=["generated", "local"], signature="(a,b)->value", description="bad signature", runtime="python", deps=[], permissions=[], tests=[], backgroundable=False, state_schema={}, source_digest="x", build_cmd="python -m py_compile tool.py", run_cmd="python tool.py", timeout_s=10, determinism_class="stable")
    with pytest.raises(ValidationError):
        validate_tool_candidate(spec, "def run(a):\n    return a\n", SafetyGuard())


def test_validate_tool_candidate_rejects_top_level_side_effects(tmp_path: Path) -> None:
    marker = tmp_path / "side_effect.txt"
    source = (
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('boom', encoding='utf-8')\n\n"
        "def run(**kwargs):\n"
        "    return 1\n"
    )
    spec = ToolSpec(name="generated/local/unsafe", category_path=["generated", "local"], signature="()->value", description="unsafe", runtime="python", deps=[], permissions=[], tests=[{"input": {}, "expected": 1}], backgroundable=False, state_schema={}, source_digest="x", build_cmd="python -m py_compile tool.py", run_cmd="python tool.py", timeout_s=10, determinism_class="stable")
    with pytest.raises(SafetyViolation):
        validate_tool_candidate(spec, source, SafetyGuard(), SandboxManager(tmp_path / "sandboxes"))
    assert marker.exists() is False


def test_schema_models_preserve_mandatory_spec_fields() -> None:
    def field_names(model_cls) -> set[str]:
        fields = getattr(model_cls, "model_fields", None)
        if fields is None:
            fields = getattr(model_cls, "__fields__")
        return set(fields)

    required_fields = {
        AgentTemplate: {"agent_id", "description", "capability_set", "symbol_set", "default_tool_scope", "success_stats", "staleness_clock", "model_policy_tag"},
        ChildSpec: {"child_id", "role", "instruction", "tool_scope", "model_class", "required_capabilities", "required_permissions", "dependency_ids", "comm_mode", "resume_policy", "init_summary"},
        ToolSpec: {"name", "category_path", "signature", "description", "runtime", "deps", "permissions", "tests", "backgroundable", "state_schema", "source_digest", "build_cmd", "run_cmd", "timeout_s", "determinism_class"},
        SummaryRecord: {"objective", "evidence", "artifacts", "unresolved", "open_handles", "next_actions", "symbols", "verifier_state", "provenance"},
        Checkpoint: {"summary", "artifact_refs", "open_handles", "unresolved_goals", "budget_state", "verifier_state", "resume_constraints"},
        AsyncHandle: {"handle_id", "tool_name", "sandbox_hash", "working_directory", "launch_time", "timeout", "stdout_path", "stderr_path", "state", "artifact_refs"},
        MemoryNode: {"node_id", "type", "label", "content", "embedding", "symbol_set", "file_paths", "source_task_id", "verifier_support", "timestamps", "provenance", "tombstoned"},
        ArchiveEntry: {"code_hash", "runtime_hash", "scores", "behavior_bin", "scope_tag", "complexity_bucket", "mutable_loc", "trace_refs"},
    }
    for model_cls, required in required_fields.items():
        assert required.issubset(field_names(model_cls))


def test_graph_type_contracts_match_spec() -> None:
    assert {item.value for item in NodeType} == {"AgentRun", "Event", "Summary", "Artifact", "RawBlob", "OpenHandle", "VerifierEvidence"}
    assert {item.value for item in EdgeType} == {"CALLS_AGENT", "EMITS", "SUMMARIZES", "PRODUCES", "BACKLINKS_TO", "WAITS_ON", "CONTINUES_FROM", "VALIDATED_BY"}
    assert {item.value for item in LongTermNodeType} == {
        "Symbol",
        "File",
        "Query",
        "Answer",
        "ToolFailure",
        "FixPattern",
        "TaskNote",
        "Procedure",
        "EnvironmentFingerprint",
        "ArtifactSignature",
    }

def test_generated_tool_spec_uses_resolved_dependency_args(runtime_dir: Path, tmp_path: Path) -> None:
    runtime = load_runtime(runtime_dir)
    shell = FixedShell(tmp_path / "shell_resolved_args")
    task = BenchmarkTask(
        task_id="tool.dep_args",
        family="tool",
        prompt="compute total times rate",
        task_type="unit",
        operations=[
            OperationSpec(
                op_id="expr",
                kind="generated_expression",
                output_key="adjusted",
                description="Adjust total by rate",
                expression="total * rate",
                args={},
                dependencies=["total", "rate"],
            )
        ],
        expected=None,
        verifier_type="json_exact",
        allow_best_effort=True,
    )
    context = PolicyContext(
        runtime_dir=runtime.runtime_dir,
        shell=shell,
        task=task,
        provider=LocalDeterministicProvider(),
        seed=0,
        state=RuntimeState(visible_tool_names=sorted(shell.tool_registry.tools)),
        budget=RuntimeBudget(),
        trace=[],
        objective=task.prompt,
    )
    spec, _, executor = runtime.tooling.propose_tool_spec(context, task.operations[0], {"total": 10, "rate": 0.2})
    assert spec.signature == "(rate, total) -> value"
    assert spec.tests == [{"input": {"total": 10, "rate": 0.2}, "expected": 2.0}]
    assert executor(total=10, rate=0.2) == 2.0


def test_generated_expression_runtime_accepts_safe_builtin_named_args(runtime_dir: Path, tmp_path: Path) -> None:
    runtime = load_runtime(runtime_dir)
    shell = FixedShell(tmp_path / "shell_builtin_shadow")
    runner = TaskRuntime(runtime, shell, LocalDeterministicProvider())
    task = BenchmarkTask(
        task_id="tool.safe_builtin_arg",
        family="tool",
        prompt="add the provided sum and x values",
        task_type="unit",
        operations=[
            OperationSpec(
                op_id="expr",
                kind="generated_expression",
                output_key="value",
                description="Add the provided sum and x values",
                expression="sum + x",
                args={"sum": 5, "x": 2},
            )
        ],
        expected=7,
        verifier_type="number_exact",
    )
    result = runner.run_task(task, 0)
    assert result.hard_invalid is False
    assert result.artifact == 7


def test_stage1_smoke_detects_trace_nondeterminism_even_when_trace_path_reused(tmp_path: Path) -> None:
    suite = build_demo_suite()
    evaluator = RuntimeEvaluator(suite, tmp_path / "eval", LocalDeterministicProvider(), baseline_runtime_dir=None)
    shared_trace_path = tmp_path / "shared_trace.json"
    traces = [
        [{"event": "agent_start"}, {"event": "stop"}],
        [{"event": "agent_start"}, {"event": "tool_operation"}, {"event": "stop"}],
    ]

    def fake_evaluate_runtime(runtime_dir, partition="train", seeds=(0, 1, 2), use_cache=True, tasks_override=None):
        trace = traces.pop(0)
        shared_trace_path.write_text(json.dumps(trace), encoding="utf-8")
        return SuiteEvaluation(
            runtime_hash="runtime",
            objective_scores={},
            task_scores={},
            family_scores={},
            run_results=[
                RunResult(
                    task_id="proxy",
                    seed=0,
                    artifact={"ok": True},
                    verifier_score=1.0,
                    cost=0.0,
                    latency=0.0,
                    faults=0,
                    trace_path=str(shared_trace_path),
                    hard_invalid=False,
                    mode="single",
                )
            ],
            invalid=False,
        )

    evaluator.evaluate_runtime = fake_evaluate_runtime  # type: ignore[method-assign]
    stage1 = evaluator.stage1_smoke(tmp_path / "runtime")
    assert stage1.passed is False


def test_stage1_smoke_ignores_volatile_trace_fields(tmp_path: Path) -> None:
    suite = build_demo_suite()
    evaluator = RuntimeEvaluator(suite, tmp_path / "eval", LocalDeterministicProvider(), baseline_runtime_dir=None)
    trace_paths = [tmp_path / "trace_a.json", tmp_path / "trace_b.json"]
    traces = [
        [{"event": "model_response", "latency_s": 0.01, "purpose": "tool_spec"}, {"event": "stop", "verified": True}],
        [{"event": "model_response", "latency_s": 0.25, "purpose": "tool_spec"}, {"event": "stop", "verified": True}],
    ]

    def fake_evaluate_runtime(runtime_dir, partition="train", seeds=(0, 1, 2), use_cache=True, tasks_override=None):
        trace = traces.pop(0)
        trace_path = trace_paths.pop(0)
        trace_path.write_text(json.dumps(trace), encoding="utf-8")
        return SuiteEvaluation(
            runtime_hash="runtime",
            objective_scores={},
            task_scores={},
            family_scores={},
            run_results=[
                RunResult(
                    task_id="proxy",
                    seed=0,
                    artifact={"ok": True},
                    verifier_score=1.0,
                    cost=0.0,
                    latency=0.0,
                    faults=0,
                    trace_path=str(trace_path),
                    hard_invalid=False,
                    mode="single",
                )
            ],
            invalid=False,
        )

    evaluator.evaluate_runtime = fake_evaluate_runtime  # type: ignore[method-assign]
    stage1 = evaluator.stage1_smoke(tmp_path / "runtime")
    assert stage1.passed is True


def test_evaluator_preserves_long_term_within_ordered_episode(runtime_dir: Path, tmp_path: Path) -> None:
    first = BenchmarkTask(
        task_id="episode.first",
        family="mem",
        prompt="Remember the exact symbol EP_RATE.",
        task_type="memory_query",
        symbolic_seeds=["EP_RATE"],
        context_items=[{"symbol": "EP_RATE", "value": 7}],
        operations=[OperationSpec(op_id="seed", kind="memory_lookup", output_key="answer", description="Lookup exact symbol", requires_exact_symbol="EP_RATE")],
        expected="7",
        verifier_type="string_exact",
        transfer_scored=True,
        episode_id="episode",
        episode_order=0,
    )
    second = BenchmarkTask(
        task_id="episode.second",
        family="mem",
        prompt="Reuse the exact symbol EP_RATE from the prior task.",
        task_type="memory_query",
        symbolic_seeds=["EP_RATE"],
        operations=[OperationSpec(op_id="reuse", kind="memory_lookup", output_key="answer", description="Lookup exact symbol", requires_exact_symbol="EP_RATE")],
        expected="7",
        verifier_type="string_exact",
        transfer_scored=True,
        episode_id="episode",
        episode_order=1,
    )
    suite = BenchmarkSuite(name="episode", train=[second, first], val=[], test=[], proxy=[first])
    evaluator = RuntimeEvaluator(suite, tmp_path / "eval_episode", LocalDeterministicProvider(), baseline_runtime_dir=None)
    evaluation = evaluator.evaluate_runtime(runtime_dir, partition="train", seeds=[0], use_cache=False)
    scores = {run.task_id: run.verifier_score for run in evaluation.run_results}
    assert scores["episode.first"] == 1.0
    assert scores["episode.second"] == 1.0


def test_canonical_agent_execution_invalidates() -> None:
    pool = AgentPool()
    canonical = pool.get_canonical("root")
    with pytest.raises(HardInvalidation):
        pool.assert_clone(canonical)



def test_runtime_solves_demo_train_suite(runtime_dir: Path, demo_suite, provider_local, tmp_path: Path) -> None:
    evaluator = RuntimeEvaluator(demo_suite, tmp_path / "eval", provider_local, baseline_runtime_dir=runtime_dir)
    evaluation = evaluator.evaluate_runtime(runtime_dir, partition="train", seeds=[0], use_cache=False)
    assert evaluation.invalid is False
    assert all(run.verifier_score == 1.0 for run in evaluation.run_results)
    assert evaluation.objective_scores["sbar:global"] > 0.9



def test_stage0_rejects_non_unique_search(runtime_dir: Path, demo_suite, provider_local, tmp_path: Path) -> None:
    evaluator = RuntimeEvaluator(demo_suite, tmp_path / "eval", provider_local, baseline_runtime_dir=runtime_dir)
    candidate = MutationCandidate(runtime_dir=str(runtime_dir), patch_text=build_patch("class", "klass"), touched_scope=["top"], prompt="", objective="sbar:top")
    stage0, _ = evaluator.stage0_patch_integrity(runtime_dir, candidate)
    assert stage0.passed is False



def test_scope_scheduler_updates_credit() -> None:
    scheduler = ScopeScheduler()
    initial = scheduler.utility(["tool"], "sbar:tool")
    scheduler.update_scope_credit("sbar:tool", ["tool"], 0.25)
    later = scheduler.utility(["tool"], "sbar:tool")
    assert later > initial



def test_archive_insert_and_select_parent(runtime_dir: Path, demo_suite, provider_local, tmp_path: Path) -> None:
    evaluator = RuntimeEvaluator(demo_suite, tmp_path / "eval", provider_local, baseline_runtime_dir=runtime_dir)
    evaluation = evaluator.evaluate_runtime(runtime_dir, partition="train", seeds=[0], use_cache=False)
    runtime = load_runtime(runtime_dir)
    archive = QualityDiversityArchive()
    inserted = archive.insert(str(runtime_dir), runtime.runtime_hash, runtime.code_hash, runtime.mutable_loc, evaluation, scope=[])
    assert inserted
    record = archive.select_parent("sbar:global", seed=0)
    assert record.entry.runtime_hash == runtime.runtime_hash



def test_task_local_generated_tools_reset_between_tasks(runtime_dir: Path, provider_local, tmp_path: Path) -> None:
    shell = FixedShell(tmp_path / "shell")
    source, executor = validate_expression_tool("a+b", [{"input": {"a": 2, "b": 3}, "expected": 5}], shell.safety_guard)
    spec = ToolSpec(name="generated/local/test123", category_path=["generated", "local"], signature="(a,b)->value", description="test", runtime="python", deps=[], permissions=[], tests=[], backgroundable=False, state_schema={}, source_digest="x", build_cmd="python -m py_compile tool.py", run_cmd="python tool.py", timeout_s=10, determinism_class="stable")
    shell.tool_registry.register_generated_tool(spec, source, executor=executor)
    assert "generated/local/test123" in shell.tool_registry.tools
    shell.reset_for_task("next-task", transfer_scored=False)
    assert "generated/local/test123" not in shell.tool_registry.tools
    assert "generated/local" not in shell.tool_registry.category_summaries


def test_async_tool_handle_completes(runtime_dir: Path, provider_local, tmp_path: Path) -> None:
    shell = FixedShell(tmp_path / "shell_async")
    source, executor = validate_expression_tool("a+b", [{"input": {"a": 2, "b": 3}, "expected": 5}], shell.safety_guard)
    spec = ToolSpec(name="generated/local/async123", category_path=["generated", "local"], signature="(a,b)->value", description="test", runtime="python", deps=[], permissions=[], tests=[], backgroundable=True, state_schema={}, source_digest="x", build_cmd="python -m py_compile tool.py", run_cmd="python tool.py", timeout_s=10, determinism_class="stable")
    shell.tool_registry.register_generated_tool(spec, source, executor=executor)
    handle = shell.tool_executor.launch_async("generated/local/async123", {"a": 2, "b": 3}, tmp_path / "handles", "task_async")
    shell.open_handles.add(handle)
    finished = shell.tool_executor.await_handle(handle.handle_id, shell.open_handles)
    assert finished["state"] == "completed"
    assert shell.open_handles.get(handle.handle_id).state == "completed"
    registered = shell.tool_registry.get("generated/local/async123")
    assert registered.historical_runs == 1
    assert registered.historical_passes == 1
    assert registered.distinct_tasks == {"task_async"}


def test_async_launches_use_distinct_files_and_accumulate_stats(tmp_path: Path) -> None:
    shell = FixedShell(tmp_path / "shell_async_multi")
    source, executor = validate_expression_tool("a+b", [{"input": {"a": 2, "b": 3}, "expected": 5}], shell.safety_guard)
    spec = ToolSpec(name="generated/local/async_multi", category_path=["generated", "local"], signature="(a,b)->value", description="test", runtime="python", deps=[], permissions=[], tests=[], backgroundable=True, state_schema={}, source_digest="x", build_cmd="python -m py_compile tool.py", run_cmd="python tool.py", timeout_s=10, determinism_class="stable")
    shell.tool_registry.register_generated_tool(spec, source, executor=executor)
    first = shell.tool_executor.launch_async("generated/local/async_multi", {"a": 2, "b": 3}, tmp_path / "handles", "task_a")
    second = shell.tool_executor.launch_async("generated/local/async_multi", {"a": 4, "b": 5}, tmp_path / "handles", "task_b")
    assert first.stdout_path != second.stdout_path
    assert first.stderr_path != second.stderr_path
    shell.open_handles.add(first)
    shell.open_handles.add(second)
    first_result = shell.tool_executor.await_handle(first.handle_id, shell.open_handles)
    second_result = shell.tool_executor.await_handle(second.handle_id, shell.open_handles)
    assert first_result["output"] == 5
    assert second_result["output"] == 9
    registered = shell.tool_registry.get("generated/local/async_multi")
    assert registered.historical_runs == 2
    assert registered.historical_passes == 2
    assert registered.distinct_tasks == {"task_a", "task_b"}


def test_wait_async_fails_when_process_handle_is_missing(tmp_path: Path) -> None:
    shell = FixedShell(tmp_path / "shell_missing")
    source, executor = validate_expression_tool("a+b", [{"input": {"a": 2, "b": 3}, "expected": 5}], shell.safety_guard)
    spec = ToolSpec(name="generated/local/missing123", category_path=["generated", "local"], signature="(a,b)->value", description="test", runtime="python", deps=[], permissions=[], tests=[], backgroundable=True, state_schema={}, source_digest="x", build_cmd="python -m py_compile tool.py", run_cmd="python tool.py", timeout_s=10, determinism_class="stable")
    shell.tool_registry.register_generated_tool(spec, source, executor=executor)
    handle = shell.tool_executor.launch_async("generated/local/missing123", {"a": 2, "b": 3}, tmp_path / "handles", "task_missing")
    record = shell.tool_executor._async_processes.pop(handle.handle_id)
    record.process.kill()
    record.process.wait()
    record.stdout_handle.close()
    record.stderr_handle.close()
    result = shell.tool_executor.wait_async(handle)
    assert result.success is False
    assert "missing" in result.stderr


def test_horizontal_workers_do_not_share_runtime_state(runtime_dir: Path, tmp_path: Path) -> None:
    runtime = load_runtime(runtime_dir)
    shell = FixedShell(tmp_path / "shell_worker_isolation")
    runner = TaskRuntime(runtime, shell, LocalDeterministicProvider())
    task = BenchmarkTask(
        task_id="worker.isolation",
        family="top",
        prompt="verify worker isolation",
        task_type="unit",
        operations=[
            OperationSpec(op_id="base", kind="custom", output_key="x", description="base", args={"value": 1}),
            OperationSpec(op_id="dep", kind="custom", output_key="y", description="dependent", dependencies=["base"], args={}),
        ],
        expected={},
        verifier_type="json_exact",
        allow_best_effort=True,
    )
    context = PolicyContext(
        runtime_dir=runtime.runtime_dir,
        shell=shell,
        task=task,
        provider=LocalDeterministicProvider(),
        seed=0,
        state=RuntimeState(visible_tool_names=sorted(shell.tool_registry.tools)),
        budget=RuntimeBudget(),
        trace=[],
        objective=task.prompt,
    )
    first_frame = AgentFrame(agent=shell.agent_pool.clone("root"), objective="worker1", operation_ids=["base"], depth=1, role="worker")
    second_frame = AgentFrame(agent=shell.agent_pool.clone("root"), objective="worker2", operation_ids=["dep"], depth=1, role="worker")
    first_output, _, _ = runner._execute_isolated_frame(context, first_frame, [task.operations[0]], isolate_runtime_state=True)
    second_output, _, _ = runner._execute_isolated_frame(context, second_frame, [task.operations[1]], isolate_runtime_state=True)
    assert first_output == {"value": 1}
    assert second_output == {}
    assert context.state.artifacts == {}


def test_generated_tool_materializes_inside_content_addressed_sandbox(tmp_path: Path) -> None:
    shell = FixedShell(tmp_path / "shell_long_name")
    source, executor = validate_expression_tool("a+b", [{"input": {"a": 2, "b": 3}, "expected": 5}], shell.safety_guard)
    long_name = "generated/local/" + "_".join(["verylongsegment"] * 12)
    spec = ToolSpec(name=long_name, category_path=["generated", "local"], signature="(a,b)->value", description="test", runtime="python", deps=[], permissions=[], tests=[], backgroundable=False, state_schema={}, source_digest="x", build_cmd="python -m py_compile tool.py", run_cmd="python tool.py", timeout_s=10, determinism_class="stable")
    registered = shell.tool_registry.register_generated_tool(spec, source, executor=executor)
    sandbox_dir = shell.sandbox_manager.ensure_environment(registered.spec)
    tool_files = list(sandbox_dir.glob("*.py"))
    assert len(tool_files) == 1
    assert tool_files[0].parent == sandbox_dir
    assert tool_files[0].read_text(encoding="utf-8") == source
    assert registered.sandbox_hash == shell.sandbox_manager.sandbox_hash(registered.spec)


def _run_background_tool_task(runtime_dir: Path, tmp_path: Path, tool_name: str, source: str) -> RunResult:
    runtime = load_runtime(runtime_dir)
    shell = FixedShell(tmp_path / tool_name.replace("/", "_"))
    runner = TaskRuntime(runtime, shell, LocalDeterministicProvider())
    spec = ToolSpec(name=tool_name, category_path=["custom", "local"], signature="()->value", description=tool_name, runtime="python", deps=[], permissions=[], tests=[], backgroundable=True, state_schema={}, source_digest="x", build_cmd="python -m py_compile tool.py", run_cmd="python tool.py", timeout_s=10, determinism_class="stable")
    shell.tool_registry.register_generated_tool(spec, source)
    task = BenchmarkTask(task_id=f"{tool_name}.task", family="tool", task_type="unit", prompt=f"run {tool_name}", operations=[OperationSpec(op_id="op", kind="builtin", output_key="out", description=f"run {tool_name}", tool_hint=tool_name, args={})], expected=None, verifier_type="json_exact", allow_best_effort=True)
    return runner.run_task(task, 0)


def test_async_tool_nonzero_exit_invalidates_run(runtime_dir: Path, tmp_path: Path) -> None:
    result = _run_background_tool_task(runtime_dir, tmp_path, "custom/local/exit1", "def run(**kwargs):\n    raise SystemExit(1)\n")
    assert result.hard_invalid is True
    assert "process exited with code 1" in (result.invalid_reason or "")


def test_async_tool_stdout_noise_returns_controlled_failure(runtime_dir: Path, tmp_path: Path) -> None:
    result = _run_background_tool_task(runtime_dir, tmp_path, "custom/local/noise1", "def run(**kwargs):\n    print('noise')\n    return 1\n")
    assert result.hard_invalid is True
    assert "tool execution failed" in (result.invalid_reason or "")


def test_provider_tool_spec_fallback_handles_non_json(runtime_dir: Path, tmp_path: Path) -> None:
    class BadJsonProvider(LocalDeterministicProvider):
        def generate(self, request: ModelRequest) -> ModelResponse:
            return ModelResponse(text="not json", model_name="bad")

    runtime = load_runtime(runtime_dir)
    shell = FixedShell(tmp_path / "shell_bad_json")
    runner = TaskRuntime(runtime, shell, BadJsonProvider())
    task = build_demo_suite().by_id("proxy.tool.provider_synthesis")
    result = runner.run_task(task, 0)
    assert result.hard_invalid is False
    assert result.artifact == 7


def test_openai_provider_preserves_empty_string_output() -> None:
    class FakeResponse:
        output_text = ""
        usage = None
        id = "resp_1"
        status = "completed"

        def __str__(self) -> str:
            return "<fallback-response>"

    class FakeClient:
        class responses:
            @staticmethod
            def create(**kwargs):
                return FakeResponse()

    provider = OpenAIProvider(api_key="sk-test")
    provider._client = FakeClient()
    response = provider.generate(ModelRequest(instructions="", prompt="", model_class="small", seed=0, metadata={}))
    assert response.text == ""
    assert response.output_tokens == 0


def test_openai_provider_uses_expected_default_models_and_reasoning() -> None:
    observed: list[dict[str, object]] = []

    class FakeResponse:
        output_text = "hi"
        usage = None
        id = "resp_defaults"
        status = "completed"

    class FakeResponses:
        @staticmethod
        def create(**kwargs):
            observed.append(dict(kwargs))
            return FakeResponse()

    class FakeClient:
        responses = FakeResponses()

    provider = OpenAIProvider(api_key="sk-test")
    provider._client = FakeClient()

    large = provider.generate(ModelRequest(instructions="system", prompt="hello", model_class="large", seed=0, metadata={"max_output_tokens": 5}))
    medium = provider.generate(ModelRequest(instructions="", prompt="hello", model_class="medium", seed=0, metadata={}))
    small = provider.generate(ModelRequest(instructions="", prompt="hello", model_class="small", seed=0, metadata={}))

    assert large.text == "hi"
    assert medium.text == "hi"
    assert small.text == "hi"
    assert observed[0]["model"] == "gpt-5.4"
    assert observed[0]["reasoning"] == {"effort": "medium"}
    assert observed[0]["max_output_tokens"] == 5
    assert observed[1]["model"] == "gpt-5.4"
    assert observed[1]["reasoning"] == {"effort": "none"}
    assert observed[2]["model"] == "gpt-5-nano"
    assert observed[2]["reasoning"] == {"effort": "none"}


def test_build_provider_forwards_reasoning_effort_from_profile() -> None:
    provider = build_provider(
        "openai",
        provider_profile=HostedProviderProfile(
            name="openai",
            reasoning_effort_map={"large": "none", "small": "medium"},
        ),
        api_key="sk-test",
    )

    assert isinstance(provider, OpenAIProvider)
    assert provider.reasoning_effort_map["large"] == "none"
    assert provider.reasoning_effort_map["small"] == "medium"


def test_minimax_provider_uses_separate_runtime_environment_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGINTOR_MAS_MINIMAX_API_KEY", "sk-test")
    provider = build_provider("minimax")
    assert isinstance(provider, MiniMaxProvider)
    assert provider.base_url == "https://api.minimax.io/anthropic"
    assert provider.resolve_model("small") == "MiniMax-M2.5"


def test_minimax_provider_uses_anthropic_messages_endpoint() -> None:
    observed: list[dict[str, object]] = []

    class FakeTextBlock:
        type = "text"
        text = "hi"

    class FakeResponse:
        id = "msg_1"
        content = [FakeTextBlock()]
        stop_reason = "end_turn"
        usage = type("Usage", (), {"input_tokens": 7, "output_tokens": 2})()

    class FakeMessages:
        @staticmethod
        def create(**kwargs):
            observed.append(dict(kwargs))
            return FakeResponse()

    class FakeClient:
        messages = FakeMessages()

    provider = MiniMaxProvider(api_key="test-key")
    provider._client = FakeClient()

    response = provider.generate(ModelRequest(instructions="system", prompt="hello", model_class="medium", seed=0, metadata={"max_output_tokens": 5}))

    assert response.text == "hi"
    assert observed[0]["model"] == "MiniMax-M2.5"
    assert observed[0]["max_tokens"] == 5
    assert observed[0]["system"] == "system"
    assert observed[0]["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
    ]
    assert response.input_tokens == 7
    assert response.output_tokens == 2


def test_docker_executor_mounts_key_file_from_provider_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime_dir = init_runtime(tmp_path / "runtime")
    executor = DockerRuntimeExecutor(tmp_path / "docker_ws", repo_root=Path.cwd())
    key_file = tmp_path / "minimax.key"
    key_file.write_text("sk-test", encoding="utf-8")
    monkeypatch.setenv("AGINTOR_MAS_MINIMAX_KEY_FILE", str(key_file))
    monkeypatch.setattr(DockerRuntimeExecutor, "ensure_image", lambda self: None)
    captured_commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        captured_commands.append(list(command))
        output_mount = None
        for index, item in enumerate(command):
            if item == "-v":
                mount = str(command[index + 1])
                if mount.endswith(":/mnt/output"):
                    output_mount = mount.split(":/mnt/output", 1)[0]
                    break
        assert output_mount is not None
        output_path = Path(output_mount) / "run_result.json"
        output_path.write_text(
            json.dumps(
                [
                    {
                        "task_id": "top.sum_product",
                        "seed": 0,
                        "artifact": {"sum": 10, "product": 30},
                        "verifier_score": 1.0,
                        "cost": 0.0,
                        "latency": 0.0,
                        "faults": 0,
                        "trace_path": str(tmp_path / "trace.json"),
                    }
                ]
            ),
            encoding="utf-8",
        )

        class Completed:
            returncode = 0
            stdout = ""
            stderr = ""

        return Completed()

    monkeypatch.setattr(container_runtime_module.subprocess, "run", fake_run)

    results = executor.run_unit(
        runtime_dir,
        [build_demo_suite().train[0]],
        0,
        provider_name="minimax",
    )

    assert results[0].task_id == "top.sum_product"
    mounts: list[str] = []
    for command in captured_commands:
        for index, item in enumerate(command):
            if item == "-v":
                mounts.append(str(command[index + 1]))
    assert any(str(key_file.resolve()) in mount and "/mnt/keys/provider_api_key.txt:ro" in mount for mount in mounts)


def test_open_handle_table_rejects_incomplete_handles(tmp_path: Path) -> None:
    shell = FixedShell(tmp_path / "shell_invalid_handles")
    shell.open_handles.add(
        AsyncHandle(
            handle_id="h1",
            tool_name="generated/local/test",
            sandbox_hash="hash",
            working_directory="wd",
            launch_time=0.0,
            timeout=10.0,
            stdout_path="stdout.log",
            stderr_path="",
            state="running",
            artifact_refs=[],
        )
    )
    with pytest.raises(HardInvalidation):
        shell.open_handles.validate()
