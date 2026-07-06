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


from .pipeline import _run_factory_pipeline
from .export import _replace_runtime_destination, _write_seed_runtime
from .followups import FactoryMessageOutcome, apply_factory_message
from .planning import (
    _build_benchmark_plan,
    _build_verifier_bundle,
    _normalize_benchmark_plan_against_suite,
    build_goal_conditioned_suite,
)

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
    signal_sufficiency_path: str = ""
    runtime_kind: str = "policy_modules"
    runtime_spec_digest: str = ""
    oracle_package_hash: str = ""
    validation_plan_hash: str = ""
    oracle_package_ref: str = ""
    oracle_public_ref: str = ""


def build_runtime_from_goal(
    goal_prompt: str,
    *,
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
    trace_context: OpenAITraceContext | None = None,
    runtime_kind: str = "policy_modules",
) -> BuiltRuntimeResult:
    return _run_factory_pipeline(
        goal_input=goal_prompt,
        destination=destination,
        workspace=workspace,
        provider=provider,
        steps=steps,
        mutator_type=mutator_type,
        profile_path=profile_path,
        runtime_profile=runtime_profile,
        runtime_backend=runtime_backend,
        artifact_mode=artifact_mode,
        force=force,
        seed_runtime_source=None,
        trace_context=trace_context,
        runtime_kind=runtime_kind,
    )


def build_runtime_from_followup(
    prior_goal: GoalSpec,
    instruction: str,
    *,
    destination: str | Path,
    workspace: str | Path,
    provider: ModelProvider,
    steps: int = 10,
    mutator_type: str = "heuristic",
    profile_path: str | Path | None = None,
    runtime_profile: RuntimeProfile | None = None,
    runtime_backend: str | None = None,
    artifact_mode: str | ArtifactMode | None = None,
    seed_runtime_source: Path | None = None,
    runtime_provider_name: str | None = None,
    trace_context: OpenAITraceContext | None = None,
    runtime_kind: str = "policy_modules",
) -> BuiltRuntimeResult:
    """Run the factory pipeline against an amended goal spec.

    The amended goal preserves the prior `goal_id` so the chat keeps a stable
    identity. The runtime at `destination` is replaced in place with the new
    leader; ``seed_runtime_source`` (typically the prior leader's runtime dir)
    is copied into the seed slot so evolution starts from the previous build.
    """

    if runtime_provider_name is None:
        if runtime_profile is not None:
            runtime_provider_name = runtime_profile.runtime_provider.name
        elif profile_path is not None:
            runtime_provider_name = load_runtime_profile(profile_path=profile_path).runtime_provider.name
    amended = amend_goal_spec(
        prior_goal,
        instruction,
        runtime_provider_name=runtime_provider_name,
        default_runtime_backend=runtime_backend,
    )
    amended = amended.model_copy(update={"constraints": {**dict(amended.constraints), "runtime_kind": runtime_kind}})
    return _run_factory_pipeline(
        goal_input=amended,
        destination=destination,
        workspace=workspace,
        provider=provider,
        steps=steps,
        mutator_type=mutator_type,
        profile_path=profile_path,
        runtime_profile=runtime_profile,
        runtime_backend=runtime_backend,
        artifact_mode=artifact_mode,
        force=True,
        seed_runtime_source=seed_runtime_source,
        trace_context=trace_context,
        runtime_kind=runtime_kind,
    )
