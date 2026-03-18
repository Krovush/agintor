from __future__ import annotations

import contextlib
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from .benchmarks import BenchmarkSuite, build_demo_suite
from .evolution import EvolutionEngine
from .goal_rubric import derive_goal_expectations
from .project import init_runtime
from .providers import ModelProvider
from .pydantic_compat import model_copy
from .runtime_loader import (
    RUNTIME_ABI_VERSION,
    RUNTIME_EXPORT_BUNDLE_FILE,
    RUNTIME_PROVENANCE_BUNDLE_FILE,
    load_runtime,
    runtime_identity_inputs,
)
from .runtime_profile import RUNTIME_PROFILE_FILE, RuntimeProfile, load_runtime_profile, profile_to_json
from .schemas import ArchiveEntry, ArchiveRecord
from .utils import ensure_directory, file_digest, stable_hash


@dataclass
class BuiltRuntimeResult:
    goal_prompt: str
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
    summary_path: str


def _canonical_goal_prompt(goal_prompt: str) -> str:
    return " ".join(goal_prompt.split()).strip()


def _goal_task_id(goal_prompt: str) -> str:
    canonical_goal = _canonical_goal_prompt(goal_prompt)
    return f"goal.capability.{stable_hash(canonical_goal)[:10]}"


def _goal_task_clone_id(goal_prompt: str, source_task_id: str) -> str:
    canonical_goal = _canonical_goal_prompt(goal_prompt)
    return f"{_goal_task_id(canonical_goal)}.{stable_hash(canonical_goal, source_task_id)[:8]}"


def _goal_score_keys(goal_prompt: str, suite: BenchmarkSuite) -> list[str]:
    prefix = f"{_goal_task_id(goal_prompt)}."
    return sorted(
        f"s:{task.task_id}"
        for task in suite.train
        if task.task_id.startswith(prefix)
    )


def build_goal_conditioned_suite(goal_prompt: str, profile: RuntimeProfile) -> BenchmarkSuite:
    del profile
    clean_goal = _canonical_goal_prompt(goal_prompt)
    if not clean_goal:
        raise ValueError("goal prompt may not be empty")
    suite = build_demo_suite()
    goal_expected = derive_goal_expectations(clean_goal)
    goal_tasks = []
    for family in goal_expected["target_families"]:
        family_tasks = suite.representative_family_tasks(str(family), partition="train", limit=1)
        if not family_tasks:
            continue
        source_task = family_tasks[0]
        goal_tasks.append(
            model_copy(
                source_task,
                deep=True,
                update={
                    "task_id": _goal_task_clone_id(clean_goal, source_task.task_id),
                    "prompt": f"{source_task.prompt}\n\nGoal emphasis: {clean_goal}",
                    "metadata": {
                        **source_task.metadata,
                        "goal_conditioned": True,
                        "goal_prompt": clean_goal,
                        "goal_keywords": goal_expected["goal_keywords"],
                        "target_families": goal_expected["target_families"],
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
        return float("-inf")
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


def _select_export_leader(
    engine: EvolutionEngine,
    candidates: list[ArchiveRecord],
    goal_keys: list[str],
) -> tuple[ArchiveRecord, float, float]:
    best_goal_score = max((_mean_goal_score(record.entry.scores, goal_keys) for record in candidates), default=float("-inf"))
    top_goal_candidates = [
        record for record in candidates if _mean_goal_score(record.entry.scores, goal_keys) == best_goal_score
    ]
    if not top_goal_candidates:
        top_goal_candidates = list(candidates)
    ranked_candidates: list[tuple[tuple[float, float, int, str], ArchiveRecord]] = []
    for record in top_goal_candidates:
        evaluation = engine.evaluator.evaluate_validation(Path(record.runtime_dir))
        val_score = evaluation.objective_scores.get("sbar:global", float("-inf"))
        rank = (best_goal_score, val_score, -record.entry.mutable_loc, record.entry.runtime_hash)
        ranked_candidates.append((rank, record))
    best_rank, leader = max(ranked_candidates, key=lambda item: item[0])
    return leader, best_goal_score, best_rank[1]


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
    clean_goal = _canonical_goal_prompt(goal_prompt)
    if not clean_goal:
        raise ValueError("goal prompt may not be empty")
    destination_path = Path(destination)
    build_root = ensure_directory(Path(workspace) / f"build_{stable_hash(clean_goal)[:8]}")
    seed_runtime_dir = build_root / "seed_runtime"
    init_runtime(seed_runtime_dir, force=True)
    merged_profile = load_runtime_profile(seed_runtime_dir, profile_path=profile_path)
    (seed_runtime_dir / RUNTIME_PROFILE_FILE).write_text(profile_to_json(merged_profile), encoding="utf-8")
    suite = build_goal_conditioned_suite(clean_goal, merged_profile)
    goal_keys = _goal_score_keys(clean_goal, suite)
    engine = EvolutionEngine(
        suite,
        build_root / "evolution",
        provider,
        seed_runtime_dir,
        mutator_type=mutator_type,
        reference_runtime_dir=seed_runtime_dir,
        runtime_backend=runtime_backend,
        runtime_profile=merged_profile,
        profile_path=Path(profile_path) if profile_path is not None else None,
        retain_artifacts=retain_artifacts,
    )
    summary = engine.run(steps=steps)
    candidates = _export_candidate_records(engine, goal_keys)
    if not candidates:
        raise RuntimeError("runtime build produced no archive candidates")
    leader, best_goal_score, best_val_score = _select_export_leader(engine, candidates, goal_keys)
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
    with _without_bytecode_writes():
        exported_runtime = load_runtime(destination_path, runtime_profile=merged_profile)
    runtime_hash = exported_runtime.runtime_hash
    code_hash = exported_runtime.code_hash
    manifest_version = exported_runtime.manifest.version
    runtime_id = exported_runtime.manifest.runtime_id
    identity_inputs = runtime_identity_inputs(destination_path, runtime_profile=merged_profile)
    export_bundle = {
        "runtime_abi": RUNTIME_ABI_VERSION,
        "runtime_hash": runtime_hash,
        "code_hash": code_hash,
        "manifest_version": manifest_version,
        "runtime_id": runtime_id,
        "runtime_provider": merged_profile.runtime_provider.name,
        "source_runtime_dir": str(leader.runtime_dir),
        "selection_policy": "goal_score_mean_then_validation",
        "export_bundle_file": RUNTIME_EXPORT_BUNDLE_FILE,
        "provenance_bundle_file": RUNTIME_PROVENANCE_BUNDLE_FILE,
    }
    (destination_path / RUNTIME_EXPORT_BUNDLE_FILE).write_text(
        json.dumps(export_bundle, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    _write_runtime_provenance_bundle(
        destination_path,
        runtime_hash=runtime_hash,
        code_hash=code_hash,
        runtime_provider=merged_profile.runtime_provider.name,
        source_runtime_dir=str(leader.runtime_dir),
        goal_prompt=clean_goal,
        runtime_identity=identity_inputs,
    )
    summary_path = build_root / "build_summary.json"
    payload = {
        "goal_prompt": clean_goal,
        "goal_task_ids": [key[2:] for key in goal_keys],
        "output_runtime_dir": str(destination_path),
        "workspace": str(build_root),
        "agintor_provider": getattr(provider, "provider_name", provider.__class__.__name__.lower()),
        "runtime_provider": merged_profile.runtime_provider.name,
        "mutator_type": mutator_type,
        "best_train_score": summary.best_train_score,
        "best_goal_score": best_goal_score,
        "best_val_score": best_val_score,
        "archive_cells": summary.archive_cells,
        "accepted_mutations": summary.accepted,
        "history_path": summary.history_path,
        "leader_runtime_dir": str(leader.runtime_dir),
        "leader_runtime_hash": leader.entry.runtime_hash,
        "selection_policy": "goal_score_mean_then_validation",
        "runtime_abi": RUNTIME_ABI_VERSION,
        "export_bundle_file": RUNTIME_EXPORT_BUNDLE_FILE,
        "provenance_bundle_file": RUNTIME_PROVENANCE_BUNDLE_FILE,
    }
    summary_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return BuiltRuntimeResult(
        goal_prompt=clean_goal,
        output_runtime_dir=str(destination_path),
        workspace=str(build_root),
        agintor_provider=getattr(provider, "provider_name", provider.__class__.__name__.lower()),
        runtime_provider=merged_profile.runtime_provider.name,
        mutator_type=mutator_type,
        best_train_score=summary.best_train_score,
        best_goal_score=best_goal_score,
        best_val_score=best_val_score,
        archive_cells=summary.archive_cells,
        accepted_mutations=summary.accepted,
        summary_path=str(summary_path),
    )
