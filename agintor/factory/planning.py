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


from .export import _load_template_manifest

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
    trace_context: OpenAITraceContext | None = None,
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
        metadata={
            "mode": "planning",
            "max_output_tokens": 16000,
            **({"trace_context": trace_context.model_dump()} if trace_context is not None else {}),
        },
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
        merged_goal["amendment_index"] = goal_spec.amendment_index
        merged_goal["amendment_history"] = list(goal_spec.amendment_history)
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
