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
