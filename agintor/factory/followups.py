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


from .export import _replace_runtime_destination

@dataclass
class FactoryMessageOutcome:
    chat: FactoryChatIdentity
    message: FactoryMessage
    result: BuiltRuntimeResult


def _provider_name(provider: ModelProvider) -> str:
    return str(getattr(provider, "provider_name", None) or provider.__class__.__name__).strip().lower()


def _runtime_profile_hash(profile: RuntimeProfile) -> str:
    return stable_hash(runtime_profile_payload(profile))


def _load_project_runtime_profile(project_path: Path) -> RuntimeProfile:
    profile_path = project_path / RUNTIME_PROFILE_FILE
    if not profile_path.exists():
        raise FactoryChatError(
            f"factory project runtime profile is missing at {profile_path}; "
            "cannot continue this factory chat"
        )
    return load_runtime_profile(project_path, profile_path=None)


def _load_current_runtime_hash(
    runtime_dir: Path,
    *,
    runtime_profile: RuntimeProfile,
    runtime_backend: str,
) -> str:
    try:
        loaded = load_runtime(
            runtime_dir,
            runtime_profile=runtime_profile,
            runtime_backend=runtime_backend,
        )
    except Exception as exc:
        raise FactoryChatError(
            f"factory project runtime at {runtime_dir} could not be loaded to validate "
            "its pinned runtime hash"
        ) from exc
    runtime_hash = str(loaded.runtime_hash or "").strip()
    if not runtime_hash:
        raise FactoryChatError(
            f"factory project runtime at {runtime_dir} did not report a runtime hash"
        )
    return runtime_hash


def _factory_followup_staging_dir(workspace: str | Path, message_id: str) -> Path:
    staging_root = ensure_directory(Path(workspace) / ".factory_chat_staging")
    return staging_root / f"{message_id}.{stable_hash(message_id, now_ts())[:12]}"


def _factory_message_artifacts(result: BuiltRuntimeResult) -> dict[str, str | Path]:
    return {
        "goal_spec_path": result.goal_spec_path,
        "success_criteria_path": result.success_criteria_path,
        "benchmark_plan_path": result.benchmark_plan_path,
        "verifier_bundle_path": result.verifier_bundle_path,
        "runtime_plan_path": result.runtime_plan_path,
        "deployment_contract_path": str(Path(result.runtime_plan_path).with_name(DEPLOYMENT_CONTRACT_FILE)),
        "export_summary_path": result.export_summary_path,
        "build_summary_path": result.summary_path,
    }


def _validate_factory_planning_artifacts(artifacts: dict[str, str | Path]) -> None:
    for field_name, source in artifacts.items():
        if not source:
            continue
        source_path = Path(source)
        if not source_path.exists() or not source_path.is_file():
            raise FactoryChatError(
                f"factory planning artifact {field_name!r} is missing at {source_path}"
            )


def _resolve_factory_seed_runtime_source(
    project_path: Path,
    leader_runtime_dir: str | Path | None,
) -> Path | None:
    if leader_runtime_dir is None:
        return None
    raw = Path(leader_runtime_dir)
    if raw.is_absolute():
        return raw
    if str(raw).strip() in {"", "."}:
        return project_path

    project_root = project_path.resolve()
    candidates = [
        (project_root.parent / raw).resolve(),
        (project_root / raw).resolve(),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def apply_factory_message(
    project_dir: str | Path,
    instruction: str,
    *,
    workspace: str | Path,
    provider: ModelProvider,
    steps: int = 10,
    mutator_type: str = "heuristic",
    profile_path: str | Path | None = None,
    runtime_backend: str | None = None,
    artifact_mode: str | ArtifactMode | None = None,
) -> FactoryMessageOutcome:
    """Top-level factory chat entry point.

    First call against a fresh ``project_dir`` creates a new factory chat and
    runs the initial build. Subsequent calls amend the prior message's goal
    spec with ``instruction`` and rebuild the runtime in place. Each call
    appends one ``FactoryMessage`` to the chat ledger.
    """

    from .service import build_runtime_from_followup, build_runtime_from_goal

    project_path = Path(project_dir).resolve()
    chat_store = FactoryChatStore(project_path)
    instruction_text = canonical_goal_prompt(instruction)
    if not instruction_text:
        raise ValueError("factory instruction may not be empty")
    effective_profile_path = profile_path
    has_existing_chat = chat_store.has_chat()
    chat = chat_store.load_chat() if has_existing_chat else None
    if has_existing_chat and profile_path is not None:
        raise FactoryChatError(
            "factory follow-ups use the runtime profile pinned in the project; "
            "start a new project to use a different profile"
        )
    if has_existing_chat and effective_profile_path is None:
        embedded_profile = project_path / RUNTIME_PROFILE_FILE
        if embedded_profile.exists():
            effective_profile_path = embedded_profile
    effective_backend = str(
        runtime_backend or (chat.runtime_backend if chat is not None else "local")
    ).strip().lower()
    effective_profile = (
        _load_project_runtime_profile(project_path)
        if has_existing_chat
        else load_runtime_profile(profile_path=effective_profile_path)
    )
    effective_runtime_provider = str(effective_profile.runtime_provider.name or "").strip().lower()
    effective_agintor_provider = _provider_name(provider)

    if has_existing_chat:
        assert chat is not None
        stored_backend = str(chat.runtime_backend or "").strip().lower()
        stored_runtime_provider = str(chat.runtime_provider or "").strip().lower()
        stored_agintor_provider = str(chat.agintor_provider or "").strip().lower()
        stored_runtime_profile_hash = str(chat.runtime_profile_hash or "").strip()
        current_runtime_profile_hash = _runtime_profile_hash(effective_profile)
        if not stored_runtime_profile_hash:
            raise FactoryChatError(
                f"factory chat {chat.chat_id!r} has incomplete runtime profile pinning; "
                "start a new project"
            )
        if current_runtime_profile_hash != stored_runtime_profile_hash:
            raise FactoryChatError(
                f"factory chat {chat.chat_id!r} is pinned to runtime profile hash "
                f"{stored_runtime_profile_hash!r}; current project profile reports "
                f"{current_runtime_profile_hash!r}"
            )
        if effective_backend != stored_backend:
            raise FactoryChatError(
                f"factory chat {chat.chat_id!r} is pinned to runtime backend {stored_backend!r}; "
                f"got {effective_backend!r}"
            )
        if effective_runtime_provider != stored_runtime_provider:
            raise FactoryChatError(
                f"factory chat {chat.chat_id!r} is pinned to runtime provider {stored_runtime_provider!r}; "
                f"got {effective_runtime_provider!r}"
            )
        if effective_agintor_provider != stored_agintor_provider:
            raise FactoryChatError(
                f"factory chat {chat.chat_id!r} is pinned to Agintor provider {stored_agintor_provider!r}; "
                f"got {effective_agintor_provider!r}"
            )
        prior_message = chat_store.latest_message()
        if prior_message is None or not prior_message.goal_spec_path:
            raise FactoryChatError(
                f"factory chat at {chat_store.manifest_path} has no recorded prior message; "
                "cannot apply a follow-up instruction"
            )
        current_runtime_hash = _load_current_runtime_hash(
            project_path,
            runtime_profile=effective_profile,
            runtime_backend=stored_backend,
        )
        if (
            str(prior_message.leader_runtime_hash or "").strip()
            and current_runtime_hash != str(prior_message.leader_runtime_hash or "").strip()
        ):
            raise FactoryChatError(
                f"factory chat {chat.chat_id!r} is pinned to runtime hash "
                f"{prior_message.leader_runtime_hash!r}; current project runtime reports "
                f"{current_runtime_hash!r}"
            )
        message_index = chat_store.next_message_index()
        message_id = chat_store.allocate_message_id(message_index=message_index, prompt=instruction_text)
        trace_context = OpenAITraceContext(
            factory_chat_id=chat.chat_id,
            factory_message_id=message_id,
            factory_message_index=message_index,
        )
        prior_goal = (GoalSpec).model_validate(
            json.loads(Path(prior_message.goal_spec_path).read_text(encoding="utf-8"))
        )
        seed_runtime_source = _resolve_factory_seed_runtime_source(
            project_path,
            prior_message.leader_runtime_dir,
        )
        staging_destination = _factory_followup_staging_dir(workspace, message_id)
        if staging_destination.exists():
            shutil.rmtree(staging_destination)
        try:
            result = build_runtime_from_followup(
                prior_goal,
                instruction_text,
                destination=staging_destination,
                workspace=workspace,
                provider=provider,
                steps=steps,
                mutator_type=mutator_type,
                profile_path=effective_profile_path,
                runtime_profile=effective_profile,
                runtime_backend=stored_backend,
                artifact_mode=artifact_mode,
                seed_runtime_source=seed_runtime_source,
                runtime_provider_name=stored_runtime_provider,
                trace_context=trace_context,
            )
            artifacts = _factory_message_artifacts(result)
            _validate_factory_planning_artifacts(artifacts)
            post_profile = _load_project_runtime_profile(staging_destination)
            post_profile_hash = _runtime_profile_hash(post_profile)
            if post_profile_hash != stored_runtime_profile_hash:
                raise FactoryChatError(
                    f"factory follow-up for chat {chat.chat_id!r} changed the pinned runtime profile "
                    f"from {stored_runtime_profile_hash!r} to {post_profile_hash!r}"
                )
            post_runtime_hash = _load_current_runtime_hash(
                staging_destination,
                runtime_profile=post_profile,
                runtime_backend=stored_backend,
            )
            _replace_runtime_destination(
                staging_destination,
                project_path,
                force=True,
                preserve_names=(CHAT_DIR_NAME, ".runtime_sessions"),
            )
            committed_profile = _load_project_runtime_profile(project_path)
            committed_profile_hash = _runtime_profile_hash(committed_profile)
            if committed_profile_hash != stored_runtime_profile_hash:
                raise FactoryChatError(
                    f"factory follow-up for chat {chat.chat_id!r} committed runtime profile "
                    f"{committed_profile_hash!r}, expected {stored_runtime_profile_hash!r}"
                )
            committed_runtime_hash = _load_current_runtime_hash(
                project_path,
                runtime_profile=committed_profile,
                runtime_backend=stored_backend,
            )
            if committed_runtime_hash != post_runtime_hash:
                raise FactoryChatError(
                    f"factory follow-up for chat {chat.chat_id!r} committed runtime hash "
                    f"{committed_runtime_hash!r}, expected {post_runtime_hash!r}"
                )
            result = replace(result, output_runtime_dir=str(project_path))
        finally:
            shutil.rmtree(staging_destination, ignore_errors=True)
        message = FactoryMessage(
            message_id=message_id,
            message_index=message_index,
            parent_message_id=prior_message.message_id,
            chat_id=chat.chat_id,
            prompt=instruction_text,
            created_at=now_ts(),
            build_id=result.build_id,
            leader_runtime_hash=post_runtime_hash,
            leader_runtime_dir=result.output_runtime_dir,
        )
    else:
        initial_goal_spec = build_goal_spec(
            instruction_text,
            runtime_provider_name=effective_runtime_provider,
            default_runtime_backend=effective_backend,
        )
        chat_id = chat_store.allocate_chat_id(goal_id=initial_goal_spec.goal_id)
        message_id = chat_store.allocate_message_id(message_index=0, prompt=instruction_text)
        trace_context = OpenAITraceContext(
            factory_chat_id=chat_id,
            factory_message_id=message_id,
            factory_message_index=0,
        )
        result = build_runtime_from_goal(
            instruction_text,
            destination=project_path,
            workspace=workspace,
            provider=provider,
            steps=steps,
            mutator_type=mutator_type,
            profile_path=effective_profile_path,
            runtime_profile=effective_profile,
            runtime_backend=effective_backend,
            artifact_mode=artifact_mode,
            force=True,
            trace_context=trace_context,
        )
        artifacts = _factory_message_artifacts(result)
        _validate_factory_planning_artifacts(artifacts)
        exported_profile = _load_project_runtime_profile(project_path)
        exported_profile_hash = _runtime_profile_hash(exported_profile)
        exported_runtime_hash = _load_current_runtime_hash(
            project_path,
            runtime_profile=exported_profile,
            runtime_backend=effective_backend,
        )
        chat = chat_store.create_chat(
            goal_id=result.goal_id,
            runtime_provider=result.runtime_provider,
            agintor_provider=result.agintor_provider,
            runtime_backend=effective_backend,
            runtime_profile_hash=exported_profile_hash,
            chat_id=chat_id,
        )
        message = FactoryMessage(
            message_id=message_id,
            message_index=0,
            parent_message_id=None,
            chat_id=chat.chat_id,
            prompt=instruction_text,
            created_at=now_ts(),
            build_id=result.build_id,
            leader_runtime_hash=exported_runtime_hash,
            leader_runtime_dir=result.output_runtime_dir,
        )

    recorded = chat_store.record_message(
        message,
        prompt_text=instruction_text,
        planning_artifacts=artifacts,
    )
    refreshed_chat = chat_store.load_chat()
    return FactoryMessageOutcome(chat=refreshed_chat, message=recorded, result=result)


def _load_runtime_hash(runtime_dir: Path) -> str:
    bundle_path = runtime_dir / RUNTIME_EXPORT_BUNDLE_FILE
    if not bundle_path.exists():
        return ""
    try:
        payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    return str(payload.get("runtime_hash") or "")
