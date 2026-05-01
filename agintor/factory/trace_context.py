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


def _build_factory_trace_context(
    parent: OpenAITraceContext | None,
    *,
    build_id: str,
    objective: str,
) -> OpenAITraceContext | None:
    if parent is None:
        return None
    return build_trace_context(
        provider_role="factory",
        request_id=build_id,
        build_id=build_id,
        factory_chat_id=parent.factory_chat_id,
        factory_message_id=parent.factory_message_id,
        factory_message_index=parent.factory_message_index,
        session_id=parent.session_id,
        objective=objective,
    )
