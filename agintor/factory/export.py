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
from ..contracts import DomainEvidenceContract, GoalSpec, sealed_benchmark_task_payload
from ..oracle.projections import public_oracle_projection
from ..runtime.langgraph.compiler import RuntimeSpecCompiler
from .runtime_specs import is_spec_backed_runtime_kind, runtime_spec_for_plan
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
    loaded_runtime = load_runtime(
        destination_path,
        runtime_backend=runtime_backend,
        runtime_profile=runtime_profile,
    )
    export_host = RuntimeHost(
        export_dir / ".runtime_host",
        runtime_backend=runtime_backend,
        artifact_mode=ArtifactMode.NONE,
    )
    export_host.inspect(destination_path)
    if getattr(loaded_runtime, "runtime_spec", None) is not None:
        validation_request = runtime_solve_request_for_user_request(
            runtime_backend=runtime_backend,
            seed=0,
            solve_request=load_solve_request(prompt="spec runtime smoke"),
        )
        validation_response = export_host.solve(
            destination_path,
            validation_request,
            provider=LocalDeterministicProvider(),
            runtime_profile=runtime_profile,
        )
        solve_result = validation_response.solve_result
        lifecycle = str(solve_result.run_lifecycle_state or solve_result.lifecycle_state or "").lower()
        if lifecycle != "completed" or solve_result.artifact != "spec runtime smoke":
            raise RuntimeError("exported spec-backed runtime failed deterministic prompt-mode solve validation")
        return
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
    objective_names: list[str] = []
    for objective_name in [*goal_keys, "sbar:global"]:
        if objective_name not in objective_names:
            objective_names.append(objective_name)
    objective_rank = {name: index for index, name in enumerate(objective_names)}
    deduped: dict[str, ArchiveRecord] = {}
    capability_records = getattr(engine.archive, "archive_records", lambda _kind=None: [])("capability")
    for record in capability_records:
        if record.objective not in objective_rank:
            continue
        existing = deduped.get(record.entry.runtime_hash)
        if existing is None:
            deduped[record.entry.runtime_hash] = record
            continue
        current_rank = objective_rank.get(record.objective, len(objective_rank))
        existing_rank = objective_rank.get(existing.objective, len(objective_rank))
        if current_rank < existing_rank:
            deduped[record.entry.runtime_hash] = record
            continue
        if current_rank == existing_rank and record.entry.scores.get(record.objective, float("-inf")) > existing.entry.scores.get(existing.objective, float("-inf")):
            deduped[record.entry.runtime_hash] = record
    return list(deduped.values())


def _write_seed_runtime(
    seed_runtime_dir: Path,
    runtime_plan: RuntimePlan,
    *,
    goal_spec: GoalSpec | None = None,
    seed_source: Path | None = None,
    runtime_profile: RuntimeProfile | None = None,
    runtime_backend: str | None = None,
) -> None:
    runtime_kind = str(getattr(runtime_plan, "runtime_kind", "policy_modules") or "policy_modules")
    spec_backed = is_spec_backed_runtime_kind(runtime_kind)
    if spec_backed:
        if goal_spec is None:
            raise ValueError(f"{runtime_kind} seed runtime generation requires a GoalSpec")
        spec = runtime_spec_for_plan(runtime_plan, goal_spec)
        if spec is None:
            raise ValueError(f"{runtime_kind} seed runtime generation did not produce a runtime spec")
        RuntimeSpecCompiler().compile_to_directory(spec, seed_runtime_dir, force=True)
        manifest_path = seed_runtime_dir / "runtime_manifest.json"
        manifest = RuntimeManifest.model_validate(json.loads(manifest_path.read_text(encoding="utf-8")))
        manifest = manifest.model_copy(
            update={
                "oracle_package_hash": str(getattr(runtime_plan, "oracle_package_hash", "") or ""),
                "metadata": {
                    **dict(manifest.metadata or {}),
                    "oracle_package_hash": str(getattr(runtime_plan, "oracle_package_hash", "") or ""),
                    "oracle_public_view_hash": str(getattr(runtime_plan, "oracle_public_view_hash", "") or ""),
                },
            },
            deep=True,
        )
        _write_json(manifest_path, manifest)
        oracle_hash = str(getattr(runtime_plan, "oracle_package_hash", "") or "")
        oracle_public_ref = str(getattr(runtime_plan, "oracle_public_ref", "") or "")
        if oracle_hash and oracle_public_ref:
            public_path = Path(oracle_public_ref)
            if public_path.is_file():
                target = seed_runtime_dir / "oracle" / "public.json"
                ensure_directory(target.parent)
                shutil.copy2(public_path, target)
    elif seed_source is not None and seed_source.exists() and seed_source.is_dir():
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
    if not spec_backed:
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
            record.archive_kind == "capability"
            and winning_goal_score is not None
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
                "archive_kind": record.archive_kind,
                "promotion_type": str(getattr(record.promotion_type or record.entry.promotion_type, "value", record.promotion_type or record.entry.promotion_type or "")),
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
        "train": [sealed_benchmark_task_payload(task) for task in suite.train],
        "val": [sealed_benchmark_task_payload(task) for task in suite.val],
        "test": [sealed_benchmark_task_payload(task) for task in suite.test],
        "proxy": [sealed_benchmark_task_payload(task) for task in suite.proxy],
        "evidence_contract": suite.evidence_contract.model_dump(mode="json") if suite.evidence_contract is not None else None,
    }
    _write_json(path, payload)
    reloaded = json.loads(path.read_text(encoding="utf-8"))
    return BenchmarkSuite(
        name=reloaded["name"],
        train=[(type(suite.train[0])).model_validate(item) for item in reloaded["train"]] if suite.train else [],
        val=[(type(suite.val[0])).model_validate(item) for item in reloaded["val"]] if suite.val else [],
        test=[(type(suite.test[0])).model_validate(item) for item in reloaded["test"]] if suite.test else [],
        proxy=[(type(suite.proxy[0])).model_validate(item) for item in reloaded["proxy"]] if suite.proxy else [],
        evidence_contract=DomainEvidenceContract.model_validate(reloaded["evidence_contract"]) if reloaded.get("evidence_contract") else None,
    )
