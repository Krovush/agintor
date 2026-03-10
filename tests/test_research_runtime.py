from __future__ import annotations

from pathlib import Path

from agintor.benchmarks import BenchmarkSuite, build_research_suite
from agintor.evaluator import RuntimeEvaluator
from agintor.project import init_runtime
from agintor.prompts import load_prompt_spec
from agintor.providers import LocalDeterministicProvider
from agintor.research_runtime import build_research_task
from agintor.runner import TaskRuntime
from agintor.runtime_api import PolicyContext, RuntimeBudget, RuntimeState
from agintor.runtime_loader import load_runtime
from agintor.schemas import BenchmarkTask, OperationSpec, ToolSpec
from agintor.shell import FixedShell
from agintor.tool_runtime import RegisteredTool


def test_prompt_registry_loads_research_and_mutation_prompts() -> None:
    assert load_prompt_spec("evolve.mutator_patch.v1").prompt_id == "evolve.mutator_patch.v1"
    assert load_prompt_spec("runtime.final_synthesis.v1").allowed_tools == []
    assert "web_search" in load_prompt_spec("runtime.source_extract.v1").allowed_tools


def test_research_suite_runs_offline_with_runtime_backed_path(tmp_path: Path) -> None:
    runtime_dir = init_runtime(tmp_path / "runtime")
    suite = build_research_suite()
    evaluator = RuntimeEvaluator(suite, tmp_path / "eval", LocalDeterministicProvider(), baseline_runtime_dir=None)
    task = suite.by_id("e2e.research.blueprint")
    evaluation = evaluator.evaluate_runtime(runtime_dir, partition="train", seeds=[0], use_cache=False, tasks_override=[task])
    run = evaluation.run_results[0]
    assert run.hard_invalid is False
    assert run.verifier_score >= 0.95
    assert isinstance(run.artifact, dict)
    assert len(run.artifact["sources"]) >= task.min_source_count
    assert "## Architecture" in run.artifact["answer_markdown"]


def test_stage4_full_preserves_transfer_scored_episodes_across_minibatches(tmp_path: Path) -> None:
    episode_tasks = []
    for index in range(5):
        episode_tasks.append(
            BenchmarkTask(
                task_id=f"mem.episode.{index}",
                family="mem",
                prompt="What value does EP_ALPHA map to?",
                task_type="memory_query",
                symbolic_seeds=["EP_ALPHA"],
                context_items=[{"symbol": "EP_ALPHA", "value": 17}] if index == 0 else [],
                operations=[
                    OperationSpec(
                        op_id="lookup",
                        kind="memory_lookup",
                        output_key="answer",
                        description="Lookup exact symbol value",
                        requires_exact_symbol="EP_ALPHA",
                    )
                ],
                expected="17",
                verifier_type="string_exact",
                transfer_scored=True,
                episode_id="episode-1",
                episode_order=index,
            )
        )
    suite = BenchmarkSuite(name="episode_regression", train=episode_tasks, val=[], test=[], proxy=[])
    runtime_dir = init_runtime(tmp_path / "runtime_episode")
    evaluator = RuntimeEvaluator(suite, tmp_path / "eval_episode", LocalDeterministicProvider(), baseline_runtime_dir=None)
    evaluator.stage4_minibatch_size = 4
    stage4 = evaluator.stage4_full(runtime_dir, runtime_dir)
    assert stage4.passed is True
    assert stage4.suite_evaluation is not None
    assert stage4.suite_evaluation.invalid is False


def test_hinted_tool_requires_exact_parameter_names_before_selection(tmp_path: Path) -> None:
    runtime_dir = init_runtime(tmp_path / "runtime_hint")
    runtime = load_runtime(runtime_dir)
    shell = FixedShell(tmp_path / "shell_hint")
    runner = TaskRuntime(runtime, shell, LocalDeterministicProvider())

    bad_hint = ToolSpec(
        name="custom/local/by_region",
        category_path=["custom", "local"],
        signature="(region_id) -> value",
        description="lookup by region id",
        runtime="python",
        deps=[],
        permissions=[],
        tests=[],
        backgroundable=False,
        state_schema={},
        source_digest="bad_hint",
        build_cmd="python -m py_compile tool.py",
        run_cmd="python tool.py",
        timeout_s=10,
        determinism_class="stable",
    )
    fallback = ToolSpec(
        name="custom/local/by_id",
        category_path=["custom", "local"],
        signature="(id) -> value",
        description="lookup by exact id",
        runtime="python",
        deps=[],
        permissions=[],
        tests=[],
        backgroundable=False,
        state_schema={},
        source_digest="fallback_hint",
        build_cmd="python -m py_compile tool.py",
        run_cmd="python tool.py",
        timeout_s=10,
        determinism_class="stable",
    )
    shell.tool_registry._tools[bad_hint.name] = RegisteredTool(spec=bad_hint, executor=lambda region_id: region_id, safety_validated=True)
    shell.tool_registry._tools[fallback.name] = RegisteredTool(spec=fallback, executor=lambda id: id, safety_validated=True)
    shell.tool_registry._category_summaries["custom/local"] = "custom lookups"

    task = BenchmarkTask(
        task_id="tool.hinted.signature",
        family="tool",
        prompt="Look up the exact id.",
        task_type="unit",
        operations=[
            OperationSpec(
                op_id="lookup",
                kind="builtin",
                output_key="value",
                description="lookup by exact id",
                tool_hint=bad_hint.name,
                args={"id": 7},
            )
        ],
        expected=7,
        verifier_type="number_exact",
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
    runtime.tooling.rank_categories = lambda ctx, operation, summaries: ["custom/local"]  # type: ignore[method-assign]
    runtime.tooling.rank_tools = lambda ctx, operation, candidate_tools: [fallback.name, bad_hint.name]  # type: ignore[method-assign]
    output, tool_name, created_tool, faults = runner._execute_tool_operation(context, task.operations[0], {"id": 7}, None)
    assert output == 7
    assert tool_name == fallback.name
    assert created_tool is False
    assert faults == 0


def test_async_executor_backed_tool_without_source_materializes_thread_fallback(tmp_path: Path) -> None:
    shell = FixedShell(tmp_path / "shell_async_executor")
    spec = ToolSpec(
        name="generated/local/inmem_async",
        category_path=["generated", "local"],
        signature="(a,b) -> value",
        description="in-memory async tool",
        runtime="python",
        deps=[],
        permissions=[],
        tests=[],
        backgroundable=True,
        state_schema={},
        source_digest="inmem_async",
        build_cmd="python -m py_compile tool.py",
        run_cmd="python tool.py",
        timeout_s=10,
        determinism_class="stable",
    )
    shell.tool_registry._tools[spec.name] = RegisteredTool(
        spec=spec,
        executor=lambda a, b: a + b,
        sandbox_hash=shell.sandbox_manager.sandbox_hash(spec),
        safety_validated=True,
    )
    handle = shell.tool_executor.launch_async(spec.name, {"a": 2, "b": 3}, tmp_path / "handles", "task_async")
    shell.open_handles.add(handle)
    finished = shell.tool_executor.await_handle(handle.handle_id, shell.open_handles)
    assert finished["state"] == "completed"
    assert finished["output"] == 5
