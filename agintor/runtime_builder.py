from __future__ import annotations

import contextlib
import json
import secrets
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .artifacts import ArtifactMode
from .benchmarks import BenchmarkSuite, build_demo_suite
from .evolution import EvolutionEngine
from .goal_rubric import build_goal_spec, build_success_criteria_bundle, canonical_goal_prompt
from .project import baseline_template_dir, init_runtime
from .providers import LocalDeterministicProvider, ModelProvider
from .runtime_api import load_solve_request, runtime_solve_request_for_user_request
from .runtime_host import RuntimeHost
from .runtime_loader import (
    DEPLOYMENT_CONTRACT_FILE,
    RUNTIME_EXPORT_BUNDLE_FILE,
    load_runtime,
)
from .runtime_profile import (
    RUNTIME_PROFILE_FILE,
    HostedProviderProfile,
    RuntimeProfile,
    load_runtime_profile,
    runtime_profile_payload,
)
from .runtime_sdk import (
    KERNEL_BUNDLE_DIR,
    KERNEL_CAPABILITY_FLAGS,
    KERNEL_MANIFEST_FILE,
    bundle_runtime_kernel,
    preview_kernel_manifest,
)
from .schemas import (
    ArchiveEntry,
    ArchiveRecord,
    BenchmarkPlan,
    BuildSummary,
    DeploymentContract,
    ExportSummary,
    GoalSpec,
    ProviderPlan,
    ProviderRole,
    RuntimeIsolationPolicy,
    RuntimeManifest,
    RuntimePlan,
    ModelRequest,
    SuccessCriteriaBundle,
    VerifierBundle,
    VerifierSpec,
)
from .utils import ensure_directory, stable_hash
from .versioning import RUNTIME_CONTRACT_VERSION


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
    export_summary_path: str
    summary_path: str


@dataclass(frozen=True)
class BuildWorkspaceLayout:
    root: Path
    goal_dir: Path
    planning_dir: Path
    seed_runtime_dir: Path
    evolution_dir: Path
    export_dir: Path


def _build_workspace_layout(workspace: str | Path, clean_goal: str) -> BuildWorkspaceLayout:
    workspace_root = ensure_directory(Path(workspace))
    prefix = f"build_{stable_hash(clean_goal)[:8]}_"
    build_root: Path | None = None
    for _ in range(128):
        candidate = workspace_root / f"{prefix}{secrets.token_hex(4)}"
        try:
            candidate.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            continue
        build_root = candidate
        break
    if build_root is None:
        raise RuntimeError(f"unable to allocate a unique build workspace under {workspace_root}")
    return BuildWorkspaceLayout(
        root=build_root,
        goal_dir=ensure_directory(build_root / "goal"),
        planning_dir=ensure_directory(build_root / "planning"),
        seed_runtime_dir=build_root / "seed_runtime",
        evolution_dir=ensure_directory(build_root / "evolution"),
        export_dir=ensure_directory(build_root / "export"),
    )


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
            (source_task).model_copy(deep=True, update={
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
                })
        )
    return BenchmarkSuite(
        name=f"goal_conditioned_{stable_hash(clean_goal)[:8]}",
        train=[*suite.train, *goal_tasks],
        val=list(suite.val),
        test=list(suite.test),
        proxy=list(suite.proxy),
    )


def _merge_mapping(base: dict[str, Any], updates: Any) -> dict[str, Any]:
    if not isinstance(updates, dict):
        return dict(base)
    return {**dict(base), **updates}


def _normalize_partition_task_ids(
    requested_task_ids: Iterable[str],
    *,
    available_task_ids: Iterable[str],
    default_task_ids: list[str],
    task_family_map: dict[str, str],
    family_targets: Iterable[str],
    excluded_task_ids: Iterable[str] = (),
) -> list[str]:
    available = set(available_task_ids)
    excluded = set(excluded_task_ids)
    required_families = {family for family in family_targets if family}
    normalized: list[str] = []
    seen: set[str] = set()
    for task_id in requested_task_ids:
        if task_id not in available or task_id in seen:
            continue
        normalized.append(task_id)
        seen.add(task_id)
    if not normalized:
        return list(default_task_ids)

    required_family_defaults: dict[str, list[str]] = {}
    for task_id in default_task_ids:
        if task_id in excluded:
            continue
        family = task_family_map.get(task_id)
        if family not in required_families:
            continue
        required_family_defaults.setdefault(family, []).append(task_id)

    covered_families = {
        task_family_map[task_id]
        for task_id in normalized
        if task_id not in excluded and task_id in task_family_map
    }
    for family in sorted(required_families):
        if family in covered_families:
            continue
        for task_id in required_family_defaults.get(family, []):
            if task_id in seen:
                continue
            normalized.append(task_id)
            seen.add(task_id)
    return normalized


def _normalize_benchmark_plan_against_suite(
    benchmark_plan: BenchmarkPlan,
    suite: BenchmarkSuite,
    *,
    goal_spec: GoalSpec,
) -> BenchmarkPlan:
    default_plan = _build_benchmark_plan(goal_spec, suite)
    available_ids = {
        "train": [task.task_id for task in suite.train],
        "proxy": [task.task_id for task in suite.proxy],
        "val": [task.task_id for task in suite.val],
        "test": [task.task_id for task in suite.test],
    }
    known_ids = {task_id for values in available_ids.values() for task_id in values}
    synthetic_ids = [
        task.task_id
        for task in suite.train
        if task.metadata.get("goal_conditioned") is True
    ]
    payload = (benchmark_plan).model_dump()
    payload["goal_id"] = goal_spec.goal_id
    payload["family_targets"] = [
        family
        for family in payload.get("family_targets", [])
        if family in {"top", "mem", "tool", "e2e"}
    ]
    if not payload["family_targets"]:
        payload["family_targets"] = list(default_plan.family_targets)
    expected_synthetic_ids = list(default_plan.synthetic_task_ids)
    payload["train_task_ids"] = _normalize_partition_task_ids(
        payload.get("train_task_ids", []),
        available_task_ids=available_ids["train"],
        default_task_ids=list(default_plan.train_task_ids),
        task_family_map=suite.task_family_map("train"),
        family_targets=payload["family_targets"],
        excluded_task_ids=expected_synthetic_ids,
    )
    payload["proxy_task_ids"] = [task_id for task_id in payload.get("proxy_task_ids", []) if task_id in available_ids["proxy"]] or list(default_plan.proxy_task_ids)
    payload["val_task_ids"] = _normalize_partition_task_ids(
        payload.get("val_task_ids", []),
        available_task_ids=available_ids["val"],
        default_task_ids=list(default_plan.val_task_ids),
        task_family_map=suite.task_family_map("val"),
        family_targets=payload["family_targets"],
    )
    payload["test_task_ids"] = _normalize_partition_task_ids(
        payload.get("test_task_ids", []),
        available_task_ids=available_ids["test"],
        default_task_ids=list(default_plan.test_task_ids),
        task_family_map=suite.task_family_map("test"),
        family_targets=payload["family_targets"],
    )
    payload["synthetic_task_ids"] = [task_id for task_id in payload.get("synthetic_task_ids", []) if task_id in known_ids and task_id in synthetic_ids]
    if expected_synthetic_ids and set(payload["synthetic_task_ids"]) != set(expected_synthetic_ids):
        payload["synthetic_task_ids"] = list(default_plan.synthetic_task_ids)
    for task_id in payload["synthetic_task_ids"]:
        if task_id not in payload["train_task_ids"]:
            payload["train_task_ids"].append(task_id)
    payload["frozen"] = True
    return (BenchmarkPlan).model_validate(payload)


def _maybe_provider_refine_planning(
    provider: ModelProvider,
    *,
    goal_prompt: str,
    goal_spec: GoalSpec,
    success_bundle: SuccessCriteriaBundle,
    benchmark_plan: BenchmarkPlan,
    suite: BenchmarkSuite,
) -> tuple[GoalSpec, SuccessCriteriaBundle, BenchmarkPlan]:
    provider_name = getattr(provider, "provider_name", provider.__class__.__name__).strip().lower()
    if provider_name == "local":
        return goal_spec, success_bundle, benchmark_plan
    request = ModelRequest(
        instructions=(
            "Return strict JSON with keys goal_spec, success_criteria, and benchmark_plan. "
            "Preserve the same goal_id, bundle_id, plan_id, and verifier_bundle_id. "
            "Only revise fields when the raw goal clearly supports the revision."
        ),
        prompt=json.dumps(
            {
                "raw_goal": goal_prompt,
                "goal_spec": (goal_spec).model_dump(),
                "success_criteria": (success_bundle).model_dump(),
                "benchmark_plan": (benchmark_plan).model_dump(),
            },
            sort_keys=True,
        ),
        model_class="large",
        seed=0,
        metadata={"mode": "planning", "max_output_tokens": 16000},
    )
    try:
        response = provider.generate(request)
        payload = json.loads(response.text)
    except Exception:
        return goal_spec, success_bundle, benchmark_plan
    if not isinstance(payload, dict):
        return goal_spec, success_bundle, benchmark_plan
    goal_payload = payload.get("goal_spec")
    if isinstance(goal_payload, dict):
        merged_goal = _merge_mapping((goal_spec).model_dump(), goal_payload)
        merged_goal["goal_id"] = goal_spec.goal_id
        merged_goal["raw_prompt"] = goal_spec.raw_prompt
        merged_goal["constraints"] = _merge_mapping(goal_spec.constraints, goal_payload.get("constraints"))
        merged_goal["deployment_preferences"] = _merge_mapping(goal_spec.deployment_preferences, goal_payload.get("deployment_preferences"))
        goal_spec = (GoalSpec).model_validate(merged_goal)
        success_bundle = build_success_criteria_bundle(goal_spec)
        benchmark_plan = _normalize_benchmark_plan_against_suite(
            _build_benchmark_plan(goal_spec, suite),
            suite,
            goal_spec=goal_spec,
        )
    success_payload = payload.get("success_criteria")
    if isinstance(success_payload, dict):
        merged_success = _merge_mapping((success_bundle).model_dump(), success_payload)
        merged_success["bundle_id"] = success_bundle.bundle_id
        merged_success["goal_id"] = goal_spec.goal_id
        success_bundle = (SuccessCriteriaBundle).model_validate(merged_success)
    benchmark_payload = payload.get("benchmark_plan")
    if isinstance(benchmark_payload, dict):
        merged_plan = _merge_mapping((benchmark_plan).model_dump(), benchmark_payload)
        merged_plan["plan_id"] = benchmark_plan.plan_id
        merged_plan["goal_id"] = goal_spec.goal_id
        merged_plan["verifier_bundle_id"] = benchmark_plan.verifier_bundle_id
        benchmark_plan = _normalize_benchmark_plan_against_suite(
            (BenchmarkPlan).model_validate(merged_plan),
            suite,
            goal_spec=goal_spec,
        )
    return goal_spec, success_bundle, benchmark_plan


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
    return []


def _required_runtime_env_any_of(profile: RuntimeProfile) -> list[list[str]]:
    credential_group = [
        name
        for name in [
            str(profile.runtime_provider.api_key_env or "").strip(),
            str(profile.runtime_provider.api_key_file_env or "").strip(),
        ]
        if name
    ]
    return [credential_group] if credential_group else []


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
    kernel_manifest = preview_kernel_manifest()
    notes = list(goal_spec.assumptions)
    if profile.runtime_provider.api_key_file_env:
        notes.append(
            f"{profile.runtime_provider.api_key_file_env} may be used as a key-file alternative for the default runtime provider."
        )
    required_env_any_of = _required_runtime_env_any_of(profile)
    environment_allowlist = sorted(
        {
            *[str(name).strip() for name in _required_runtime_env_names(profile)],
            *[name for group in required_env_any_of for name in group],
            str(profile.runtime_provider.base_url_env or "").strip(),
            str(profile.runtime_provider.pricing_env or "").strip(),
        }
    )
    environment_allowlist = [name for name in environment_allowlist if name]
    network_policy = str(goal_spec.constraints.get("network_policy", "provider-only"))
    required_guarantees = [
        "timeout_enforcement",
        "workspace_isolation",
        "environment_filtering",
    ]
    desired_guarantees = ["process_cleanup"]
    if network_policy in {"none", "restricted"}:
        required_guarantees.append("network_disablement")
    return DeploymentContract(
        entry_command='agintor solve <runtime_dir> --prompt "<request>"',
        runtime_contract_version=RUNTIME_CONTRACT_VERSION,
        python_version=">=3.12",
        supported_backends=list(goal_spec.deployment_preferences.get("supported_backends", ["local", "docker"])),
        required_env_names=_required_runtime_env_names(profile),
        required_env_any_of=required_env_any_of,
        environment_allowlist=environment_allowlist,
        network_policy=network_policy,
        filesystem_policy=str(goal_spec.constraints.get("filesystem_policy", "workspace-read-write")),
        dependency_digest_set=sorted(set(kernel_manifest.files.values())),
        capability_flags=[*KERNEL_CAPABILITY_FLAGS, "benchmark_mode", "prompt_mode"],
        runtime_isolation_policy=RuntimeIsolationPolicy(
            timeout_envelope={"seconds": profile.execution.latency_max},
            workspace_root=".",
            environment_allowlist=environment_allowlist,
            network_policy=network_policy,
            filesystem_policy=str(goal_spec.constraints.get("filesystem_policy", "workspace-read-write")),
            required_guarantees=required_guarantees,
            desired_guarantees=desired_guarantees,
        ),
        notes=notes,
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
    provider_plan = ProviderPlan(
        plan_id=f"providers.{stable_hash(goal_spec.goal_id, agintor_provider, profile.runtime_provider.name)[:12]}",
        agintor_provider=ProviderRole(name=agintor_provider),
        runtime_provider=ProviderRole(
            name=profile.runtime_provider.name,
            api_key_env=profile.runtime_provider.api_key_env,
            api_key_file_env=profile.runtime_provider.api_key_file_env,
            model_map=dict(profile.runtime_provider.model_map),
        ),
        runtime_backend=runtime_backend,
    )
    return RuntimePlan(
        plan_id=f"runtime.{stable_hash(goal_spec.goal_id, benchmark_plan.plan_id)[:12]}",
        goal_id=goal_spec.goal_id,
        runtime_contract_version=RUNTIME_CONTRACT_VERSION,
        seed_template=str(baseline_template_dir()),
        mutable_files=list(manifest.mutable_files),
        immutable_manifest=list(manifest.immutable_manifest),
        runtime_profile=runtime_profile_payload(profile),
        provider_plan=provider_plan,
        tooling_scope=_tooling_scope_from_suite(suite),
        deployment_contract=deployment_contract,
    )


def _factory_runtime_profile_for_plan(profile: RuntimeProfile, runtime_plan: RuntimePlan) -> RuntimeProfile:
    payload = (profile).model_dump()
    runtime_payload = dict(runtime_plan.runtime_profile)
    payload["prompts"] = _merge_mapping(payload.get("prompts", {}), runtime_payload.get("prompts"))
    for key in ("runtime_provider", "execution", "topology", "memory", "tooling", "control"):
        if key in runtime_payload:
            payload[key] = runtime_payload[key]
    return (RuntimeProfile).model_validate(payload)


def _write_seed_runtime(seed_runtime_dir: Path, runtime_plan: RuntimePlan) -> None:
    init_runtime(seed_runtime_dir, force=True)
    _write_json(seed_runtime_dir / RUNTIME_PROFILE_FILE, runtime_plan.runtime_profile)
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


def _goal_requires_repo_writes(goal_spec: GoalSpec) -> bool:
    tokens = {
        *[str(token).lower() for token in goal_spec.goal_keywords],
        *[str(token).lower() for token in goal_spec.goal_phrases],
    }
    return any(token in tokens for token in {"edit", "file", "files", "patch", "repo", "repository"})


def _runtime_provider_name(runtime_plan: RuntimePlan) -> str:
    return str(runtime_plan.provider_plan.runtime_provider.name or "").strip().lower()


def _filesystem_is_read_only(policy: str) -> bool:
    normalized = str(policy or "").strip().lower()
    return "read-only" in normalized or normalized in {"readonly", "read_only", "none"}


def _repair_planning_artifacts(
    goal_spec: GoalSpec,
    suite: BenchmarkSuite,
    benchmark_plan: BenchmarkPlan,
    verifier_bundle: VerifierBundle,
    runtime_plan: RuntimePlan,
    deployment_contract: DeploymentContract,
    issues: list[dict[str, Any]],
) -> tuple[VerifierBundle, RuntimePlan, DeploymentContract, bool]:
    repaired = False
    repaired_bundle = verifier_bundle
    repaired_runtime_plan = runtime_plan
    repaired_contract = deployment_contract
    issue_ids = {str(issue.get("issue_id") or "") for issue in issues}
    requested_backend = str(goal_spec.constraints.get("runtime_backend", "")).strip().lower()
    if f"{goal_spec.goal_id}.backend" in issue_ids and requested_backend in {"local", "docker"}:
        repaired_contract = (repaired_contract).model_copy(update={"supported_backends": [requested_backend]})
        repaired_runtime_plan = (repaired_runtime_plan).model_copy(update={"deployment_contract": repaired_contract})
        repaired = True
    if any(issue_id.startswith(f"{goal_spec.goal_id}.verifier.") for issue_id in issue_ids):
        repaired_bundle = _build_verifier_bundle(benchmark_plan, suite)
        repaired = True
    if f"{goal_spec.goal_id}.provider.network" in issue_ids:
        runtime_profile_payload = dict(repaired_runtime_plan.runtime_profile)
        runtime_profile_payload["runtime_provider"] = (HostedProviderProfile(name="local")).model_dump()
        repaired_contract = (repaired_contract).model_copy(update={
                "required_env_names": [],
                "required_env_any_of": [],
                "environment_allowlist": [],
                "notes": [*list(repaired_contract.notes), "Runtime provider was repaired to local deterministic mode because network access is restricted."],
            })
        repaired_runtime_plan = (repaired_runtime_plan).model_copy(update={
                "runtime_profile": runtime_profile_payload,
                "provider_plan": (repaired_runtime_plan.provider_plan).model_copy(update={"runtime_provider": ProviderRole(name="local")}),
                "deployment_contract": repaired_contract,
            })
        repaired = True
    return repaired_bundle, repaired_runtime_plan, repaired_contract, repaired


def _plan_consistency_check(
    goal_spec: GoalSpec,
    suite: BenchmarkSuite,
    verifier_bundle: VerifierBundle,
    runtime_plan: RuntimePlan,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []

    def add_issue(issue_id: str, severity: str, message: str) -> None:
        issues.append({"issue_id": issue_id, "severity": severity, "message": message})

    requested_backend = str(goal_spec.constraints.get("runtime_backend", "")).strip().lower()
    supported_backends = {backend.strip().lower() for backend in runtime_plan.deployment_contract.supported_backends}
    if requested_backend and supported_backends and requested_backend not in supported_backends:
        add_issue(
            f"{goal_spec.goal_id}.backend",
            "error",
            f"requested runtime backend {requested_backend!r} is not supported by the deployment contract",
        )
    if _goal_requires_repo_writes(goal_spec) and _filesystem_is_read_only(runtime_plan.deployment_contract.filesystem_policy):
        add_issue(
            f"{goal_spec.goal_id}.filesystem",
            "error",
            "the normalized goal implies repository writes, but the deployment contract is read-only",
        )
    if (
        str(runtime_plan.deployment_contract.network_policy).strip().lower() == "restricted"
        and _runtime_provider_name(runtime_plan) not in {"", "local"}
    ):
        add_issue(
            f"{goal_spec.goal_id}.provider.network",
            "error",
            "the runtime provider requires network access, but the deployment contract forbids network use",
        )
    if goal_spec.constraints.get("network_policy") == "restricted" and "tool" in goal_spec.target_families:
        add_issue(
            f"{goal_spec.goal_id}.network",
            "warning",
            "tool-oriented goals are constrained by restricted network access; only local tooling paths remain valid",
        )
    task_ids_with_verifiers = {spec.artifact_contract.get("task_id") for spec in verifier_bundle.verifiers}
    for task in [*suite.train, *suite.proxy, *suite.val, *suite.test]:
        if task.task_id not in task_ids_with_verifiers:
            add_issue(
                f"{goal_spec.goal_id}.verifier.{task.task_id}",
                "error",
                f"benchmark task {task.task_id!r} is missing from the verifier bundle",
            )
    return issues


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
    artifact_mode: str | ArtifactMode | None = None,
    force: bool = False,
) -> BuiltRuntimeResult:
    clean_goal = canonical_goal_prompt(goal_prompt)
    if not clean_goal:
        raise ValueError("goal prompt may not be empty")
    destination_path = Path(destination)
    layout = _build_workspace_layout(workspace, clean_goal)
    build_id = f"build.{stable_hash(clean_goal, layout.root.name)[:12]}"
    agintor_provider = getattr(provider, "provider_name", provider.__class__.__name__.lower())
    effective_runtime_backend = (runtime_backend or "local").strip().lower()
    merged_profile = load_runtime_profile(profile_path=profile_path)
    (layout.goal_dir / "raw_goal.txt").write_text(clean_goal, encoding="utf-8")

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
            verifier_bundle,
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
    _write_seed_runtime(layout.seed_runtime_dir, runtime_plan)
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
    )
