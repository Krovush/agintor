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


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if hasattr(value, "dict") or hasattr(value, "model_dump"):
        return _jsonable((value).model_dump())
    return value


def _write_json(path: Path, payload: Any) -> Path:
    ensure_directory(path.parent)
    path.write_text(json.dumps(_jsonable(payload), indent=2, sort_keys=True), encoding="utf-8")
    return path


def _persist_model(path: Path, model_cls: type[Any], value: Any):
    _write_json(path, value)
    return (model_cls).model_validate(json.loads(path.read_text(encoding="utf-8")))


def _load_template_manifest() -> RuntimeManifest:
    template_root = baseline_template_dir()
    manifest_path = template_root / "runtime_manifest.json"
    return (RuntimeManifest).model_validate(json.loads(manifest_path.read_text(encoding="utf-8")))


@contextlib.contextmanager
def _without_bytecode_writes():
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        yield
    finally:
        sys.dont_write_bytecode = previous


def _write_export_snapshot(path: Path, payload: Any) -> Path:
    _write_json(path, payload)
    return path


def _runtime_relative_path(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _validate_exported_runtime(
    destination_path: Path,
    *,
    build_id: str,
    goal_id: str,
    runtime_id: str,
    runtime_hash: str,
    runtime_backend: str,
    runtime_profile: RuntimeProfile,
    export_dir: Path,
) -> None:
    """Validate the exported runtime boundary."""
    export_host = RuntimeHost(
        export_dir / ".runtime_host",
        runtime_backend=runtime_backend,
        artifact_mode=ArtifactMode.NONE,
    )
    export_host.inspect(destination_path)
    validation_request = runtime_solve_request_for_user_request(
        runtime_backend=runtime_backend,
        seed=0,
        solve_request=load_solve_request(
            prompt="Given the numbers [2, 3, 5], compute the sum and product and return JSON with keys sum and product."
        ),
    )
    validation_response = export_host.solve(
        destination_path,
        validation_request,
        provider=LocalDeterministicProvider(),
        runtime_profile=runtime_profile,
    )
    solve_result = validation_response.solve_result
    observed_model_calls = int(solve_result.budget.get("model_calls", 0) or 0)
    if not solve_result.verified or solve_result.artifact != {"sum": 10, "product": 30}:
        raise RuntimeError("exported runtime failed deterministic prompt-mode solve validation")
    if observed_model_calls != 0:
        raise RuntimeError("exported runtime deterministic prompt-mode validation unexpectedly used model calls")


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


def _write_seed_runtime(
    seed_runtime_dir: Path,
    runtime_plan: RuntimePlan,
    *,
    seed_source: Path | None = None,
    runtime_profile: RuntimeProfile | None = None,
    runtime_backend: str | None = None,
) -> None:
    if seed_source is not None and seed_source.exists() and seed_source.is_dir():
        if seed_runtime_dir.exists():
            shutil.rmtree(seed_runtime_dir)
        shutil.copytree(
            seed_source,
            seed_runtime_dir,
            ignore=_seed_runtime_ignore(seed_source),
        )
    else:
        init_runtime(seed_runtime_dir, force=True)
    _write_json(seed_runtime_dir / RUNTIME_PROFILE_FILE, runtime_plan.runtime_profile)
    _write_json(seed_runtime_dir / DEPLOYMENT_CONTRACT_FILE, runtime_plan.deployment_contract)
    bundle_runtime_kernel(seed_runtime_dir, force=True)
    if runtime_profile is not None:
        with _without_bytecode_writes():
            load_runtime(
                seed_runtime_dir,
                runtime_profile=runtime_profile,
                runtime_backend=runtime_backend or "local",
            )


def _seed_runtime_ignore(seed_source: Path):
    ignored_names = {"__pycache__", KERNEL_BUNDLE_DIR, CHAT_DIR_NAME, ".runtime_sessions"}
    allowed_root_names = _seed_runtime_root_names(seed_source)

    def ignore(directory: str, names: list[str]) -> set[str]:
        path = Path(directory)
        ignored = {
            name
            for name in names
            if name in ignored_names or name.endswith((".pyc", ".pyo"))
        }
        if path.resolve() == seed_source.resolve():
            ignored.update(name for name in names if name not in allowed_root_names and name not in ignored)
        return ignored

    return ignore


def _seed_runtime_root_names(seed_source: Path) -> set[str]:
    names = {
        "runtime_manifest.json",
        RUNTIME_PROFILE_FILE,
        DEPLOYMENT_CONTRACT_FILE,
    }
    manifest_path = seed_source / "runtime_manifest.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return names
    for value in list(payload.get("mutable_files", [])) + list(payload.get("immutable_manifest", [])):
        first_part = Path(str(value)).parts[0] if str(value).strip() else ""
        if first_part and first_part != KERNEL_BUNDLE_DIR:
            names.add(first_part)
    return names


def _replace_runtime_destination(
    source_runtime_dir: Path,
    destination_path: Path,
    *,
    force: bool,
    preserve_names: Iterable[str] = (),
) -> None:
    _reject_nested_runtime_replacement(source_runtime_dir, destination_path)
    preserved: list[tuple[str, Path, bool]] = []
    preserve_root = destination_path.parent / f".agintor-preserve-{stable_hash(destination_path, now_ts())[:12]}"
    try:
        if destination_path.exists():
            if not destination_path.is_dir():
                if not force:
                    raise FileExistsError(f"destination {destination_path} already exists")
                destination_path.unlink()
            else:
                visible_entries = [entry for entry in destination_path.iterdir()]
                if visible_entries and not force:
                    raise FileExistsError(f"destination {destination_path} already exists")
                if force:
                    ensure_directory(preserve_root)
                    preserve_name_set = set(preserve_names)
                    for candidate in list(destination_path.iterdir()):
                        name = candidate.name
                        target = preserve_root / name
                        if target.exists():
                            shutil.rmtree(target) if target.is_dir() else target.unlink()
                        shutil.move(str(candidate), str(target))
                        preserved.append((name, target, name in preserve_name_set))
                    shutil.rmtree(destination_path)
                else:
                    destination_path.rmdir()
        shutil.copytree(
            source_runtime_dir,
            destination_path,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
        for name, preserved_path, replace_existing in preserved:
            target = destination_path / name
            if target.exists() and not replace_existing:
                continue
            if target.exists():
                shutil.rmtree(target) if target.is_dir() else target.unlink()
            shutil.move(str(preserved_path), str(target))
    finally:
        if preserve_root.exists():
            shutil.rmtree(preserve_root, ignore_errors=True)


def _reject_nested_runtime_replacement(source_runtime_dir: Path, destination_path: Path) -> None:
    source = source_runtime_dir.resolve()
    destination = destination_path.resolve()
    if source == destination or source.is_relative_to(destination) or destination.is_relative_to(source):
        raise ValueError(
            f"refusing to replace runtime destination {destination_path} from nested source {source_runtime_dir}"
        )


def _score_rows_for_candidates(
    engine: EvolutionEngine,
    candidates: list[ArchiveRecord],
    goal_keys: list[str],
) -> list[dict[str, Any]]:
    goal_scores = {
        record.entry.runtime_hash: _mean_goal_score(record.entry.scores, goal_keys)
        for record in candidates
    }
    validation_scores: dict[str, float | None] = {}
    validation_errors: dict[str, str] = {}
    winning_goal_score: float | None = None
    grouped_candidates: dict[float, list[ArchiveRecord]] = {}
    for record in candidates:
        grouped_candidates.setdefault(goal_scores[record.entry.runtime_hash], []).append(record)
    for goal_score in sorted(grouped_candidates, reverse=True):
        successful_validations = False
        for record in grouped_candidates[goal_score]:
            runtime_hash = record.entry.runtime_hash
            try:
                validation = engine.evaluator.evaluate_validation(Path(record.runtime_dir))
            except Exception as exc:
                validation_errors[runtime_hash] = str(exc)
                continue
            validation_scores[runtime_hash] = validation.objective_scores.get("sbar:global", float("-inf"))
            successful_validations = True
        if successful_validations:
            winning_goal_score = goal_score
            break
    rows: list[dict[str, Any]] = []
    for record in candidates:
        runtime_hash = record.entry.runtime_hash
        export_eligible = (
            winning_goal_score is not None
            and goal_scores[runtime_hash] == winning_goal_score
            and runtime_hash in validation_scores
        )
        rows.append(
            {
                "runtime_hash": runtime_hash,
                "runtime_dir": record.runtime_dir,
                "goal_score": goal_scores[runtime_hash],
                "validation_score": validation_scores.get(runtime_hash),
                "validation_evaluated": runtime_hash in validation_scores or runtime_hash in validation_errors,
                "validation_error": validation_errors.get(runtime_hash),
                "export_eligible": export_eligible,
                "train_score": record.entry.scores.get("sbar:global", float("-inf")),
                "mutable_loc": record.entry.mutable_loc,
                "scope_tag": record.entry.scope_tag,
                "behavior_bin": list(record.entry.behavior_bin),
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            0 if row["export_eligible"] else 1,
            -row["goal_score"],
            0 if row["validation_score"] is None else -row["validation_score"],
            -row["train_score"],
            row["mutable_loc"],
            row["runtime_hash"],
        ),
    )


def _persist_benchmark_suite(path: Path, suite: BenchmarkSuite) -> BenchmarkSuite:
    payload = {
        "suite_id": f"benchmark-suite.{suite.name}",
        "name": suite.name,
        "train": [(task).model_dump() for task in suite.train],
        "val": [(task).model_dump() for task in suite.val],
        "test": [(task).model_dump() for task in suite.test],
        "proxy": [(task).model_dump() for task in suite.proxy],
    }
    _write_json(path, payload)
    reloaded = json.loads(path.read_text(encoding="utf-8"))
    return BenchmarkSuite(
        name=reloaded["name"],
        train=[(type(suite.train[0])).model_validate(item) for item in reloaded["train"]] if suite.train else [],
        val=[(type(suite.val[0])).model_validate(item) for item in reloaded["val"]] if suite.val else [],
        test=[(type(suite.test[0])).model_validate(item) for item in reloaded["test"]] if suite.test else [],
        proxy=[(type(suite.proxy[0])).model_validate(item) for item in reloaded["proxy"]] if suite.proxy else [],
    )
