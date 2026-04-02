from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from agintor.archive import QualityDiversityArchive, ScopeScheduler, behavior_descriptor, interface_bitmask, objective_specs_from_suite
from agintor.benchmarks import build_demo_suite
from agintor.evaluator import RuntimeEvaluator
from agintor.exceptions import ValidationError
from agintor.patches import build_patch
from agintor.predictors import DecisionFamilyModelBank, Ensemble
from agintor.prompt_builder import build_mutation_prompt
from agintor.project import init_runtime
from agintor.providers import LocalDeterministicProvider
from agintor.runtime_api import AgentFrame, PolicyContext, RuntimeBudget, RuntimeState
from agintor.runtime_profile import load_runtime_profile
from agintor.runtime_loader import load_runtime
from agintor.runner import TaskRuntime
from agintor.scoring import ScoreCalculator, estimate_reference_scales, mean_improvement
from agintor.schemas import (
    BenchmarkTask,
    ChildSpec,
    EvaluationStageResult,
    MemoryNode,
    ModelRequest,
    MutationCandidate,
    ObjectiveKind,
    ObjectiveSpec,
    OperationSpec,
    RunResult,
    SuiteEvaluation,
    ToolSpec,
)
from agintor.shell import FixedShell
from agintor.tool_runtime import RegisteredTool, SandboxManager
from agintor.verifiers import run_checker, verify_task_with_evidence

pytestmark = pytest.mark.usefixtures("module_failure_artifact_bucket")


def _run_result(task_id: str, verifier_score: float, family_mode: str = "single", *, cost: float = 0.0, latency: float = 0.0, faults: int = 0, created_tools: int = 0, promoted_nodes: int = 0, checks_used: int = 0) -> RunResult:
    return RunResult(
        task_id=task_id,
        seed=0,
        artifact={"task_id": task_id},
        verifier_score=verifier_score,
        cost=cost,
        latency=latency,
        faults=faults,
        trace_path=f"{task_id}.json",
        hard_invalid=False,
        mode=family_mode,
        created_tools=created_tools,
        promoted_nodes=promoted_nodes,
        checks_used=checks_used,
    )


def _suite_evaluation(runtime_hash: str, objective_scores: dict[str, float], run_results: list[RunResult]) -> SuiteEvaluation:
    return SuiteEvaluation(
        runtime_hash=runtime_hash,
        objective_scores=objective_scores,
        task_scores={},
        family_scores={},
        run_results=run_results,
        invalid=False,
    )


def _make_context(runtime_dir: Path, tmp_path: Path, task: BenchmarkTask, *, provider: LocalDeterministicProvider | None = None) -> tuple[Any, FixedShell, PolicyContext]:
    runtime = load_runtime(runtime_dir)
    shell = FixedShell(tmp_path / task.task_id.replace("/", "_"))
    profile = load_runtime_profile(runtime.runtime_dir)
    context = PolicyContext(
        runtime_dir=runtime.runtime_dir,
        shell=shell,
        task=task,
        provider=provider or LocalDeterministicProvider(),
        profile=profile,
        seed=0,
        state=RuntimeState(visible_tool_names=sorted(shell.tool_registry.tools)),
        budget=RuntimeBudget(),
        trace=[],
        objective=task.prompt,
    )
    return runtime, shell, context


class _ConstantProbabilityModel:
    def __init__(self, value: float) -> None:
        self.value = value

    def predict(self, x: Any) -> float:
        return self.value


class _ConstantPositiveModel:
    def __init__(self, value: float) -> None:
        self.value = value

    def predict(self, x: Any) -> float:
        return self.value


def test_load_runtime_does_not_rewrite_runtime_sources(runtime_dir: Path) -> None:
    before = {
        path.name: path.read_text(encoding="utf-8")
        for path in runtime_dir.glob("*.py")
    }
    manifest_before = (runtime_dir / "runtime_manifest.json").read_text(encoding="utf-8")
    load_runtime(runtime_dir)
    after = {
        path.name: path.read_text(encoding="utf-8")
        for path in runtime_dir.glob("*.py")
    }
    manifest_after = (runtime_dir / "runtime_manifest.json").read_text(encoding="utf-8")
    assert after == before
    assert manifest_after == manifest_before


def test_score_calculator_computes_robust_task_metrics() -> None:
    calculator = ScoreCalculator(
        baseline_costs={"top.task": 10.0},
        baseline_latencies={"top.task": 20.0},
        prior_variances={"top": 0.09},
    )
    runs = [
        _run_result("top.task", 1.0, cost=10.0, latency=20.0),
        _run_result("top.task", 0.8, cost=5.0, latency=10.0),
        _run_result("top.task", 0.2, cost=20.0, latency=30.0, faults=1),
    ]
    score = calculator.task_score("top", runs)
    utilities = [
        1.0 - 0.08 * math.log1p(10.0 / 10.0) - 0.05 * math.log1p(20.0 / 20.0),
        0.8 - 0.08 * math.log1p(5.0 / 10.0) - 0.05 * math.log1p(10.0 / 20.0),
        0.2 - 0.08 * math.log1p(20.0 / 10.0) - 0.05 * math.log1p(30.0 / 20.0) - 0.12,
    ]
    mean_utility = sum(utilities) / len(utilities)
    sample_var = sum((value - mean_utility) ** 2 for value in utilities) / len(utilities)
    sigma2_hat = (1.0 - 0.35) * sample_var + 0.35 * 0.09
    sigma_hat = math.sqrt(max(1e-12, sigma2_hat))
    expected_rho = mean_utility - 0.25 * sigma_hat - 0.30 * sigma_hat / math.sqrt(len(utilities))
    assert score.utilities == pytest.approx(utilities)
    assert [run.utility for run in runs] == pytest.approx(utilities)
    assert score.s == pytest.approx(mean_utility)
    assert score.rho == pytest.approx(expected_rho)
    assert score.cvar == pytest.approx(min(utilities))


def test_suite_score_uses_family_weights_instead_of_task_count() -> None:
    calculator = ScoreCalculator(
        family_weights={"top": 0.5, "mem": 0.5, "tool": 0.0, "e2e": 0.0},
    )
    runs = [
        _run_result("top.a", 1.0),
        _run_result("top.b", 0.0),
        _run_result("mem.a", 0.2),
    ]
    evaluation = calculator.suite_score(
        "runtime",
        {"top.a": "top", "top.b": "top", "mem.a": "mem"},
        runs,
    )
    assert evaluation.family_scores["top"]["s"] == pytest.approx(0.5)
    assert evaluation.family_scores["mem"]["s"] == pytest.approx(0.2)
    assert evaluation.objective_scores["sbar:global"] == pytest.approx(0.35)


def test_scope_scheduler_pairwise_credit_increases_joint_scope_utility() -> None:
    singleton_only = ScopeScheduler()
    baseline = singleton_only.utility(["top", "tool"], "sbar:global")
    singleton_only.update_counterfactuals(["top", "tool"], {"top": 1.0, "tool": 1.0}, {})
    utility_without_pair = singleton_only.utility(["top", "tool"], "sbar:global")

    with_pair = ScopeScheduler()
    with_pair.update_counterfactuals(["top", "tool"], {"top": 1.0, "tool": 1.0}, {("top", "tool"): 1.0})
    utility_with_pair = with_pair.utility(["top", "tool"], "sbar:global")

    assert utility_without_pair > baseline
    assert utility_with_pair > utility_without_pair


def test_quality_diversity_archive_prefers_lower_complexity_inside_delta() -> None:
    archive = QualityDiversityArchive(delta_f=0.002)
    evaluation = _suite_evaluation(
        "parent",
        {"sbar:global": 1.0},
        [_run_result("top.sum_product", 1.0, family_mode="vertical", created_tools=1, checks_used=1)],
    )
    replacement = _suite_evaluation(
        "child",
        {"sbar:global": 1.001},
        [_run_result("top.sum_product", 1.0, family_mode="vertical", created_tools=1, checks_used=1)],
    )
    archive.insert("parent_dir", "parent", "code_parent", 100, evaluation, scope=["tool"])
    archive.insert("child_dir", "child", "code_child", 90, replacement, scope=["tool"])
    island = archive.island("sbar:global")
    assert len(island) == 1
    assert island[0].entry.runtime_hash == "child"


def test_archive_complexity_bucket_uses_mutable_ast_nodes_over_loc() -> None:
    archive = QualityDiversityArchive()
    evaluation = _suite_evaluation("baseline", {"sbar:global": 1.0}, [_run_result("top.sum_product", 1.0)])
    archive.insert("baseline_dir", "baseline", "code_baseline", 100, evaluation, scope=["top"], mutable_ast_nodes=10)
    archive.insert("child_dir", "child", "code_child", 1, evaluation, scope=["top"], mutable_ast_nodes=100)
    assert archive.runtime_descriptors["baseline"].complexity_bucket == 0
    assert archive.runtime_descriptors["child"].complexity_bucket > archive.runtime_descriptors["baseline"].complexity_bucket


def test_model_bank_retrain_gate_is_thresholded() -> None:
    bank = DecisionFamilyModelBank(ensemble_size=2)
    for idx in range(6):
        label = float(idx % 2 == 0)
        bank.add_observation("mode", [float(idx), 1.0], probability_label=label)
    bank.maybe_retrain(49, 9)
    assert "mode" not in bank._models
    bank.maybe_retrain(50, 0)
    assert "mode" in bank._models


def test_model_bank_utility_builds_conservative_and_optimistic_bounds() -> None:
    bank = DecisionFamilyModelBank()
    bank._models["mode"] = Ensemble(probability_models=[_ConstantProbabilityModel(0.60), _ConstantProbabilityModel(0.80)])
    bank._models["mode:token"] = Ensemble(positive_models=[_ConstantPositiveModel(2.0), _ConstantPositiveModel(4.0)])
    bank._models["mode:latency"] = Ensemble(positive_models=[_ConstantPositiveModel(1.0), _ConstantPositiveModel(3.0)])
    bank._models["mode:fault"] = Ensemble(probability_models=[_ConstantProbabilityModel(0.10), _ConstantProbabilityModel(0.30)])
    utility, conservative, optimistic = bank.utility("mode", [1.0, 0.0], token_ref=1.0, latency_ref=1.0, beta=1.5, aux_value=0.2)
    assert conservative < utility < optimistic
    assert utility < 0.70


def test_stage0_patch_integrity_enforces_block_and_line_limits(runtime_dir: Path, tmp_path: Path) -> None:
    evaluator = RuntimeEvaluator(build_demo_suite(), tmp_path / "eval", LocalDeterministicProvider(), baseline_runtime_dir=None)
    too_many_blocks = "\n".join(build_patch(f"search-{idx}", f"replace-{idx}") for idx in range(5))
    candidate = MutationCandidate(runtime_dir=str(runtime_dir), patch_text=too_many_blocks, touched_scope=["top"], prompt="", objective="sbar:top")
    stage0, _ = evaluator.stage0_patch_integrity(runtime_dir, candidate)
    assert stage0.passed is False
    assert "max block" in stage0.reason

    search = "\n".join(f"old_{idx}" for idx in range(61))
    replace = "\n".join(f"new_{idx}" for idx in range(61))
    oversized = MutationCandidate(runtime_dir=str(runtime_dir), patch_text=build_patch(search, replace), touched_scope=["top"], prompt="", objective="sbar:top")
    stage0_oversized, _ = evaluator.stage0_patch_integrity(runtime_dir, oversized)
    assert stage0_oversized.passed is False
    assert "max changed" in stage0_oversized.reason


def test_stage0_rejects_search_blocks_longer_than_eight_lines(runtime_dir: Path, tmp_path: Path) -> None:
    evaluator = RuntimeEvaluator(build_demo_suite(), tmp_path / "eval_lines", LocalDeterministicProvider(), baseline_runtime_dir=None)
    search = "\n".join(f"line_{idx}" for idx in range(9))
    replace = "\n".join(f"line_new_{idx}" for idx in range(9))
    candidate = MutationCandidate(runtime_dir=str(runtime_dir), patch_text=build_patch(search, replace), touched_scope=["top"], prompt="", objective="sbar:top")
    stage0, _ = evaluator.stage0_patch_integrity(runtime_dir, candidate)
    assert stage0.passed is False
    assert "8 lines" in stage0.reason


def test_stage0_rejects_patches_that_only_match_immutable_shell_files(runtime_dir: Path, tmp_path: Path) -> None:
    evaluator = RuntimeEvaluator(build_demo_suite(), tmp_path / "eval_shell", LocalDeterministicProvider(), baseline_runtime_dir=None)
    candidate = MutationCandidate(
        runtime_dir=str(runtime_dir),
        patch_text=build_patch("class FixedShell:", "class FixedShellShadow:"),
        touched_scope=["ctl"],
        prompt="",
        objective="sbar:global",
    )
    stage0, _ = evaluator.stage0_patch_integrity(runtime_dir, candidate)
    assert stage0.passed is False
    assert "exactly one mutable file" in stage0.reason


def test_stage0_rejects_changes_outside_mutable_method_contracts(runtime_dir: Path, tmp_path: Path) -> None:
    evaluator = RuntimeEvaluator(build_demo_suite(), tmp_path / "eval_boundaries", LocalDeterministicProvider(), baseline_runtime_dir=None)
    candidate = MutationCandidate(
        runtime_dir=str(runtime_dir),
        patch_text=build_patch("    THETA_CREATE = 0.58", "    THETA_CREATE = 0.52"),
        touched_scope=["top"],
        prompt="",
        objective="sbar:top",
    )
    stage0, _ = evaluator.stage0_patch_integrity(runtime_dir, candidate)
    assert stage0.passed is False
    assert "mutable method" in stage0.reason


def test_stage0_rejects_replacements_that_escape_allowed_method(runtime_dir: Path, tmp_path: Path) -> None:
    evaluator = RuntimeEvaluator(build_demo_suite(), tmp_path / "eval_escape", LocalDeterministicProvider(), baseline_runtime_dir=None)
    search = (
        "            scored.append((score, tool.spec.name))\n"
        "        return [name for _, name in sorted(scored, key=lambda item: (-item[0], item[1]))]"
    )
    replace = (
        "            scored.append((score, tool.spec.name))\n"
        "        return [name for _, name in sorted(scored, key=lambda item: (-item[0], item[1]))]\n"
        "\n"
        "def escaped_helper():\n"
        "    return 'bad'"
    )
    candidate = MutationCandidate(
        runtime_dir=str(runtime_dir),
        patch_text=build_patch(search, replace),
        touched_scope=["tool"],
        prompt="",
        objective="sbar:tool",
    )
    stage0, _ = evaluator.stage0_patch_integrity(runtime_dir, candidate)
    assert stage0.passed is False
    assert "mutable method" in stage0.reason


def test_staged_evaluate_short_circuits_after_first_failed_stage(tmp_path: Path) -> None:
    evaluator = RuntimeEvaluator(build_demo_suite(), tmp_path / "eval", LocalDeterministicProvider(), baseline_runtime_dir=None)
    objective = ObjectiveSpec(name="sbar:global", kind=ObjectiveKind.GLOBAL)
    candidate = MutationCandidate(runtime_dir=str(tmp_path / "runtime"), patch_text="", touched_scope=["tool"], prompt="", objective=objective.name)

    stage1_calls: list[int] = []

    def stage0_ok(parent_dir: Path, mutation: MutationCandidate) -> tuple[EvaluationStageResult, Path]:
        stage1_calls.append(0)
        return EvaluationStageResult(stage=0, passed=True, reason="ok"), tmp_path / "child"

    def stage1_fail(child_dir: Path) -> EvaluationStageResult:
        stage1_calls.append(1)
        return EvaluationStageResult(stage=1, passed=False, reason="stop")

    evaluator.stage0_patch_integrity = stage0_ok  # type: ignore[method-assign]
    evaluator.stage1_smoke = stage1_fail  # type: ignore[method-assign]
    evaluator.stage2_proxy = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("stage2 should not run"))  # type: ignore[method-assign]
    evaluator.staged_evaluate(tmp_path / "parent", candidate, objective)
    assert stage1_calls == [0, 1]

    evaluator = RuntimeEvaluator(build_demo_suite(), tmp_path / "eval2", LocalDeterministicProvider(), baseline_runtime_dir=None)
    stage2_calls: list[int] = []
    evaluator.stage0_patch_integrity = lambda parent_dir, mutation: (EvaluationStageResult(stage=0, passed=True, reason="ok"), tmp_path / "child2")  # type: ignore[method-assign]
    evaluator.stage1_smoke = lambda child_dir: EvaluationStageResult(stage=1, passed=True, reason="ok")  # type: ignore[method-assign]

    def stage2_fail(parent_dir: Path, child_dir: Path, scope: list[str], epsilon_proxy: float = 0.01) -> EvaluationStageResult:
        stage2_calls.append(2)
        return EvaluationStageResult(stage=2, passed=False, reason="stop")

    evaluator.stage2_proxy = stage2_fail  # type: ignore[method-assign]
    evaluator.stage3_local_subset = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("stage3 should not run"))  # type: ignore[method-assign]
    evaluator.staged_evaluate(tmp_path / "parent", candidate, objective)
    assert stage2_calls == [2]


def test_evaluator_objective_subset_matches_spec_shape(tmp_path: Path) -> None:
    suite = build_demo_suite()
    evaluator = RuntimeEvaluator(suite, tmp_path / "eval", LocalDeterministicProvider(), baseline_runtime_dir=None)
    single = evaluator._objective_subset(ObjectiveSpec(name="s:tool.generated_sum_squares_mod", kind=ObjectiveKind.SINGLE_TASK, task_id="tool.generated_sum_squares_mod"))
    assert single[0].task_id == "tool.generated_sum_squares_mod"
    assert {task.family for task in single} == {"tool"}

    family = evaluator._objective_subset(ObjectiveSpec(name="sbar:top", kind=ObjectiveKind.FAMILY, family="top"))
    assert 1 <= len(family) <= 4
    assert {task.family for task in family} == {"top"}

    global_subset = evaluator._objective_subset(ObjectiveSpec(name="sbar:global", kind=ObjectiveKind.GLOBAL))
    assert [task.family for task in global_subset] == ["top", "mem", "tool", "e2e"]


def test_build_mutation_prompt_contains_only_mutable_files_and_touched_contracts(runtime_dir: Path) -> None:
    prompt = build_mutation_prompt(
        runtime_dir,
        "sbar:tool",
        ["tool", "ctl"],
        {"phase": "pair"},
        [{"task_id": "tool.generated_sum_squares_mod", "trace_path": "train_trace.json"}],
        [{"runtime_hash": "abc", "score": 1.0}],
    )
    payload = json.loads(prompt)
    runtime = load_runtime(runtime_dir)
    assert payload["objective"] == "sbar:tool"
    assert payload["touched_scope"] == ["tool", "ctl"]
    assert set(payload["mutable_files"]) == set(runtime.manifest.mutable_files)
    assert set(payload["contracts"]) == {"tool", "ctl"}
    assert payload["recent_failing_train_traces"] == [{"task_id": "tool.generated_sum_squares_mod", "trace_path": "train_trace.json"}]
    assert payload["immutable_manifest"] == runtime.manifest.immutable_manifest
    assert payload["patch_rules"] == {"format": "SEARCH/REPLACE only", "max_blocks": 4, "max_changed_lines": 60, "max_search_lines": 8}


def test_build_mutation_prompt_caps_exemplars_at_six(runtime_dir: Path) -> None:
    prompt = build_mutation_prompt(
        runtime_dir,
        "sbar:tool",
        ["tool"],
        {"phase": "pair"},
        [],
        [{"runtime_hash": f"rt-{idx}", "score": float(idx)} for idx in range(8)],
    )
    payload = json.loads(prompt)
    assert len(payload["high_performing_exemplars"]) == 6
    assert payload["high_performing_exemplars"][0]["runtime_hash"] == "rt-0"


def test_control_policy_assign_model_prefers_lowest_cost_qualifying_and_affordable_model(runtime_dir: Path, tmp_path: Path) -> None:
    suite = build_demo_suite()
    runtime, shell, context = _make_context(runtime_dir, tmp_path, suite.by_id("top.sum_product"))
    frame = AgentFrame(agent=shell.agent_pool.clone("root"), objective=context.task.prompt, operation_ids=["sum"], depth=0)
    context.profile.control.model_specs = {
        "cheap": {"solve": 1.0, "cost": 0.05, "latency": 0.10, "dollar": 0.10, "fail": 0.10},
        "mid": {"solve": 1.0, "cost": 0.10, "latency": 0.05, "dollar": 0.05, "fail": 0.05},
        "expensive": {"solve": 1.0, "cost": 0.20, "latency": 0.01, "dollar": 0.01, "fail": 0.01},
    }
    context.profile.control.model_order = ["cheap", "mid", "expensive"]
    chosen = runtime.control.assign_model(context, context.task.operations[0], frame)
    assert chosen == "mid"

    context.budget.cost = 95.0
    context.profile.control.model_specs = {
        "over_budget": {"solve": 1.0, "cost": 0.20, "latency": 0.01, "dollar": 0.01, "fail": 0.01},
        "fits_budget": {"solve": 1.0, "cost": 0.05, "latency": 0.20, "dollar": 0.05, "fail": 0.05},
    }
    context.profile.control.model_order = ["over_budget", "fits_budget"]
    assert runtime.control.assign_model(context, context.task.operations[0], frame) == "fits_budget"


def test_control_policy_request_checks_uses_checker_ladder(runtime_dir: Path, tmp_path: Path) -> None:
    suite = build_demo_suite()
    runtime, _, context = _make_context(runtime_dir, tmp_path, suite.by_id("tool.generated_sum_squares_mod"))
    checks = runtime.control.request_checks(context, {"value": 2}, exact_verifier_exists=True, irreversible=True, external_visible=True)
    assert checks
    assert "benchmark" in checks

    context.task = context.task.copy(update={"externally_visible": False})
    context.state.unresolved_goals = []
    internal_checks = runtime.control.request_checks(context, {"value": 2}, exact_verifier_exists=False, irreversible=False, external_visible=False)
    assert internal_checks == ["local"]


def test_topology_merge_ensemble_uses_deterministic_priority(runtime_dir: Path, tmp_path: Path) -> None:
    runtime, _, _ = _make_context(runtime_dir, tmp_path, build_demo_suite().by_id("top.sum_product"))
    artifact = runtime.topology.merge_ensemble(
        None,
        [
            {"worker_id": "b", "artifact": {"pick": "later"}, "verifier_support": 0.9, "predicted_solve": 0.9, "unresolved_critical": 0},
            {"worker_id": "c", "artifact": {"pick": "verified"}, "verifier_support": 1.0, "predicted_solve": 0.1, "unresolved_critical": 1},
            {"worker_id": "a", "artifact": {"pick": "tie_breaker"}, "verifier_support": 0.9, "predicted_solve": 0.9, "unresolved_critical": 0},
        ],
    )
    assert artifact == {"pick": "verified"}


def test_runner_resolve_agent_reuses_existing_agents_and_keeps_ephemerals_out_of_pool(runtime_dir: Path, tmp_path: Path) -> None:
    task = BenchmarkTask(
        task_id="top.reuse_boundary",
        family="mem",
        prompt="Memory specialist retrieve exact symbol path lookup",
        task_type="unit",
        expected={},
        verifier_type="json_exact",
        allow_best_effort=True,
    )
    runtime, shell, context = _make_context(runtime_dir, tmp_path, task)
    runner = TaskRuntime(runtime, shell, LocalDeterministicProvider())

    reusable = ChildSpec(
        child_id="child_reuse",
        role="child",
        instruction="Memory specialist retrieve exact symbol path lookup",
        tool_scope=[],
        model_class="small",
        required_capabilities=["memory", "retrieve", "symbol"],
        required_permissions=["local"],
        dependency_ids=[],
        comm_mode="summary_only",
        resume_policy="checkpoint",
        init_summary={"op_id": "lookup"},
    )
    created = ChildSpec(
        child_id="child_create",
        role="child",
        instruction="Solve the remote quantum graph coloring subproblem",
        tool_scope=["custom/unknown"],
        model_class="medium",
        required_capabilities=["quantum"],
        required_permissions=["network"],
        dependency_ids=[],
        comm_mode="summary_only",
        resume_policy="checkpoint",
        init_summary={"op_id": "mystery"},
    )

    runtime.topology.score_agent = lambda ctx, agent, child_spec: 1.0 if (child_spec.child_id == "child_reuse" and agent.agent_id == "memory") else -1.0  # type: ignore[method-assign]
    reused_agent = runner._resolve_agent(context, reusable)
    created_agent = runner._resolve_agent(context, created)
    assert reused_agent.agent_id == "memory"
    assert created_agent.agent_id == "child_create"
    assert "child_create" not in {agent.agent_id for agent in shell.agent_pool.list()}


def test_memory_policy_select_spans_for_compaction_limits_groups_and_avoids_overlap(runtime_dir: Path, tmp_path: Path) -> None:
    runtime, shell, context = _make_context(runtime_dir, tmp_path, build_demo_suite().by_id("proxy.mem.compaction_trace"))
    span_ids = [
        shell.short_term.add_node("RawBlob", f"blob_{idx}", "evidence " * 48)
        for idx in range(8)
    ]
    selected = runtime.memory.select_spans_for_compaction(context, span_ids, active_fraction=1.2)
    flattened = [node_id for group in selected for node_id in group]
    assert selected
    assert len(flattened) == len(set(flattened))


def test_memory_policy_summarize_span_preserves_resume_fields(runtime_dir: Path, tmp_path: Path) -> None:
    runtime, _, context = _make_context(runtime_dir, tmp_path, build_demo_suite().by_id("e2e.revenue_report"))
    context.state.unresolved_goals = ["net_total"]
    context.state.open_handle_ids = ["tracked-handle"]
    nodes = [
        {"type": "Artifact", "label": "answer.json", "content": {"ok": True}, "metadata": {"artifact_ref": "answer.json", "symbols": ["FEE_RATE"]}},
        {"type": "OpenHandle", "label": "job-1", "content": {"handle_id": "job-1"}, "metadata": {}},
        {"type": "VerifierEvidence", "label": "benchmark", "content": "score=1.0", "metadata": {}},
        {"type": "Event", "label": "lookup", "content": "retrieved fee rate", "metadata": {"symbols": ["FEE_RATE"]}},
    ]
    summary = runtime.memory.summarize_span(context, nodes)
    assert summary.artifacts == ["answer.json"]
    assert set(summary.open_handles) == {"tracked-handle", "job-1"}
    assert summary.unresolved == ["net_total"]
    assert summary.symbols == ["FEE_RATE"]
    assert any(item.startswith("evidence:benchmark=") for item in summary.evidence)
    assert "event:lookup" in summary.evidence


def test_memory_policy_dedup_and_upsert_follow_exact_symbol_rules(runtime_dir: Path, tmp_path: Path) -> None:
    runtime, shell, context = _make_context(runtime_dir, tmp_path, build_demo_suite().by_id("mem.symbol_lookup"))
    existing = MemoryNode(
        node_id="alpha",
        type="Symbol",
        label="ALPHA_7",
        content="17",
        embedding=[],
        symbol_set=["ALPHA_7"],
        file_paths=[],
        source_task_id="mem.symbol_lookup",
        verifier_support=0.6,
        timestamps={"created": 1.0},
        provenance={"source": "task_context"},
        tombstoned=False,
    )
    shell.long_term.upsert(existing)
    candidate = MemoryNode(
        node_id="candidate",
        type="Symbol",
        label="ALPHA_7",
        content="seventeen",
        embedding=[],
        symbol_set=["ALPHA_7"],
        file_paths=[],
        source_task_id="mem.symbol_lookup",
        verifier_support=1.0,
        timestamps={"created": 2.0},
        provenance={"source": "retrieval"},
        tombstoned=False,
    )
    action, target_id = runtime.memory.dedup_candidates(context, candidate, shell.long_term.all_nodes())
    assert (action, target_id) == ("merge", "alpha")
    runtime.memory.upsert_memory(context, candidate, action, target_id)
    merged = shell.long_term.nodes["alpha"]
    assert merged.content == "seventeen"
    assert merged.verifier_support == 1.0
    runtime.memory.upsert_memory(context, candidate.copy(update={"content": "17 exact"}), "refine", "alpha")
    assert shell.long_term.nodes["alpha"].content == "17 exact"
    runtime.memory.upsert_memory(context, candidate, "tombstone", "alpha")
    assert shell.long_term.nodes["alpha"].tombstoned is True


def test_memory_policy_dedup_does_not_merge_same_label_across_distinct_paths(runtime_dir: Path, tmp_path: Path) -> None:
    runtime, _, context = _make_context(runtime_dir, tmp_path, build_demo_suite().by_id("mem.path_lookup"))
    existing = MemoryNode(
        node_id="existing",
        type="File",
        label="config.yaml",
        content="platform",
        embedding=[],
        symbol_set=[],
        file_paths=["/etc/config.yaml"],
        source_task_id=context.task.task_id,
        verifier_support=0.7,
        timestamps={"created": 1.0},
        provenance={"source": "task_context"},
        tombstoned=False,
    )
    candidate = MemoryNode(
        node_id="candidate",
        type="File",
        label="config.yaml",
        content="api",
        embedding=[],
        symbol_set=[],
        file_paths=["/srv/app/config.yaml"],
        source_task_id=context.task.task_id,
        verifier_support=0.8,
        timestamps={"created": 2.0},
        provenance={"source": "task_context"},
        tombstoned=False,
    )
    action, target_id = runtime.memory.dedup_candidates(context, candidate, [existing])
    assert (action, target_id) == ("new", None)


def test_tool_policy_build_expression_tool_falls_back_to_valid_candidate(runtime_dir: Path, tmp_path: Path) -> None:
    runtime, _, _ = _make_context(runtime_dir, tmp_path, build_demo_suite().by_id("tool.generated_sum_squares_mod"))
    expression, _, executor, tests = runtime.tooling._build_expression_tool(["sum(", "a + b"], {"a": 2, "b": 3}, FixedShell(tmp_path / "fallback_guard").safety_guard)
    assert expression == "a + b"
    assert tests == [{"input": {"a": 2, "b": 3}, "expected": 5}]
    assert executor(a=2, b=3) == 5


def test_tool_policy_promote_tool_requires_thresholds(runtime_dir: Path, tmp_path: Path) -> None:
    runtime, _, context = _make_context(runtime_dir, tmp_path, build_demo_suite().by_id("tool.generated_sum_squares_mod"))
    stable_spec = ToolSpec(
        name="generated/local/promote",
        category_path=["generated", "local"],
        signature="(a,b) -> value",
        description="promote",
        runtime="python",
        deps=[],
        permissions=[],
        tests=[],
        backgroundable=False,
        state_schema={},
        source_digest="digest",
        build_cmd="python -m py_compile tool.py",
        run_cmd="python tool.py",
        timeout_s=10,
        determinism_class="stable",
    )
    promotable = RegisteredTool(spec=stable_spec, historical_passes=4, historical_runs=5, distinct_tasks={"a", "b", "c"}, safety_validated=True)
    unstable = RegisteredTool(spec=stable_spec.copy(update={"determinism_class": "unstable"}), historical_passes=4, historical_runs=5, distinct_tasks={"a", "b", "c"}, safety_validated=True)
    low_reuse = RegisteredTool(spec=stable_spec, historical_passes=4, historical_runs=5, distinct_tasks={"a", "b"}, safety_validated=True)
    assert runtime.tooling.promote_tool(context, promotable) is True
    assert runtime.tooling.promote_tool(context, unstable) is False
    assert runtime.tooling.promote_tool(context, low_reuse) is False


def test_runner_inspects_only_top_k_categories(runtime_dir: Path, tmp_path: Path) -> None:
    suite = build_demo_suite()
    runtime, shell, context = _make_context(runtime_dir, tmp_path, suite.by_id("top.sum_product"))
    runner = TaskRuntime(runtime, shell, LocalDeterministicProvider())
    inspected: list[str] = []
    original_tools_in_category = shell.tool_registry.tools_in_category

    def recording_tools_in_category(category_key: str):
        inspected.append(category_key)
        return original_tools_in_category(category_key)

    context.profile.tooling.k_c = 2
    runtime.tooling.rank_categories = lambda ctx, operation, summaries: ["data/csv", "math/basic", "generated/local", "overflow/category"]  # type: ignore[method-assign]
    runtime.tooling.rank_tools = lambda ctx, operation, candidate_tools: []  # type: ignore[method-assign]
    shell.tool_registry.tools_in_category = recording_tools_in_category  # type: ignore[method-assign]
    frame = AgentFrame(agent=shell.agent_pool.clone("root"), objective=context.task.prompt, operation_ids=["sum"], depth=0, tool_scope=context.state.visible_tool_names)

    output, tool_name, created_tool, faults = runner._execute_tool_operation(context, frame, suite.by_id("top.sum_product").operations[0], {"numbers": [2, 3, 5]}, None)
    assert inspected[-2:] == ["data/csv", "math/basic"]
    assert "overflow/category" not in inspected
    assert output == 10
    assert tool_name == "math/basic/sum_numbers"
    assert created_tool is False
    assert faults == 0


def test_root_tool_scope_filters_after_category_first_discovery(runtime_dir: Path, tmp_path: Path) -> None:
    suite = build_demo_suite()
    task = suite.by_id("tool.csv_stats").copy(deep=True)
    task.operations[0].tool_hint = None
    runtime, shell, context = _make_context(runtime_dir, tmp_path, task)
    runner = TaskRuntime(runtime, shell, LocalDeterministicProvider())
    seen_candidate_names: list[str] = []

    def capture_rank_tools(ctx, operation, candidate_tools):
        seen_candidate_names[:] = sorted(tool.spec.name for tool in candidate_tools)
        return ["data/csv/column_sum"]

    runtime.tooling.rank_categories = lambda ctx, operation, summaries: ["data/csv"]  # type: ignore[method-assign]
    runtime.tooling.rank_tools = capture_rank_tools  # type: ignore[method-assign]
    frame = AgentFrame(
        agent=shell.agent_pool.clone("root"),
        objective=context.task.prompt,
        operation_ids=[task.operations[0].op_id],
        depth=0,
        tool_scope=context.state.visible_tool_names,
    )

    output, tool_name, created_tool, faults = runner._execute_tool_operation(
        context,
        frame,
        task.operations[0],
        {"rows": [{"sales": 5, "region_id": 1}, {"sales": 8, "region_id": 3}], "column": "sales"},
        None,
    )

    assert output == 13.0
    assert tool_name == "data/csv/column_sum"
    assert created_tool is False
    assert faults == 0
    assert seen_candidate_names
    assert all(name.startswith("data/csv/") for name in seen_candidate_names)
    assert "math/basic/sum_numbers" not in seen_candidate_names


def test_tool_hint_cannot_bypass_category_first_discovery(runtime_dir: Path, tmp_path: Path) -> None:
    suite = build_demo_suite()
    task = suite.by_id("tool.csv_stats").copy(deep=True)
    task.operations[0].tool_hint = "math/basic/sum_numbers"
    runtime, shell, context = _make_context(runtime_dir, tmp_path, task)
    runner = TaskRuntime(runtime, shell, LocalDeterministicProvider())
    runtime.tooling.rank_categories = lambda ctx, operation, summaries: ["data/csv", "math/basic"]  # type: ignore[method-assign]
    runtime.tooling.rank_tools = lambda ctx, operation, candidate_tools: ["data/csv/column_sum", "math/basic/sum_numbers"]  # type: ignore[method-assign]
    frame = AgentFrame(agent=shell.agent_pool.clone("root"), objective=context.task.prompt, operation_ids=[task.operations[0].op_id], depth=0, tool_scope=context.state.visible_tool_names)

    output, tool_name, created_tool, faults = runner._execute_tool_operation(
        context,
        frame,
        task.operations[0],
        {"rows": [{"sales": 5, "region_id": 1}, {"sales": 8, "region_id": 3}], "column": "sales"},
        None,
    )

    assert output == 13.0
    assert tool_name == "data/csv/column_sum"
    assert created_tool is False
    assert faults == 0


def test_objective_specs_from_suite_covers_train_tasks_and_global_objectives() -> None:
    suite = build_demo_suite()
    specs = objective_specs_from_suite(suite, partition="train")
    spec_names = {spec.name for spec in specs}
    train_task_names = {f"s:{task.task_id}" for task in suite.train}
    assert train_task_names.issubset(spec_names)
    assert {"sbar:top", "sbar:mem", "sbar:tool", "sbar:e2e", "rhobar:global", "sbar:global"}.issubset(spec_names)
    assert interface_bitmask(["tool", "top"]) == "1010"
    assert interface_bitmask(["ctl", "mem"]) == "0101"


def test_archive_cell_key_uses_interface_diff_mask_relative_to_baseline() -> None:
    archive = QualityDiversityArchive()
    evaluation = _suite_evaluation("runtime", {"sbar:global": 1.0}, [_run_result("top.sum_product", 1.0)])
    archive.insert("baselineish_dir", "runtime_a", "code_a", 10, evaluation, scope=["tool"], mutable_ast_nodes=10, interface_diff_mask="0010")
    archive.insert("multidiff_dir", "runtime_b", "code_b", 10, evaluation, scope=["tool"], mutable_ast_nodes=10, interface_diff_mask="1010")
    island = archive.island("sbar:global")
    assert len(island) == 2
    assert {record.runtime_dir for record in island} == {"baselineish_dir", "multidiff_dir"}


def test_behavior_descriptor_uses_trinary_bins() -> None:
    evaluation = _suite_evaluation(
        "runtime",
        {"sbar:global": 1.0},
        [
            _run_result("t1", 1.0, family_mode="vertical", created_tools=1, promoted_nodes=1, checks_used=2),
            _run_result("t2", 1.0, family_mode="vertical", created_tools=1, promoted_nodes=0, checks_used=2),
            _run_result("t3", 1.0, family_mode="single", created_tools=0, promoted_nodes=0, checks_used=0),
        ],
    )
    assert behavior_descriptor(evaluation) == ["vertical", "mid", "low", "mid"]


def test_estimate_reference_scales_uses_task_medians_with_floor() -> None:
    runs = [
        _run_result("top.task", 1.0, cost=0.2, latency=0.4),
        _run_result("top.task", 1.0, cost=3.0, latency=5.0),
        _run_result("mem.task", 1.0, cost=8.0, latency=2.0),
    ]
    costs, latencies = estimate_reference_scales(runs)
    assert costs["top.task"] == pytest.approx(1.6)
    assert latencies["top.task"] == pytest.approx(2.7)
    assert costs["mem.task"] == 8.0
    assert latencies["mem.task"] == 2.0


def test_mean_improvement_reports_lcb_and_rejects_mismatched_lengths() -> None:
    avg, se, lcb = mean_improvement([0.8, 0.7, 0.9], [0.6, 0.6, 0.6])
    assert avg == pytest.approx(0.2)
    assert se > 0.0
    assert lcb < avg
    with pytest.raises(ValueError):
        mean_improvement([1.0], [1.0, 0.5])


def test_topology_propose_children_preserves_operation_order_and_checkpoint_contract(runtime_dir: Path, tmp_path: Path) -> None:
    suite = build_demo_suite()
    runtime, shell, vertical_context = _make_context(runtime_dir, tmp_path / "vertical", suite.by_id("e2e.revenue_report"))
    vertical_frame = AgentFrame(agent=shell.agent_pool.clone("root"), objective=vertical_context.task.prompt, operation_ids=[op.op_id for op in vertical_context.task.operations], depth=0)
    children = runtime.topology.propose_children(vertical_context, vertical_frame, vertical_context.task.operations)
    assert [child.init_summary["op_id"] for child in children] == [op.op_id for op in vertical_context.task.operations]
    assert [child.dependency_ids for child in children] == [op.dependencies for op in vertical_context.task.operations]
    assert {child.role for child in children} == {"child"}
    assert {child.comm_mode for child in children} == {"summary_only"}
    assert {child.resume_policy for child in children} == {"checkpoint"}
    assert all(child.required_permissions for child in children)


def test_topology_make_checkpoint_preserves_resume_contract(runtime_dir: Path, tmp_path: Path) -> None:
    suite = build_demo_suite()
    runtime, shell, context = _make_context(runtime_dir, tmp_path, suite.by_id("e2e.revenue_report"))
    context.state.unresolved_goals = ["net_total"]
    frame = AgentFrame(
        agent=shell.agent_pool.clone("root"),
        objective=context.task.prompt,
        operation_ids=["gross_total", "fee_rate"],
        depth=1,
        role="child",
        tool_scope=["data/csv/column_sum"],
        model_class="small",
    )
    checkpoint = runtime.topology.make_checkpoint(
        context,
        frame,
        {"gross_total": 40, "fee_rate": 0.1},
        ["net_total"],
        ["handle-1"],
    )
    assert checkpoint.summary.unresolved == ["net_total"]
    assert checkpoint.summary.open_handles == ["handle-1"]
    assert checkpoint.summary.next_actions == ["resume"]
    assert checkpoint.artifact_refs == ["gross_total", "fee_rate"]
    assert checkpoint.resume_constraints == {"tool_scope": ["data/csv/column_sum"], "model_class": "small"}


def test_memory_retrieve_long_term_prefers_exact_path_and_task_context(runtime_dir: Path, tmp_path: Path) -> None:
    task = BenchmarkTask(
        task_id="mem.path_priority",
        family="mem",
        prompt="Find the owner of /srv/app/config.yaml.",
        task_type="unit",
        file_paths=["/srv/app/config.yaml"],
        expected="platform",
        verifier_type="string_exact",
    )
    runtime, _, context = _make_context(runtime_dir, tmp_path, task)
    exact = MemoryNode(
        node_id="exact",
        type="File",
        label="/srv/app/config.yaml",
        content="platform",
        embedding=[],
        symbol_set=[],
        file_paths=["/srv/app/config.yaml"],
        source_task_id=task.task_id,
        verifier_support=1.0,
        timestamps={"created": 1.0},
        provenance={"source": "task_context"},
        tombstoned=False,
    )
    fuzzy = MemoryNode(
        node_id="fuzzy",
        type="TaskNote",
        label="config owner",
        content="maybe platform",
        embedding=[],
        symbol_set=[],
        file_paths=[],
        source_task_id=task.task_id,
        verifier_support=0.2,
        timestamps={"created": 1.0},
        provenance={"source": "other"},
        tombstoned=False,
    )
    ranked = runtime.memory.retrieve_long_term(context, task.prompt, [], task.file_paths, [fuzzy, exact])
    assert [node.node_id for node in ranked][:2] == ["exact", "fuzzy"]


def test_memory_score_and_promotion_gate_use_verifier_support(runtime_dir: Path, tmp_path: Path) -> None:
    runtime, _, context = _make_context(runtime_dir, tmp_path, build_demo_suite().by_id("mem.symbol_lookup"))
    low_support = MemoryNode(
        node_id="symbol",
        type="Symbol",
        label="ALPHA_7",
        content="17",
        embedding=[],
        symbol_set=["ALPHA_7"],
        file_paths=[],
        source_task_id=context.task.task_id,
        verifier_support=0.4,
        timestamps={"created": 1.0},
        provenance={"source": "task_context"},
        tombstoned=False,
    )
    high_support = low_support.copy(update={"node_id": "symbol_high", "verifier_support": 0.8})
    note = low_support.copy(update={"node_id": "note", "type": "TaskNote"})
    assert runtime.memory.score_memory_unit(context, high_support, []) > runtime.memory.score_memory_unit(context, low_support, [])
    assert runtime.memory.should_promote(context, low_support, 1.0) is False
    assert runtime.memory.should_promote(context, high_support, 1.0) is True
    assert runtime.memory.should_promote(context, note, 1.0) is True


def test_memory_compaction_respects_high_watermark_and_shell_resets_long_term_by_task(tmp_path: Path) -> None:
    shell = FixedShell(tmp_path / "shell_reset")
    runtime_dir = shell.workspace / "runtime_unused"
    node = MemoryNode(
        node_id="symbol",
        type="Symbol",
        label="ALPHA_1",
        content="3",
        embedding=[],
        symbol_set=["ALPHA_1"],
        file_paths=[],
        source_task_id="task",
        verifier_support=1.0,
        timestamps={"created": 0.0},
        provenance={"source": "task_context"},
        tombstoned=False,
    )
    shell.long_term.upsert(node)
    shell.reset_for_task("task_a", transfer_scored=False)
    assert shell.long_term.all_nodes() == []
    shell.reset_for_task("task_a", transfer_scored=True, episode_id="episode-1")
    shell.long_term.upsert(node)
    shell.reset_for_task("task_b", transfer_scored=True, episode_id="episode-1")
    assert [item.node_id for item in shell.long_term.all_nodes()] == ["symbol"]

    runtime = load_runtime(init_runtime(tmp_path / "runtime_mem_reset"))
    task = build_demo_suite().by_id("proxy.mem.compaction_trace")
    context = PolicyContext(
        runtime_dir=runtime.runtime_dir,
        shell=FixedShell(tmp_path / "shell_compaction"),
        task=task,
        provider=LocalDeterministicProvider(),
        profile=load_runtime_profile(runtime.runtime_dir),
        seed=0,
        state=RuntimeState(visible_tool_names=[]),
        budget=RuntimeBudget(),
        trace=[],
        objective=task.prompt,
    )
    span_ids = [
        context.shell.short_term.add_node("RawBlob", f"blob_{idx}", "evidence " * 32)
        for idx in range(4)
    ]
    assert runtime.memory.select_spans_for_compaction(context, span_ids, active_fraction=0.75) == []
    selected = runtime.memory.select_spans_for_compaction(context, span_ids, active_fraction=1.2)
    assert selected
    assert len({node_id for group in selected for node_id in group}) == sum(len(group) for group in selected)


def test_tool_policy_ranks_categories_tools_and_dispatches_backgroundable_async(runtime_dir: Path, tmp_path: Path) -> None:
    suite = build_demo_suite()
    runtime, shell, context = _make_context(runtime_dir, tmp_path, suite.by_id("tool.csv_stats"))
    operation = suite.by_id("tool.csv_stats").operations[0]
    category_summaries = {
        "math/basic": {"summary": "generic math", "descendants": 5, "historical_pass_rate": 0.2, "cache_hit": 0.2, "coldstart": 0.1, "permission_risk": 0.0},
        "data/csv": {"summary": "sum and max csv columns", "descendants": 2, "historical_pass_rate": 1.0, "cache_hit": 1.0, "coldstart": 0.05, "permission_risk": 0.0},
    }
    assert runtime.tooling.rank_categories(context, operation, category_summaries)[0] == "data/csv"

    sum_tool = shell.tool_registry.get("data/csv/column_sum")
    math_tool = shell.tool_registry.get("math/basic/sum_numbers")
    sum_tool.historical_runs = 5
    sum_tool.historical_passes = 5
    ranked_tools = runtime.tooling.rank_tools(context, operation, [math_tool, sum_tool])
    assert ranked_tools[0] == "data/csv/column_sum"
    assert runtime.tooling.should_create_tool(context, operation, ranked_tools) is False

    backgroundable_spec = ToolSpec(
        name="generated/local/bg",
        category_path=["generated", "local"],
        signature="(x) -> value",
        description="background",
        runtime="python",
        deps=[],
        permissions=[],
        tests=[],
        backgroundable=True,
        state_schema={},
        source_digest="digest",
        build_cmd="python -m py_compile tool.py",
        run_cmd="python tool.py",
        timeout_s=10,
        determinism_class="stable",
    )
    bg_tool = RegisteredTool(spec=backgroundable_spec, executor=lambda x: x)
    shell.tool_registry._tools[backgroundable_spec.name] = bg_tool
    assert runtime.tooling.dispatch_tool(context, backgroundable_spec.name, {"x": 1}) == {"async": True}


def test_control_stop_policy_respects_verification_requirements(runtime_dir: Path, tmp_path: Path) -> None:
    suite = build_demo_suite()
    runtime, _, context = _make_context(runtime_dir, tmp_path, suite.by_id("tool.generated_sum_squares_mod"))
    assert runtime.control.stop_policy(context, -0.5, -0.4, unresolved_count=0, verified_terminal=False) is False
    context.task = context.task.copy(update={"allow_best_effort": True})
    assert runtime.control.stop_policy(context, -0.5, -0.4, unresolved_count=0, verified_terminal=False) is False
    context.task = context.task.copy(update={"allow_best_effort": False})
    assert runtime.control.stop_policy(context, -0.5, -0.4, unresolved_count=0, verified_terminal=True) is True


def test_runner_returns_controlled_failure_when_verification_required_and_unverified(runtime_dir: Path, tmp_path: Path) -> None:
    runtime = load_runtime(runtime_dir)
    shell = FixedShell(tmp_path / "shell_controlled_failure")
    runner = TaskRuntime(runtime, shell, LocalDeterministicProvider())
    task = BenchmarkTask(
        task_id="tool.must_verify",
        family="tool",
        prompt="Return the sum of [2, 3].",
        task_type="unit",
        operations=[
            OperationSpec(
                op_id="sum",
                kind="builtin",
                output_key="value",
                description="Compute sum of numbers",
                tool_hint="math/basic/sum_numbers",
                args={"numbers": [2, 3]},
            )
        ],
        expected=99,
        verifier_type="number_exact",
        verification_required=True,
        allow_best_effort=False,
    )
    result = runner.run_task(task, 0)
    assert result.verifier_score == 0.0
    assert result.artifact == {"error": "controlled_failure"}


def test_runner_falls_back_to_reusable_tool_when_synthesis_fails(runtime_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    suite = build_demo_suite()
    task = suite.by_id("tool.generated_sum_squares_mod").copy(deep=True)
    runtime, shell, context = _make_context(runtime_dir, tmp_path, task)
    runner = TaskRuntime(runtime, shell, LocalDeterministicProvider())

    fallback_spec = ToolSpec(
        name="generated/local/fallback_sum_sq",
        category_path=["generated", "local"],
        signature="(numbers, modulus) -> value",
        description="fallback sum of squares mod",
        runtime="python",
        deps=[],
        permissions=[],
        tests=[],
        backgroundable=False,
        state_schema={},
        source_digest="fallback",
        build_cmd="python -m py_compile tool.py",
        run_cmd="python tool.py",
        timeout_s=10,
        determinism_class="stable",
    )
    shell.tool_registry._tools[fallback_spec.name] = RegisteredTool(
        spec=fallback_spec,
        executor=lambda numbers, modulus: sum(x * x for x in numbers) % modulus,
        safety_validated=True,
    )
    shell.tool_registry._category_summaries["generated/local"] = fallback_spec.description
    runtime.tooling.rank_categories = lambda ctx, operation, summaries: ["generated/local"]  # type: ignore[method-assign]
    runtime.tooling.rank_tools = lambda ctx, operation, candidate_tools: [fallback_spec.name]  # type: ignore[method-assign]
    frame = AgentFrame(agent=shell.agent_pool.clone("root"), objective=context.task.prompt, operation_ids=[task.operations[0].op_id], depth=0, tool_scope=context.state.visible_tool_names)

    def fail_propose_tool_spec(ctx, operation, resolved_args=None):
        raise ValidationError("synthetic failure")

    monkeypatch.setattr(runtime.tooling, "propose_tool_spec", fail_propose_tool_spec)

    output, tool_name, created_tool, faults = runner._execute_tool_operation(
        context,
        frame,
        task.operations[0],
        {"numbers": [1, 2, 3, 4], "modulus": 7},
        None,
    )
    assert output == 2
    assert tool_name == fallback_spec.name
    assert created_tool is False
    assert faults == 1


def test_evaluator_stage2_filters_proxy_tasks_by_scope_and_falls_back(tmp_path: Path) -> None:
    suite = build_demo_suite()
    evaluator = RuntimeEvaluator(suite, tmp_path / "eval", LocalDeterministicProvider(), baseline_runtime_dir=None)
    captured: list[list[str]] = []

    def fake_evaluate_runtime(runtime_dir, partition="proxy", seeds=(0,), use_cache=True, tasks_override=None):
        task_ids = [task.task_id for task in tasks_override or []]
        captured.append(task_ids)
        return SuiteEvaluation(
            runtime_hash=str(runtime_dir),
            objective_scores={f"s:{task_id}": 1.0 for task_id in task_ids},
            task_scores={},
            family_scores={},
            run_results=[_run_result(task_id, 1.0) for task_id in task_ids],
            invalid=False,
        )

    evaluator.evaluate_runtime = fake_evaluate_runtime  # type: ignore[method-assign]
    stage = evaluator.stage2_proxy(tmp_path / "parent", tmp_path / "child", ["mem"])
    assert stage.passed is True
    assert captured[0]
    scoped_tasks = {task.task_id: task for task in suite.proxy}
    assert all("mem" in scoped_tasks[task_id].proxy_scope_tags for task_id in captured[0])
    captured.clear()
    evaluator.stage2_proxy(tmp_path / "parent", tmp_path / "child", ["ctl_only_missing"])
    assert captured[0] == [suite.proxy[0].task_id]


def test_evaluator_stage_comparisons_use_common_random_numbers_for_parent_and_child(tmp_path: Path) -> None:
    suite = build_demo_suite()
    evaluator = RuntimeEvaluator(suite, tmp_path / "eval", LocalDeterministicProvider(), baseline_runtime_dir=None)
    parent_dir = init_runtime(tmp_path / "parent")
    child_dir = init_runtime(tmp_path / "child")
    calls: list[tuple[str, str, tuple[int, ...], tuple[str, ...]]] = []

    def fake_evaluate_runtime(runtime_dir, partition="train", seeds=(0, 1, 2), use_cache=True, tasks_override=None):
        task_ids = tuple(task.task_id for task in tasks_override or [])
        calls.append((Path(runtime_dir).name, partition, tuple(seeds), task_ids))
        return SuiteEvaluation(
            runtime_hash=str(runtime_dir),
            objective_scores={f"s:{task_id}": 1.0 for task_id in task_ids},
            task_scores={},
            family_scores={},
            run_results=[_run_result(task_id, 1.0) for task_id in task_ids],
            invalid=False,
        )

    evaluator.evaluate_runtime = fake_evaluate_runtime  # type: ignore[method-assign]
    evaluator.stage2_proxy(parent_dir, child_dir, ["tool"])
    assert calls[0][1] == calls[1][1] == "proxy"
    assert calls[0][2] == calls[1][2] == (0,)
    assert calls[0][2] == calls[1][2]
    assert calls[0][3] == calls[1][3]

    calls.clear()
    objective = ObjectiveSpec(name="sbar:tool", kind=ObjectiveKind.FAMILY, family="tool")
    evaluator.stage3_local_subset(parent_dir, child_dir, objective)
    assert calls[0][2] == calls[1][2] == (0,)
    assert calls[0][3] == calls[1][3]

    calls.clear()
    evaluator.stage4_full(parent_dir, child_dir)
    assert calls[0][1] == calls[1][1] == "train"
    assert calls[0][2] == calls[1][2] == (0, 1, 2)


def test_evaluator_evaluate_validation_uses_five_seed_window(tmp_path: Path) -> None:
    evaluator = RuntimeEvaluator(build_demo_suite(), tmp_path / "eval", LocalDeterministicProvider(), baseline_runtime_dir=None)
    seen: list[tuple[int, ...]] = []

    def fake_evaluate_runtime(runtime_dir, partition="train", seeds=(0, 1, 2), use_cache=True, tasks_override=None):
        seen.append(tuple(seeds))
        return SuiteEvaluation(runtime_hash="runtime", objective_scores={}, task_scores={}, family_scores={}, run_results=[], invalid=False)

    evaluator.evaluate_runtime = fake_evaluate_runtime  # type: ignore[method-assign]
    evaluator.evaluate_validation(tmp_path / "runtime")
    assert seen == [(0, 1, 2, 3, 4)]


def test_sandbox_hash_is_content_addressed_and_changes_with_validation_inputs(tmp_path: Path) -> None:
    manager = SandboxManager(tmp_path / "sandboxes")
    spec = ToolSpec(
        name="generated/local/hash_case",
        category_path=["generated", "local"],
        signature="(a,b) -> value",
        description="hash case",
        runtime="python",
        deps=["numpy"],
        permissions=[],
        tests=[{"input": {"a": 2, "b": 3}, "expected": 5}],
        backgroundable=False,
        state_schema={},
        source_digest="source-a",
        build_cmd="python -m py_compile tool.py",
        run_cmd="python tool.py",
        timeout_s=10,
        determinism_class="stable",
    )
    same_hash = manager.sandbox_hash(spec, test_digest="digest-a")
    assert manager.sandbox_hash(spec, test_digest="digest-a") == same_hash
    assert manager.sandbox_hash(spec.copy(update={"source_digest": "source-b"}), test_digest="digest-a") != same_hash
    assert manager.sandbox_hash(spec, test_digest="digest-b") != same_hash


def test_verifier_checkers_cover_local_repo_and_benchmark() -> None:
    task = build_demo_suite().by_id("top.sum_product")
    local = run_checker(task, {"sum": 10}, [], "local")
    repo = run_checker(task, {"sum": 10, "product": 30}, [], "repo")
    benchmark = run_checker(task, {"sum": 10, "product": 30}, [], "benchmark")
    assert local["passed"] is True
    assert repo["passed"] is True
    assert benchmark["score"] == 1.0
    score, evidence = verify_task_with_evidence(task, {"sum": 10, "product": 30}, [])
    assert score == 1.0
    assert evidence["matched"] is True


def test_local_provider_tool_spec_payload_uses_prompt_args_for_fallback() -> None:
    provider = LocalDeterministicProvider()
    response = provider.generate(
        ModelRequest(
            instructions="Return only JSON with keys expression and description for a deterministic Python tool.",
            prompt=json.dumps({"description": "Add the values", "args": {"a": 2, "b": 3}}, sort_keys=True),
            model_class="medium",
            seed=0,
            metadata={"mode": "tool_spec", "payload": {"expression": "", "description": "ignored", "args": {"x": 1}}},
        )
    )
    payload = json.loads(response.text)
    assert payload["expression"] == "a + b"
    assert payload["args"] == {"a": 2, "b": 3}


def test_tool_policy_validate_uses_shell_sandbox_manager(runtime_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, shell, context = _make_context(runtime_dir, tmp_path, build_demo_suite().by_id("tool.generated_sum_squares_mod"))
    captured: list[Any] = []

    def fake_validate_tool_candidate(spec, source, safety_guard, sandbox_manager=None):
        captured.append(sandbox_manager)
        return {"deterministic": True}

    monkeypatch.setitem(runtime.tooling.validate_tool.__globals__, "validate_tool_candidate", fake_validate_tool_candidate)
    spec = ToolSpec(
        name="generated/local/validate_sandbox",
        category_path=["generated", "local"],
        signature="(a,b) -> value",
        description="test",
        runtime="python",
        deps=[],
        permissions=[],
        tests=[],
        backgroundable=False,
        state_schema={},
        source_digest="digest",
        build_cmd="python -m py_compile tool.py",
        run_cmd="python tool.py",
        timeout_s=10,
        determinism_class="stable",
    )

    assert runtime.tooling.validate_tool(context, spec, "def run(a, b):\n    return a + b\n") is True
    assert captured == [context.shell.sandbox_manager]
