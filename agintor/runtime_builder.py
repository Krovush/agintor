from __future__ import annotations

import contextlib
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .benchmarks import BenchmarkSuite, build_demo_suite
from .evolution import EvolutionEngine
from .goal_rubric import build_goal_spec, build_success_criteria_bundle, canonical_goal_prompt
from .project import baseline_template_dir, init_runtime
from .providers import ModelProvider
from .pydantic_compat import model_copy, model_dump, model_validate
from .runtime_loader import (
    DEPLOYMENT_CONTRACT_FILE,
    RUNTIME_ABI_VERSION,
    RUNTIME_EXPORT_BUNDLE_FILE,
    RUNTIME_PROVENANCE_BUNDLE_FILE,
    load_runtime,
    runtime_identity_inputs,
)
from .runtime_profile import (
    RUNTIME_PROFILE_FILE,
    RuntimeProfile,
    factory_profile_payload,
    load_runtime_profile,
    profile_to_json,
    runtime_profile_payload,
)
from .schemas import (
    ArchiveEntry,
    ArchiveRecord,
    BenchmarkPlan,
    BuildSummary,
    DeploymentContract,
    FactoryProfile,
    GoalSpec,
    RuntimeManifest,
    RuntimePlan,
    SuccessCriteriaBundle,
    VerifierBundle,
    VerifierSpec,
)
from .utils import ensure_directory, file_digest, stable_hash


@dataclass
class BuiltRuntimeResult:
    build_id: str
    goal_id: str
    goal_prompt: str
    goal_spec_path: str
    success_criteria_path: str
    benchmark_plan_path: str
    verifier_bundle_path: str
    runtime_plan_path: str
    output_runtime_dir: str
    workspace: str
    agintor_provider: str
    runtime_provider: str
    mutator_type: str
    best_train_score: float
    best_goal_score: float
    best_val_score: float
    archive_cells: int
    accepted_mutations: int
    export_bundle_file: str
    provenance_bundle_file: str
    export_summary_path: str
    summary_path: str


def _goal_task_id(goal_input: GoalSpec | str) -> str:
    goal_text = goal_input.normalized_goal if isinstance(goal_input, GoalSpec) else canonical_goal_prompt(goal_input)
    return f"goal.capability.{stable_hash(goal_text)[:10]}"


def _goal_task_clone_id(goal_input: GoalSpec | str, source_task_id: str) -> str:
    return f"{_goal_task_id(goal_input)}.{stable_hash(_goal_task_id(goal_input), source_task_id)[:8]}"


def _goal_score_keys(goal_input: GoalSpec | str, suite: BenchmarkSuite) -> list[str]:
    prefix = f"{_goal_task_id(goal_input)}."
    return sorted(
        f"s:{task.task_id}"
        for task in suite.train
        if task.task_id.startswith(prefix)
    )


def build_goal_conditioned_suite(goal_input: GoalSpec | str, profile: RuntimeProfile) -> BenchmarkSuite:
    goal_spec = goal_input if isinstance(goal_input, GoalSpec) else build_goal_spec(goal_input, runtime_provider_name=profile.runtime_provider.name)
    clean_goal = goal_spec.normalized_goal
    if not clean_goal:
        raise ValueError("goal prompt may not be empty")
    suite = build_demo_suite()
    goal_tasks = []
    for family in goal_spec.target_families:
        family_tasks = suite.representative_family_tasks(str(family), partition="train", limit=1)
        if not family_tasks:
            continue
        source_task = family_tasks[0]
        goal_tasks.append(
            model_copy(
                source_task,
                deep=True,
                update={
                    "task_id": _goal_task_clone_id(goal_spec, source_task.task_id),
                    "prompt": f"{source_task.prompt}\n\nGoal emphasis: {clean_goal}",
                    "metadata": {
                        **source_task.metadata,
                        "goal_conditioned": True,
                        "goal_id": goal_spec.goal_id,
                        "goal_prompt": clean_goal,
                        "goal_keywords": goal_spec.goal_keywords,
                        "goal_phrases": goal_spec.goal_phrases,
                        "target_families": goal_spec.target_families,
                        "source_task_id": source_task.task_id,
                    },
                },
            )
        )
    return BenchmarkSuite(
        name=f"goal_conditioned_{stable_hash(clean_goal)[:8]}",
        train=[*suite.train, *goal_tasks],
        val=list(suite.val),
        test=list(suite.test),
        proxy=list(suite.proxy),
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if hasattr(value, "dict") or hasattr(value, "model_dump"):
        return _jsonable(model_dump(value))
    return value


def _write_json(path: Path, payload: Any) -> Path:
    ensure_directory(path.parent)
    path.write_text(json.dumps(_jsonable(payload), indent=2, sort_keys=True), encoding="utf-8")
    return path


def _persist_model(path: Path, model_cls: type[Any], value: Any):
    _write_json(path, value)
    return model_validate(model_cls, json.loads(path.read_text(encoding="utf-8")))


def _load_template_manifest() -> RuntimeManifest:
    template_root = baseline_template_dir()
    manifest_path = template_root / "runtime_manifest.json"
    return model_validate(RuntimeManifest, json.loads(manifest_path.read_text(encoding="utf-8")))


def _runtime_file_digests(runtime_dir: Path) -> dict[str, str]:
    skip_names = {"__pycache__", RUNTIME_PROVENANCE_BUNDLE_FILE}
    skip_suffixes = (".pyc", ".pyo")
    digests: dict[str, str] = {}
    for path in sorted(runtime_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(runtime_dir).as_posix()
        if any(part in skip_names for part in Path(rel).parts):
            continue
        if rel.endswith(skip_suffixes):
            continue
        digests[rel] = file_digest(path)
    return digests


@contextlib.contextmanager
def _without_bytecode_writes():
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        yield
    finally:
        sys.dont_write_bytecode = previous


def _write_runtime_provenance_bundle(
    destination_path: Path,
    *,
    runtime_hash: str,
    code_hash: str,
    runtime_provider: str,
    source_runtime_dir: str,
    goal_prompt: str,
    runtime_identity: dict[str, dict[str, str]],
) -> None:
    artifact_file_digests = _runtime_file_digests(destination_path)
    payload = {
        "schema_version": "agintor.runtime.provenance.v1",
        "runtime_abi": RUNTIME_ABI_VERSION,
        "runtime_hash": runtime_hash,
        "code_hash": code_hash,
        "runtime_provider": runtime_provider,
        "source_runtime_dir": source_runtime_dir,
        "goal_prompt": goal_prompt,
        "runtime_identity_inputs": runtime_identity,
        "artifact_file_digests": artifact_file_digests,
    }
    payload["attestation_hash"] = stable_hash(payload)
    (destination_path / RUNTIME_PROVENANCE_BUNDLE_FILE).write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _mean_goal_score(scores: dict[str, float], goal_keys: list[str]) -> float:
    if not goal_keys:
        return scores.get("sbar:global", float("-inf"))
    values = [scores.get(key, float("-inf")) for key in goal_keys]
    if any(value == float("-inf") for value in values):
        return float("-inf")
    return sum(values) / len(values)


def _export_candidate_records(engine: EvolutionEngine, goal_keys: list[str]) -> list[ArchiveRecord]:
    runtime_dirs = getattr(engine.archive, "runtime_dirs", None)
    runtime_evaluations = getattr(engine.archive, "runtime_evaluations", None)
    runtime_descriptors = getattr(engine.archive, "runtime_descriptors", None)
    if isinstance(runtime_dirs, dict) and isinstance(runtime_evaluations, dict) and isinstance(runtime_descriptors, dict):
        candidates: list[ArchiveRecord] = []
        for runtime_hash, runtime_dir in runtime_dirs.items():
            evaluation = runtime_evaluations.get(runtime_hash)
            descriptor = runtime_descriptors.get(runtime_hash)
            if evaluation is None or descriptor is None:
                continue
            candidates.append(
                ArchiveRecord(
                    objective="build",
                    key=runtime_hash,
                    entry=ArchiveEntry(
                        code_hash=descriptor.code_hash,
                        runtime_hash=runtime_hash,
                        scores=evaluation.objective_scores,
                        behavior_bin=list(descriptor.behavior_bin),
                        scope_tag=descriptor.scope_tag,
                        complexity_bucket=descriptor.complexity_bucket,
                        mutable_loc=descriptor.mutable_loc,
                        trace_refs=[],
                    ),
                    runtime_dir=str(runtime_dir),
                )
            )
        return candidates
    objective_names: list[str] = []
    for objective_name in [*goal_keys, "sbar:global"]:
        if objective_name not in objective_names:
            objective_names.append(objective_name)
    deduped: dict[str, ArchiveRecord] = {}
    for objective_name in objective_names:
        for record in engine.archive.island(objective_name):
            deduped.setdefault(record.entry.runtime_hash, record)
    return list(deduped.values())


def _tooling_scope_from_suite(suite: BenchmarkSuite) -> list[str]:
    scope: set[str] = set()
    for task in [*suite.train, *suite.proxy]:
        for operation in task.operations:
            if operation.tool_hint:
                scope.add("/".join(operation.tool_hint.split("/")[:2]))
            if operation.kind == "generated_expression":
                scope.add("generated/local")
    return sorted(scope)


def _required_runtime_env_names(profile: RuntimeProfile) -> list[str]:
    env_names: list[str] = []
    api_key_env = str(profile.runtime_provider.api_key_env or "").strip()
    if api_key_env:
        env_names.append(api_key_env)
    return env_names


def _build_benchmark_plan(goal_spec: GoalSpec, suite: BenchmarkSuite) -> BenchmarkPlan:
    verifier_bundle_id = f"verifier.{stable_hash(goal_spec.goal_id, suite.name, [task.task_id for task in suite.train])[:12]}"
    synthetic_task_ids = [
        task.task_id
        for task in suite.train
        if task.metadata.get("goal_conditioned") is True
    ]
    return BenchmarkPlan(
        plan_id=f"plan.{stable_hash(goal_spec.goal_id, suite.name)[:12]}",
        goal_id=goal_spec.goal_id,
        family_targets=list(goal_spec.target_families),
        train_task_ids=[task.task_id for task in suite.train],
        proxy_task_ids=[task.task_id for task in suite.proxy],
        val_task_ids=[task.task_id for task in suite.val],
        test_task_ids=[task.task_id for task in suite.test],
        synthetic_task_ids=synthetic_task_ids,
        verifier_bundle_id=verifier_bundle_id,
        frozen=True,
    )


def _verifier_tolerance(verifier_type: str) -> float:
    if verifier_type in {"json_numeric", "number_exact"}:
        return 1e-9
    return 0.0


def _build_verifier_bundle(plan: BenchmarkPlan, suite: BenchmarkSuite) -> VerifierBundle:
    task_index = {
        task.task_id: task
        for task in [*suite.train, *suite.proxy, *suite.val, *suite.test]
    }
    ordered_task_ids = [
        *plan.train_task_ids,
        *plan.proxy_task_ids,
        *plan.val_task_ids,
        *plan.test_task_ids,
    ]
    verifiers: list[VerifierSpec] = []
    seen: set[str] = set()
    for task_id in ordered_task_ids:
        if task_id in seen or task_id not in task_index:
            continue
        seen.add(task_id)
        task = task_index[task_id]
        verifiers.append(
            VerifierSpec(
                verifier_id=f"{plan.verifier_bundle_id}.{stable_hash(task_id)[:8]}",
                verifier_type=task.verifier_type,
                artifact_contract={
                    "task_id": task.task_id,
                    "task_type": task.task_type,
                    "output_keys": [operation.output_key for operation in task.operations],
                },
                tolerance=_verifier_tolerance(task.verifier_type),
                uses_trace=task.verifier_type.startswith("trace"),
                local_only=True,
                expected_signal=f"{task.task_type}:{task.verifier_type}",
            )
        )
    return VerifierBundle(
        bundle_id=plan.verifier_bundle_id,
        plan_id=plan.plan_id,
        verifiers=verifiers,
        checker_chain_defaults=["local", "subtree", "repo", "benchmark"],
        frozen=True,
        created_from={
            "goal_id": plan.goal_id,
            "family_targets": list(plan.family_targets),
            "synthetic_task_ids": list(plan.synthetic_task_ids),
        },
    )


def _build_deployment_contract(goal_spec: GoalSpec, profile: RuntimeProfile) -> DeploymentContract:
    notes = list(goal_spec.assumptions)
    if profile.runtime_provider.api_key_file_env:
        notes.append(
            f"{profile.runtime_provider.api_key_file_env} may be used as a key-file alternative for the default runtime provider."
        )
    return DeploymentContract(
        entry_command='agintor solve <runtime_dir> --prompt "<request>"',
        runtime_abi=RUNTIME_ABI_VERSION,
        python_version=">=3.11",
        supported_backends=list(goal_spec.deployment_preferences.get("supported_backends", ["local", "docker"])),
        required_env_names=_required_runtime_env_names(profile),
        network_policy=str(goal_spec.constraints.get("network_policy", "provider-only")),
        filesystem_policy=str(goal_spec.constraints.get("filesystem_policy", "workspace-read-write")),
        notes=notes,
    )


def _build_factory_profile(
    profile: RuntimeProfile,
    *,
    agintor_provider: str,
    runtime_backend: str,
    mutator_type: str,
    goal_spec: GoalSpec,
    benchmark_plan: BenchmarkPlan,
    goal_keys: Iterable[str],
) -> FactoryProfile:
    sections = factory_profile_payload(profile)
    return FactoryProfile(
        agintor_provider=agintor_provider,
        evaluation=dict(sections.get("evaluation", {})),
        evolution=dict(sections.get("evolution", {})),
        mutation={
            "mutator_type": mutator_type,
            "prompt_id": sections.get("prompts", {}).get("mutation_patch", ""),
        },
        benchmark_generation={
            "suite": "demo",
            "strategy": "goal_conditioned_demo_clone",
            "family_targets": list(goal_spec.target_families),
            "synthetic_task_ids": list(benchmark_plan.synthetic_task_ids),
        },
        leader_selection={
            "policy": "goal_score_mean_then_validation",
            "goal_score_keys": list(goal_keys),
            "validation_partition": "val",
            "validation_seeds": list(profile.evaluation.validation_seeds),
        },
        runtime_backend=runtime_backend,
    )


def _build_runtime_plan(
    goal_spec: GoalSpec,
    suite: BenchmarkSuite,
    benchmark_plan: BenchmarkPlan,
    verifier_bundle: VerifierBundle,
    profile: RuntimeProfile,
    *,
    agintor_provider: str,
    runtime_backend: str,
) -> RuntimePlan:
    del verifier_bundle
    manifest = _load_template_manifest()
    deployment_contract = _build_deployment_contract(goal_spec, profile)
    return RuntimePlan(
        plan_id=f"runtime.{stable_hash(goal_spec.goal_id, benchmark_plan.plan_id)[:12]}",
        goal_id=goal_spec.goal_id,
        runtime_abi=RUNTIME_ABI_VERSION,
        seed_template=str(baseline_template_dir()),
        mutable_files=list(manifest.mutable_files),
        immutable_manifest=list(manifest.immutable_manifest),
        runtime_profile=runtime_profile_payload(profile),
        provider_plan={
            "agintor_provider": {"name": agintor_provider},
            "runtime_provider": {
                "name": profile.runtime_provider.name,
                "api_key_env": profile.runtime_provider.api_key_env,
                "api_key_file_env": profile.runtime_provider.api_key_file_env,
                "model_map": dict(profile.runtime_provider.model_map),
            },
            "factory_runtime_backend": runtime_backend,
        },
        tooling_scope=_tooling_scope_from_suite(suite),
        deployment_contract=deployment_contract,
    )


def _write_seed_runtime(seed_runtime_dir: Path, runtime_plan: RuntimePlan, profile: RuntimeProfile) -> None:
    init_runtime(seed_runtime_dir, force=True)
    (seed_runtime_dir / RUNTIME_PROFILE_FILE).write_text(
        profile_to_json(profile, runtime_only=True),
        encoding="utf-8",
    )
    _write_json(seed_runtime_dir / DEPLOYMENT_CONTRACT_FILE, runtime_plan.deployment_contract)


def _score_rows_for_candidates(
    engine: EvolutionEngine,
    candidates: list[ArchiveRecord],
    goal_keys: list[str],
) -> list[dict[str, Any]]:
    goal_scores = {
        record.entry.runtime_hash: _mean_goal_score(record.entry.scores, goal_keys)
        for record in candidates
    }
    best_goal_score = max(goal_scores.values(), default=float("-inf"))
    top_goal_hashes = {
        runtime_hash
        for runtime_hash, score in goal_scores.items()
        if score == best_goal_score
    }
    validation_scores: dict[str, float] = {}
    for record in candidates:
        if record.entry.runtime_hash not in top_goal_hashes:
            continue
        validation = engine.evaluator.evaluate_validation(Path(record.runtime_dir))
        validation_scores[record.entry.runtime_hash] = validation.objective_scores.get("sbar:global", float("-inf"))
    rows: list[dict[str, Any]] = []
    for record in candidates:
        rows.append(
            {
                "runtime_hash": record.entry.runtime_hash,
                "runtime_dir": record.runtime_dir,
                "goal_score": goal_scores[record.entry.runtime_hash],
                "validation_score": validation_scores.get(record.entry.runtime_hash),
                "validation_evaluated": record.entry.runtime_hash in validation_scores,
                "train_score": record.entry.scores.get("sbar:global", float("-inf")),
                "mutable_loc": record.entry.mutable_loc,
                "scope_tag": record.entry.scope_tag,
                "behavior_bin": list(record.entry.behavior_bin),
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            -row["goal_score"],
            0 if row["validation_score"] is None else -row["validation_score"],
            -row["train_score"],
            row["mutable_loc"],
            row["runtime_hash"],
        ),
    )


def build_runtime_from_goal(
    goal_prompt: str,
    *,
    destination: str | Path,
    workspace: str | Path,
    provider: ModelProvider,
    steps: int = 10,
    mutator_type: str = "heuristic",
    profile_path: str | Path | None = None,
    runtime_backend: str | None = None,
    force: bool = False,
    retain_artifacts: bool = True,
) -> BuiltRuntimeResult:
    clean_goal = canonical_goal_prompt(goal_prompt)
    if not clean_goal:
        raise ValueError("goal prompt may not be empty")
    build_id = f"build.{stable_hash(clean_goal)[:12]}"
    destination_path = Path(destination)
    build_root = ensure_directory(Path(workspace) / f"build_{stable_hash(clean_goal)[:8]}")
    goal_dir = ensure_directory(build_root / "goal")
    planning_dir = ensure_directory(build_root / "planning")
    export_dir = ensure_directory(build_root / "export")
    evolution_dir = ensure_directory(build_root / "evolution")
    seed_runtime_dir = build_root / "seed_runtime"
    agintor_provider = getattr(provider, "provider_name", provider.__class__.__name__.lower())
    effective_runtime_backend = (runtime_backend or "local").strip().lower()
    merged_profile = load_runtime_profile(profile_path=profile_path)

    goal_spec = _persist_model(
        goal_dir / "goal_spec.json",
        GoalSpec,
        build_goal_spec(
            clean_goal,
            runtime_provider_name=merged_profile.runtime_provider.name,
            default_runtime_backend=effective_runtime_backend,
        ),
    )
    success_bundle = _persist_model(
        goal_dir / "success_criteria.json",
        SuccessCriteriaBundle,
        build_success_criteria_bundle(goal_spec),
    )
    goal_suite = build_goal_conditioned_suite(goal_spec, merged_profile)
    benchmark_plan = _persist_model(
        planning_dir / "benchmark_plan.json",
        BenchmarkPlan,
        _build_benchmark_plan(goal_spec, goal_suite),
    )
    verifier_bundle = _persist_model(
        planning_dir / "verifier_bundle.json",
        VerifierBundle,
        _build_verifier_bundle(benchmark_plan, goal_suite),
    )
    goal_keys = _goal_score_keys(goal_spec, goal_suite)
    factory_profile = _persist_model(
        planning_dir / "factory_profile.json",
        FactoryProfile,
        _build_factory_profile(
            merged_profile,
            agintor_provider=agintor_provider,
            runtime_backend=effective_runtime_backend,
            mutator_type=mutator_type,
            goal_spec=goal_spec,
            benchmark_plan=benchmark_plan,
            goal_keys=goal_keys,
        ),
    )
    runtime_plan = _persist_model(
        planning_dir / "runtime_plan.json",
        RuntimePlan,
        _build_runtime_plan(
            goal_spec,
            goal_suite,
            benchmark_plan,
            verifier_bundle,
            merged_profile,
            agintor_provider=agintor_provider,
            runtime_backend=effective_runtime_backend,
        ),
    )

    _write_seed_runtime(seed_runtime_dir, runtime_plan, merged_profile)
    engine = EvolutionEngine(
        goal_suite,
        evolution_dir,
        provider,
        seed_runtime_dir,
        mutator_type=mutator_type,
        reference_runtime_dir=seed_runtime_dir,
        runtime_backend=effective_runtime_backend,
        runtime_profile=merged_profile,
        profile_path=Path(profile_path) if profile_path is not None else None,
        retain_artifacts=retain_artifacts,
    )
    summary = engine.run(steps=steps)
    candidates = _export_candidate_records(engine, goal_keys)
    if not candidates:
        raise RuntimeError("runtime build produced no archive candidates")
    leaderboard_rows = _score_rows_for_candidates(engine, candidates, goal_keys)
    leaderboard_path = _write_json(export_dir / "leaderboard.json", leaderboard_rows)
    leader_row = leaderboard_rows[0]
    leader = next(record for record in candidates if record.entry.runtime_hash == leader_row["runtime_hash"])
    best_goal_score = float(leader_row["goal_score"])
    best_val_score = float(leader_row["validation_score"])

    if destination_path.exists():
        if not force:
            raise FileExistsError(f"destination {destination_path} already exists")
        if destination_path.is_dir():
            shutil.rmtree(destination_path)
        else:
            destination_path.unlink()
    shutil.copytree(
        Path(leader.runtime_dir),
        destination_path,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    (destination_path / RUNTIME_PROFILE_FILE).write_text(
        profile_to_json(merged_profile, runtime_only=True),
        encoding="utf-8",
    )
    _write_json(destination_path / DEPLOYMENT_CONTRACT_FILE, runtime_plan.deployment_contract)
    with _without_bytecode_writes():
        exported_runtime = load_runtime(
            destination_path,
            runtime_profile=merged_profile,
            runtime_backend=effective_runtime_backend,
        )
    runtime_hash = exported_runtime.runtime_hash
    code_hash = exported_runtime.code_hash
    manifest_version = exported_runtime.manifest.version
    runtime_id = exported_runtime.manifest.runtime_id
    identity_inputs = runtime_identity_inputs(destination_path, runtime_profile=merged_profile)
    export_bundle_path = destination_path / RUNTIME_EXPORT_BUNDLE_FILE
    export_bundle = {
        "schema_version": "agintor.runtime.export.v1",
        "runtime_abi": RUNTIME_ABI_VERSION,
        "runtime_hash": runtime_hash,
        "code_hash": code_hash,
        "manifest_version": manifest_version,
        "runtime_id": runtime_id,
        "build_id": build_id,
        "goal_id": goal_spec.goal_id,
        "agintor_provider": agintor_provider,
        "runtime_provider": merged_profile.runtime_provider.name,
        "source_runtime_dir": str(leader.runtime_dir),
        "source_runtime_hash": leader.entry.runtime_hash,
        "selection_policy": "goal_score_mean_then_validation",
        "runtime_profile_file": str(destination_path / RUNTIME_PROFILE_FILE),
        "deployment_contract_file": str(destination_path / DEPLOYMENT_CONTRACT_FILE),
        "export_bundle_file": str(export_bundle_path),
        "provenance_bundle_file": str(destination_path / RUNTIME_PROVENANCE_BUNDLE_FILE),
    }
    _write_json(export_bundle_path, export_bundle)
    _write_runtime_provenance_bundle(
        destination_path,
        runtime_hash=runtime_hash,
        code_hash=code_hash,
        runtime_provider=merged_profile.runtime_provider.name,
        source_runtime_dir=str(leader.runtime_dir),
        goal_prompt=clean_goal,
        runtime_identity=identity_inputs,
    )

    export_summary_payload = {
        "build_id": build_id,
        "goal_id": goal_spec.goal_id,
        "goal_prompt": clean_goal,
        "runtime_hash": runtime_hash,
        "code_hash": code_hash,
        "source_runtime_dir": str(leader.runtime_dir),
        "source_runtime_hash": leader.entry.runtime_hash,
        "agintor_provider": agintor_provider,
        "runtime_provider": merged_profile.runtime_provider.name,
        "runtime_profile_path": str(destination_path / RUNTIME_PROFILE_FILE),
        "deployment_contract_path": str(destination_path / DEPLOYMENT_CONTRACT_FILE),
        "export_bundle_path": str(export_bundle_path),
        "provenance_bundle_path": str(destination_path / RUNTIME_PROVENANCE_BUNDLE_FILE),
        "leaderboard_path": str(leaderboard_path),
        "runtime_plan_path": str(planning_dir / "runtime_plan.json"),
    }
    export_summary_path = _write_json(export_dir / "export_summary.json", export_summary_payload)
    build_summary = _persist_model(
        export_dir / "build_summary.json",
        BuildSummary,
        BuildSummary(
            build_id=build_id,
            goal_id=goal_spec.goal_id,
            goal_prompt=clean_goal,
            goal_task_ids=[key[2:] for key in goal_keys],
            goal_spec_path=str(goal_dir / "goal_spec.json"),
            success_criteria_path=str(goal_dir / "success_criteria.json"),
            benchmark_plan_path=str(planning_dir / "benchmark_plan.json"),
            verifier_bundle_path=str(planning_dir / "verifier_bundle.json"),
            runtime_plan_path=str(planning_dir / "runtime_plan.json"),
            workspace=str(build_root),
            output_runtime_dir=str(destination_path),
            history_path=getattr(summary, "history_path", ""),
            leader_runtime_hash=leader.entry.runtime_hash,
            leader_runtime_dir=str(leader.runtime_dir),
            runtime_abi=RUNTIME_ABI_VERSION,
            selection_policy="goal_score_mean_then_validation",
            best_train_score=summary.best_train_score,
            best_goal_score=best_goal_score,
            best_val_score=best_val_score,
            accepted_mutations=summary.accepted,
            archive_cells=summary.archive_cells,
            agintor_provider=agintor_provider,
            runtime_provider=merged_profile.runtime_provider.name,
            export_bundle_file=RUNTIME_EXPORT_BUNDLE_FILE,
            provenance_bundle_file=RUNTIME_PROVENANCE_BUNDLE_FILE,
            export_summary_path=str(export_summary_path),
        ),
    )
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
        workspace=str(build_root),
        agintor_provider=agintor_provider,
        runtime_provider=merged_profile.runtime_provider.name,
        mutator_type=mutator_type,
        best_train_score=summary.best_train_score,
        best_goal_score=best_goal_score,
        best_val_score=best_val_score,
        archive_cells=summary.archive_cells,
        accepted_mutations=summary.accepted,
        export_bundle_file=RUNTIME_EXPORT_BUNDLE_FILE,
        provenance_bundle_file=RUNTIME_PROVENANCE_BUNDLE_FILE,
        export_summary_path=str(export_summary_path),
        summary_path=str(export_dir / "build_summary.json"),
    )
