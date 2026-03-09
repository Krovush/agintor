from __future__ import annotations

import json
from pathlib import Path

import pytest

from agintor.archive import QualityDiversityArchive, ScopeScheduler
from agintor.benchmarks import build_demo_suite
from agintor.evaluator import RuntimeEvaluator
from agintor.exceptions import HardInvalidation, PatchApplyError
from agintor.memory_graph import LongTermGraph, ShortTermGraph
from agintor.patches import apply_patch_to_text, build_patch
from agintor.project import init_runtime
from agintor.runtime_loader import load_runtime
from agintor.schemas import MemoryNode, MutationCandidate, SummaryRecord
from agintor.shell import AgentPool, FixedShell
from agintor.tool_runtime import SafetyGuard, SandboxManager, ToolExecutor, ToolRegistry, validate_expression_tool


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
    from agintor.schemas import ToolSpec
    spec = ToolSpec(name="generated/local/test123", category_path=["generated", "local"], signature="(a,b)->value", description="test", runtime="python", deps=[], permissions=[], tests=[], backgroundable=False, state_schema={}, source_digest="x", build_cmd="python -m py_compile tool.py", run_cmd="python tool.py", timeout_s=10, determinism_class="stable")
    shell.tool_registry.register_generated_tool(spec, source, executor=executor)
    assert "generated/local/test123" in shell.tool_registry.tools
    shell.reset_for_task(transfer_scored=False)
    assert "generated/local/test123" not in shell.tool_registry.tools
