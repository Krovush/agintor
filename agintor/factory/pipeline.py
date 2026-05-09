from __future__ import annotations

import contextlib
import json
import secrets
import shutil
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable

from ..storage.artifacts import ArtifactMode
from ..evaluation.benchmarks import BenchmarkSuite, build_demo_suite
from ..search.engine import EvolutionEngine
from ..storage.factory_chat_store import CHAT_DIR_NAME, FactoryChatError, FactoryChatStore
from ..factory.goals import (
    amend_goal_spec,
    build_goal_spec,
    build_success_criteria_bundle,
    canonical_goal_prompt,
)
from ..runtime.project import baseline_template_dir, init_runtime
from ..providers import LocalDeterministicProvider, ModelProvider
from ..runtime.api import build_trace_context, load_solve_request, runtime_solve_request_for_user_request
from ..runtime.host import RuntimeHost
from ..runtime.loader import (
    DEPLOYMENT_CONTRACT_FILE,
    RUNTIME_EXPORT_BUNDLE_FILE,
    load_runtime,
)
from ..runtime.profile import (
    RUNTIME_PROFILE_FILE,
    HostedProviderProfile,
    RuntimeProfile,
    load_runtime_profile,
    runtime_profile_payload,
)
from ..runtime.sdk import (
    KERNEL_BUNDLE_DIR,
    KERNEL_CAPABILITY_FLAGS,
    KERNEL_MANIFEST_FILE,
    bundle_runtime_kernel,
    preview_kernel_manifest,
)
from ..contracts import (
    ArchiveEntry,
    ArchiveRecord,
    BenchmarkPlan,
    BuildSummary,
    DeploymentContract,
    ExportSummary,
    FactoryChatIdentity,
    FactoryMessage,
    GoalSpec,
    ProviderPlan,
    ProviderRole,
    RuntimeIsolationPolicy,
    RuntimeManifest,
    RuntimePlan,
    ModelRequest,
    OpenAITraceContext,
    SuccessCriteriaBundle,
    VerifierBundle,
    VerifierSpec,
)
from ..utils import ensure_directory, now_ts, stable_hash
from ..core.versioning import RUNTIME_CONTRACT_VERSION


from .export import (
    _export_candidate_records,
    _persist_benchmark_suite,
    _persist_model,
    _replace_runtime_destination,
    _runtime_relative_path,
    _score_rows_for_candidates,
    _validate_exported_runtime,
    _without_bytecode_writes,
    _write_json,
    _write_seed_runtime,
)
from .planning import (
    _build_benchmark_plan,
    _build_runtime_plan,
    _build_verifier_bundle,
    _factory_runtime_profile_for_plan,
    _goal_score_keys,
    _maybe_provider_refine_planning,
    _normalize_benchmark_plan_against_suite,
    _plan_consistency_check,
    _repair_planning_artifacts,
    build_goal_conditioned_suite,
)
from .trace_context import _build_factory_trace_context
from .workspace import _build_workspace_layout

def _run_factory_pipeline(
    *,
    goal_input: GoalSpec | str,
    destination: str | Path,
    workspace: str | Path,
    provider: ModelProvider,
    steps: int = 10,
    mutator_type: str = "heuristic",
    profile_path: str | Path | None = None,
    runtime_profile: RuntimeProfile | None = None,
    runtime_backend: str | None = None,
    artifact_mode: str | ArtifactMode | None = None,
    force: bool = False,
    seed_runtime_source: Path | None = None,
    trace_context: OpenAITraceContext | None = None,
) -> BuiltRuntimeResult:
    if isinstance(goal_input, GoalSpec):
        clean_goal = canonical_goal_prompt(goal_input.normalized_goal)
        prebuilt_goal_spec = goal_input
    else:
        clean_goal = canonical_goal_prompt(goal_input)
        prebuilt_goal_spec = None
    if not clean_goal:
        raise ValueError("goal prompt may not be empty")
    destination_path = Path(destination)
    layout = _build_workspace_layout(workspace, clean_goal)
    build_id = f"build.{stable_hash(clean_goal, layout.root.name)[:12]}"
    factory_trace_context = _build_factory_trace_context(
        trace_context,
        build_id=build_id,
        objective=clean_goal,
    )
    agintor_provider = getattr(provider, "provider_name", provider.__class__.__name__.lower())
    effective_runtime_backend = (runtime_backend or "local").strip().lower()
    merged_profile = runtime_profile or load_runtime_profile(profile_path=profile_path)
    (layout.goal_dir / "raw_goal.txt").write_text(clean_goal, encoding="utf-8")

    if prebuilt_goal_spec is not None:
        local_goal_spec = prebuilt_goal_spec
    else:
        local_goal_spec = build_goal_spec(
            clean_goal,
            runtime_provider_name=merged_profile.runtime_provider.name,
            default_runtime_backend=effective_runtime_backend,
        )
    local_success_bundle = build_success_criteria_bundle(local_goal_spec)
    local_suite = build_goal_conditioned_suite(local_goal_spec, merged_profile)
    local_benchmark_plan = _normalize_benchmark_plan_against_suite(
        _build_benchmark_plan(local_goal_spec, local_suite),
        local_suite,
        goal_spec=local_goal_spec,
    )
    goal_spec, success_bundle, benchmark_plan = _maybe_provider_refine_planning(
        provider,
        goal_prompt=clean_goal,
        goal_spec=local_goal_spec,
        success_bundle=local_success_bundle,
        benchmark_plan=local_benchmark_plan,
        suite=local_suite,
        trace_context=factory_trace_context,
    )
    goal_suite = _persist_benchmark_suite(
        layout.planning_dir / "benchmark_suite.json",
        build_goal_conditioned_suite(goal_spec, merged_profile),
    )
    benchmark_plan = _normalize_benchmark_plan_against_suite(
        benchmark_plan,
        goal_suite,
        goal_spec=goal_spec,
    )
    goal_spec = _persist_model(
        layout.goal_dir / "goal_spec.json",
        GoalSpec,
        goal_spec,
    )
    success_bundle = _persist_model(
        layout.goal_dir / "success_criteria.json",
        SuccessCriteriaBundle,
        success_bundle,
    )
    benchmark_plan = _persist_model(
        layout.planning_dir / "benchmark_plan.json",
        BenchmarkPlan,
        benchmark_plan,
    )
    verifier_bundle = _persist_model(
        layout.planning_dir / "verifier_bundle.json",
        VerifierBundle,
        _build_verifier_bundle(benchmark_plan, goal_suite),
    )
    goal_keys = _goal_score_keys(goal_spec, goal_suite)
    runtime_plan = _persist_model(
        layout.planning_dir / "runtime_plan.json",
        RuntimePlan,
        _build_runtime_plan(
            goal_spec,
            goal_suite,
            benchmark_plan,
            merged_profile,
            agintor_provider=agintor_provider,
            runtime_backend=effective_runtime_backend,
        ),
    )
    deployment_contract = _persist_model(
        layout.planning_dir / DEPLOYMENT_CONTRACT_FILE,
        DeploymentContract,
        runtime_plan.deployment_contract,
    )
    runtime_plan = _persist_model(
        layout.planning_dir / "runtime_plan.json",
        RuntimePlan,
        (runtime_plan).model_copy(update={"deployment_contract": deployment_contract}),
    )
    planning_issues = _plan_consistency_check(
        goal_spec,
        goal_suite,
        verifier_bundle,
        runtime_plan,
    )
    verifier_bundle, runtime_plan, deployment_contract, repaired = _repair_planning_artifacts(
        goal_spec,
        goal_suite,
        benchmark_plan,
        verifier_bundle,
        runtime_plan,
        deployment_contract,
        planning_issues,
    )
    if repaired:
        verifier_bundle = _persist_model(
            layout.planning_dir / "verifier_bundle.json",
            VerifierBundle,
            verifier_bundle,
        )
        deployment_contract = _persist_model(
            layout.planning_dir / DEPLOYMENT_CONTRACT_FILE,
            DeploymentContract,
            deployment_contract,
        )
        runtime_plan = _persist_model(
            layout.planning_dir / "runtime_plan.json",
            RuntimePlan,
            (runtime_plan).model_copy(update={"deployment_contract": deployment_contract}),
        )
        planning_issues = _plan_consistency_check(
            goal_spec,
            goal_suite,
            verifier_bundle,
            runtime_plan,
        )
    blocking_issues = [issue for issue in planning_issues if issue.get("severity") == "error"]
    if blocking_issues:
        messages = "; ".join(str(issue.get("message") or "") for issue in blocking_issues)
        raise RuntimeError(f"runtime plan failed consistency checks before seed materialization: {messages}")

    effective_runtime_profile = _factory_runtime_profile_for_plan(merged_profile, runtime_plan)
    _write_seed_runtime(
        layout.seed_runtime_dir,
        runtime_plan,
        seed_source=seed_runtime_source,
        runtime_profile=effective_runtime_profile,
        runtime_backend=effective_runtime_backend,
    )
    engine = EvolutionEngine(
        goal_suite,
        layout.evolution_dir,
        provider,
        layout.seed_runtime_dir,
        mutator_type=mutator_type,
        reference_runtime_dir=layout.seed_runtime_dir,
        runtime_backend=effective_runtime_backend,
        runtime_profile=effective_runtime_profile,
        profile_path=None,
        artifact_mode=artifact_mode or ArtifactMode.ALWAYS,
        trace_context=factory_trace_context,
    )
    summary = engine.run(steps=steps)
    candidates = _export_candidate_records(engine, goal_keys)
    if not candidates:
        raise RuntimeError("runtime build produced no archive candidates")
    leaderboard_rows = _score_rows_for_candidates(engine, candidates, goal_keys)
    leaderboard_path = _write_json(layout.evolution_dir / "leaderboard.json", leaderboard_rows)
    leader_row = next((row for row in leaderboard_rows if row.get("export_eligible")), None)
    if leader_row is None:
        raise RuntimeError("leader validation failed for every candidate group; no exportable runtime was produced")
    leader = next(record for record in candidates if record.entry.runtime_hash == leader_row["runtime_hash"])
    best_goal_score = float(leader_row["goal_score"])
    best_val_score = float(leader_row["validation_score"])

    _replace_runtime_destination(
        Path(leader.runtime_dir),
        destination_path,
        force=force,
        preserve_names=(CHAT_DIR_NAME, ".runtime_sessions"),
    )
    _write_json(destination_path / RUNTIME_PROFILE_FILE, runtime_plan.runtime_profile)
    _write_json(destination_path / DEPLOYMENT_CONTRACT_FILE, deployment_contract)
    bundle_runtime_kernel(destination_path, force=True)
    with _without_bytecode_writes():
        exported_runtime = load_runtime(
            destination_path,
            runtime_backend=effective_runtime_backend,
        )
    runtime_hash = exported_runtime.runtime_hash
    code_hash = exported_runtime.code_hash
    manifest_version = exported_runtime.manifest.version
    runtime_id = exported_runtime.manifest.runtime_id
    _validate_exported_runtime(
        destination_path,
        build_id=build_id,
        goal_id=goal_spec.goal_id,
        runtime_id=runtime_id,
        runtime_hash=runtime_hash,
        runtime_backend=effective_runtime_backend,
        runtime_profile=effective_runtime_profile,
        export_dir=layout.export_dir,
    )
    export_bundle_path = destination_path / RUNTIME_EXPORT_BUNDLE_FILE
    kernel_manifest_path = destination_path / KERNEL_BUNDLE_DIR / KERNEL_MANIFEST_FILE
    export_bundle = {
        "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
        "runtime_hash": runtime_hash,
        "code_hash": code_hash,
        "manifest_version": manifest_version,
        "runtime_id": runtime_id,
        "build_id": build_id,
        "goal_id": goal_spec.goal_id,
        "agintor_provider": agintor_provider,
        "runtime_provider": runtime_plan.provider_plan.runtime_provider.name,
        "source_runtime_dir": str(leader.runtime_dir),
        "source_runtime_hash": leader.entry.runtime_hash,
        "selection_policy": "goal_score_mean_then_validation",
        "runtime_profile_file": RUNTIME_PROFILE_FILE,
        "deployment_contract_file": DEPLOYMENT_CONTRACT_FILE,
        "kernel_manifest_file": _runtime_relative_path(destination_path, kernel_manifest_path),
        "export_bundle_file": RUNTIME_EXPORT_BUNDLE_FILE,
    }
    _write_json(export_bundle_path, export_bundle)
    workspace_export_summary = _persist_model(
        layout.export_dir / "export_summary.json",
        ExportSummary,
        ExportSummary(
            export_id=f"export.{build_id}",
            build_id=build_id,
            goal_id=goal_spec.goal_id,
            goal_prompt=clean_goal,
            runtime_hash=runtime_hash,
            code_hash=code_hash,
            runtime_id=runtime_id,
            runtime_contract_version=RUNTIME_CONTRACT_VERSION,
            source_runtime_dir=str(leader.runtime_dir),
            source_runtime_hash=leader.entry.runtime_hash,
            runtime_profile_path=RUNTIME_PROFILE_FILE,
            deployment_contract_path=DEPLOYMENT_CONTRACT_FILE,
            export_bundle_path=RUNTIME_EXPORT_BUNDLE_FILE,
            leaderboard_path=str(leaderboard_path),
            runtime_plan_path=str(layout.planning_dir / "runtime_plan.json"),
        ),
    )
    _persist_model(
        destination_path / "export_summary.json",
        ExportSummary,
        (workspace_export_summary).model_copy(update={
                "leaderboard_path": "",
                "runtime_plan_path": "",
            }),
    )
    build_summary = _persist_model(
        layout.export_dir / "build_summary.json",
        BuildSummary,
        BuildSummary(
            build_id=build_id,
            goal_id=goal_spec.goal_id,
            goal_prompt=clean_goal,
            goal_task_ids=[key[2:] for key in goal_keys],
            goal_spec_path=str(layout.goal_dir / "goal_spec.json"),
            success_criteria_path=str(layout.goal_dir / "success_criteria.json"),
            benchmark_plan_path=str(layout.planning_dir / "benchmark_plan.json"),
            verifier_bundle_path=str(layout.planning_dir / "verifier_bundle.json"),
            runtime_plan_path=str(layout.planning_dir / "runtime_plan.json"),
            deployment_contract_path=str(layout.planning_dir / DEPLOYMENT_CONTRACT_FILE),
            workspace=str(layout.root),
            output_runtime_dir=str(destination_path),
            history_path=getattr(summary, "history_path", ""),
            archive_index_path=getattr(summary, "archive_index_path", ""),
            validation_history_path=getattr(summary, "validation_history_path", ""),
            stage_failures_path=getattr(summary, "stage_failures_path", ""),
            evidence_ledger_path=getattr(summary, "evidence_ledger_path", ""),
            paired_comparisons_path=getattr(summary, "paired_comparisons_path", ""),
            promotion_ledger_path=getattr(summary, "promotion_ledger_path", ""),
            signal_sufficiency_path=getattr(summary, "signal_sufficiency_path", ""),
            promotion_counts=dict(getattr(summary, "promotion_counts", {}) or {}),
            decision_counts=dict(getattr(summary, "decision_counts", {}) or {}),
            leaderboard_path=str(leaderboard_path),
            leader_runtime_hash=leader.entry.runtime_hash,
            leader_runtime_dir=str(leader.runtime_dir),
            runtime_contract_version=RUNTIME_CONTRACT_VERSION,
            selection_policy="goal_score_mean_then_validation",
            best_train_score=summary.best_train_score,
            best_goal_score=best_goal_score,
            best_val_score=best_val_score,
            accepted_mutations=summary.accepted,
            archive_cells=summary.archive_cells,
            agintor_provider=agintor_provider,
            runtime_provider=runtime_plan.provider_plan.runtime_provider.name,
            export_bundle_file=RUNTIME_EXPORT_BUNDLE_FILE,
            export_summary_path=str(layout.export_dir / "export_summary.json"),
        ),
    )
    from .service import BuiltRuntimeResult

    return BuiltRuntimeResult(
        build_id=build_id,
        goal_id=goal_spec.goal_id,
        goal_prompt=clean_goal,
        goal_spec_path=build_summary.goal_spec_path,
        success_criteria_path=build_summary.success_criteria_path,
        benchmark_plan_path=build_summary.benchmark_plan_path,
        verifier_bundle_path=build_summary.verifier_bundle_path,
        runtime_plan_path=build_summary.runtime_plan_path,
        output_runtime_dir=str(destination_path),
        workspace=str(layout.root),
        agintor_provider=agintor_provider,
        runtime_provider=runtime_plan.provider_plan.runtime_provider.name,
        mutator_type=mutator_type,
        best_train_score=summary.best_train_score,
        best_goal_score=best_goal_score,
        best_val_score=best_val_score,
        archive_cells=summary.archive_cells,
        accepted_mutations=summary.accepted,
        export_bundle_file=RUNTIME_EXPORT_BUNDLE_FILE,
        export_summary_path=str(layout.export_dir / "export_summary.json"),
        summary_path=str(layout.export_dir / "build_summary.json"),
        signal_sufficiency_path=getattr(summary, "signal_sufficiency_path", ""),
    )
