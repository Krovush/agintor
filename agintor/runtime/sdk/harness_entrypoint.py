from __future__ import annotations

import argparse
import json
import os
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ...authority.public_tasks import assert_public_payload
from ...contracts.epochs import TaskEnvelope
from ...contracts.harness import CompositeRunPlan, HarnessPublicSessionContext
from ...contracts.outcomes import PairKey
from ...contracts.run_evidence import assert_no_resolved_credentials
from ...core.redaction import redact_sensitive_text
from ...isolation.commands import DockerCommandBackend, IsolatedCommandBackend
from ...isolation.replay import (
    IsolatedCommandReplayBackend,
    IsolatedCommandReplayBinding,
    load_isolated_command_replay_manifest,
)
from ...repositories.workspaces import resolve_local_snapshot_uri
from ..harness_profile import HarnessDeploymentProfile
from ..api.composite_compiler import compile_composite_run_plan
from ..kernel.composite_provider import ControlledProvider, CredentialReference
from ..kernel.openai_responses_provider import OpenAIResponsesProvider
from ..kernel.composite_replay_provider import (
    CompositeReplayBinding,
    CompositeReplayProvider,
    load_composite_replay_manifest,
)
from .harness_executor import HarnessSolveError, HarnessSolveResult, execute_harness_solve
from .harness_manifest import HARNESS_KERNEL_CAPABILITY_FLAGS
from .harness_release_loader import (
    HarnessReleaseLoadError,
    LoadedHarnessRelease,
    load_active_harness_release,
)


HARNESS_ENTRY_INSPECT_SCHEMA_VERSION = "repo-repair-harness-inspect-v1"
HARNESS_ENTRY_SOLVE_SCHEMA_VERSION = "repo-repair-harness-solve-request-v1"
HARNESS_ENTRY_ERROR_SCHEMA_VERSION = "repo-repair-harness-entry-error-v1"

_ADAPTER_NAME_RE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")


class HarnessEntryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HarnessAdapterSelection(HarnessEntryModel):
    name: str
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = str(value or "").strip().lower()
        if not _ADAPTER_NAME_RE.fullmatch(normalized):
            raise ValueError("adapter name must be a portable lowercase identifier")
        return normalized

    @field_validator("config")
    @classmethod
    def validate_config(cls, value: dict[str, Any]) -> dict[str, Any]:
        payload = dict(value)
        assert_no_resolved_credentials(payload)
        assert_public_payload(payload)
        return payload


class HarnessReplayExecution(HarnessEntryModel):
    mode: Literal["replay"] = "replay"
    provider_manifest_path: str = Field(min_length=1)
    command_manifest_path: str = Field(min_length=1)


class HarnessLiveExecution(HarnessEntryModel):
    mode: Literal["live"] = "live"


HarnessExecutionSelection = Annotated[
    HarnessReplayExecution | HarnessLiveExecution,
    Field(discriminator="mode"),
]


class HarnessSolveFileRequest(HarnessEntryModel):
    schema_version: Literal[HARNESS_ENTRY_SOLVE_SCHEMA_VERSION] = (
        HARNESS_ENTRY_SOLVE_SCHEMA_VERSION
    )
    task: TaskEnvelope
    execution: HarnessExecutionSelection
    workspace_snapshot_source_path: str | None = None
    run_artifact_workspace: str | None = None
    run_root: str | None = None
    run_id: str | None = None
    workspace_id: str | None = None
    pair_key: PairKey | None = None
    session_context: HarnessPublicSessionContext | None = None

    @field_validator("run_id", "workspace_id")
    @classmethod
    def validate_optional_id(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value or "").strip()
        if not _IDENTIFIER_RE.fullmatch(normalized):
            raise ValueError(f"{info.field_name} must be a portable identifier")
        return normalized

    @field_validator("run_artifact_workspace", "run_root")
    @classmethod
    def validate_optional_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = str(value or "").strip()
        if not normalized or "\x00" in normalized:
            raise ValueError("solve workspace paths must be nonempty and NUL-free")
        return normalized

    @field_validator("workspace_snapshot_source_path")
    @classmethod
    def validate_snapshot_source_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = str(value or "").strip()
        if not normalized or "\x00" in normalized:
            raise ValueError("workspace snapshot source path must be nonempty and NUL-free")
        if not Path(normalized).expanduser().is_absolute():
            raise ValueError("workspace snapshot source path must be absolute")
        return normalized

    @model_validator(mode="after")
    def validate_request_shape(self) -> "HarnessSolveFileRequest":
        if (self.run_artifact_workspace is None) == (self.run_root is None):
            raise ValueError("provide exactly one run_artifact_workspace or run_root")
        if (self.run_id is None) != (self.workspace_id is None):
            raise ValueError("run_id and workspace_id must be supplied together")
        if self.execution.mode == "replay" and self.run_id is None:
            raise ValueError("strict replay solves require explicit run_id and workspace_id")
        payload = self.model_dump(mode="json")
        assert_no_resolved_credentials(payload)
        assert_public_payload(payload)
        return self


class HarnessInspectResult(HarnessEntryModel):
    schema_version: Literal[HARNESS_ENTRY_INSPECT_SCHEMA_VERSION] = (
        HARNESS_ENTRY_INSPECT_SCHEMA_VERSION
    )
    runtime_kind: Literal["harness"] = "harness"
    capability_epoch: Literal["repo-repair-v1"] = "repo-repair-v1"
    capability_flags: tuple[str, ...]
    release_digest: str
    release_manifest_digest: str
    epoch_id: str
    epoch_manifest_digest: str
    deployment: dict[str, Any]
    protocol_source_digest: str
    compiled_semantic_digest: str
    dependency_manifest_digest: str
    provider_adapters: tuple[str, ...]
    command_backend_adapters: tuple[str, ...]


class HarnessEntryErrorResult(HarnessEntryModel):
    schema_version: Literal[HARNESS_ENTRY_ERROR_SCHEMA_VERSION] = (
        HARNESS_ENTRY_ERROR_SCHEMA_VERSION
    )
    status: Literal["failed"] = "failed"
    code: str
    message: str


class HarnessEntryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class HarnessAdapterContext:
    project_root: Path
    request_path: Path
    task: TaskEnvelope
    release: LoadedHarnessRelease
    plan: CompositeRunPlan
    session_context: HarnessPublicSessionContext | None = None


ProviderAdapterFactory = Callable[
    [HarnessAdapterSelection, HarnessAdapterContext],
    ControlledProvider,
]
CommandBackendAdapterFactory = Callable[
    [HarnessAdapterSelection, HarnessAdapterContext],
    IsolatedCommandBackend,
]


class HarnessAdapterRegistry:
    """Explicit construction boundary; it never imports adapters by name."""

    def __init__(
        self,
        *,
        allowed_provider_names: Sequence[str] = ("replay",),
        allowed_command_backend_names: Sequence[str] = (),
    ) -> None:
        self._allowed_providers = self._normalize_allowlist(allowed_provider_names)
        self._allowed_backends = self._normalize_allowlist(allowed_command_backend_names)
        self._provider_factories: dict[str, ProviderAdapterFactory] = {}
        self._backend_factories: dict[str, CommandBackendAdapterFactory] = {}

    @staticmethod
    def _normalize_allowlist(values: Sequence[str]) -> frozenset[str]:
        normalized = frozenset(str(value or "").strip().lower() for value in values)
        if any(not _ADAPTER_NAME_RE.fullmatch(value) for value in normalized):
            raise ValueError("adapter allowlists require portable lowercase names")
        return normalized

    def register_provider(self, name: str, factory: ProviderAdapterFactory) -> None:
        normalized = str(name or "").strip().lower()
        if normalized not in self._allowed_providers:
            raise ValueError(f"provider adapter {normalized!r} is not allowlisted")
        if normalized in self._provider_factories:
            raise ValueError(f"provider adapter {normalized!r} is already registered")
        if not callable(factory):
            raise TypeError("provider adapter factory must be callable")
        self._provider_factories[normalized] = factory

    def register_command_backend(
        self,
        name: str,
        factory: CommandBackendAdapterFactory,
    ) -> None:
        normalized = str(name or "").strip().lower()
        if normalized not in self._allowed_backends:
            raise ValueError(f"command backend adapter {normalized!r} is not allowlisted")
        if normalized in self._backend_factories:
            raise ValueError(f"command backend adapter {normalized!r} is already registered")
        if not callable(factory):
            raise TypeError("command backend adapter factory must be callable")
        self._backend_factories[normalized] = factory

    @property
    def provider_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._provider_factories))

    @property
    def command_backend_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._backend_factories))

    def build_provider(
        self,
        selection: HarnessAdapterSelection,
        context: HarnessAdapterContext,
    ) -> ControlledProvider:
        factory = self._provider_factories.get(selection.name)
        if factory is None:
            raise HarnessEntryError(
                "provider_adapter_unavailable",
                f"provider adapter {selection.name!r} is unavailable or not installed",
            )
        provider = factory(selection, context)
        if not callable(getattr(provider, "invoke", None)):
            raise HarnessEntryError(
                "provider_adapter_invalid",
                "provider adapter does not implement the controlled invocation boundary",
            )
        if getattr(provider, "execution_provenance", None) is None:
            raise HarnessEntryError(
                "provider_provenance_missing",
                "provider adapter does not declare execution provenance",
            )
        return provider

    def build_command_backend(
        self,
        selection: HarnessAdapterSelection,
        context: HarnessAdapterContext,
    ) -> IsolatedCommandBackend:
        factory = self._backend_factories.get(selection.name)
        if factory is None:
            raise HarnessEntryError(
                "command_backend_adapter_unavailable",
                f"command backend adapter {selection.name!r} is unavailable or not installed",
            )
        backend = factory(selection, context)
        if not callable(getattr(backend, "run", None)):
            raise HarnessEntryError(
                "command_backend_adapter_invalid",
                "command backend adapter does not implement the isolated command boundary",
            )
        return backend


class _ReplayAdapterConfig(HarnessEntryModel):
    manifest_path: str = Field(min_length=1)


class _CommandReplayAdapterConfig(HarnessEntryModel):
    manifest_path: str = Field(min_length=1)


def _resolve_request_path(raw_path: str, request_path: Path) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = request_path.parent / candidate
    return candidate.resolve()


def _build_replay_provider(
    selection: HarnessAdapterSelection,
    context: HarnessAdapterContext,
) -> CompositeReplayProvider:
    try:
        config = _ReplayAdapterConfig.model_validate(selection.config)
        manifest_path = _resolve_request_path(config.manifest_path, context.request_path)
        manifest = load_composite_replay_manifest(manifest_path)
        binding = CompositeReplayBinding.from_runtime_inputs(
            release_digest=context.release.manifest.release_digest,
            task=context.task,
            deployment=context.release.manifest.deployment,
            plan=context.plan,
            public_session_context=context.session_context,
        )
        return CompositeReplayProvider(manifest, expected_binding=binding)
    except HarnessEntryError:
        raise
    except Exception as exc:
        raise HarnessEntryError(
            "replay_manifest_invalid",
            "explicit replay manifest failed validation or runtime identity binding",
        ) from exc


def _build_command_replay_backend(
    selection: HarnessAdapterSelection,
    context: HarnessAdapterContext,
) -> IsolatedCommandReplayBackend:
    try:
        config = _CommandReplayAdapterConfig.model_validate(selection.config)
        manifest_path = _resolve_request_path(config.manifest_path, context.request_path)
        manifest = load_isolated_command_replay_manifest(manifest_path)
        binding = IsolatedCommandReplayBinding.from_runtime_inputs(
            release_digest=context.release.manifest.release_digest,
            task=context.task,
            command_policy_digest=(
                context.release.manifest.deployment.command_container_policy_digest
            ),
        )
        profile = HarnessDeploymentProfile.model_validate(context.release.profile.profile)
        return IsolatedCommandReplayBackend(
            manifest,
            expected_binding=binding,
            policy=profile.command_container_policy.to_isolated_command_policy(),
        )
    except HarnessEntryError:
        raise
    except Exception as exc:
        raise HarnessEntryError(
            "command_replay_manifest_invalid",
            "explicit command replay manifest failed validation or runtime identity binding",
        ) from exc


def _build_frozen_docker_backend(
    selection: HarnessAdapterSelection,
    context: HarnessAdapterContext,
) -> DockerCommandBackend:
    if selection.config:
        raise HarnessEntryError(
            "live_command_config_forbidden",
            "live command containment is derived only from the frozen release profile",
        )
    profile = HarnessDeploymentProfile.model_validate(context.release.profile.profile)
    return DockerCommandBackend(
        profile.command_container_policy.to_isolated_command_policy()
    )


def _build_openai_responses_provider(
    selection: HarnessAdapterSelection,
    context: HarnessAdapterContext,
) -> OpenAIResponsesProvider:
    if selection.config:
        raise HarnessEntryError(
            "live_provider_config_forbidden",
            "live provider construction is derived only from the frozen release profile",
        )
    profile = HarnessDeploymentProfile.model_validate(context.release.profile.profile)
    try:
        return OpenAIResponsesProvider(
            profile,
            deployment=context.release.manifest.deployment,
        )
    except Exception as exc:
        raise HarnessEntryError(
            "provider_adapter_invalid",
            "OpenAI Responses provider failed frozen deployment validation",
        ) from exc


def default_harness_adapter_registry(
    *,
    allowed_provider_names: Sequence[str] = ("openai", "replay"),
    allowed_command_backend_names: Sequence[str] = (
        "command_replay",
        "frozen_docker",
    ),
) -> HarnessAdapterRegistry:
    provider_names = {
        str(name or "").strip().lower() for name in allowed_provider_names
    }
    registry = HarnessAdapterRegistry(
        allowed_provider_names=allowed_provider_names,
        allowed_command_backend_names=allowed_command_backend_names,
    )
    if "replay" in provider_names:
        registry.register_provider("replay", _build_replay_provider)
    if "openai" in provider_names:
        registry.register_provider("openai", _build_openai_responses_provider)
    if "command_replay" in {
        str(name or "").strip().lower() for name in allowed_command_backend_names
    }:
        registry.register_command_backend("command_replay", _build_command_replay_backend)
    if "frozen_docker" in {
        str(name or "").strip().lower() for name in allowed_command_backend_names
    }:
        registry.register_command_backend("frozen_docker", _build_frozen_docker_backend)
    return registry


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HarnessEntryError("request_json_invalid", "request file is not valid UTF-8 JSON") from exc
    if not isinstance(payload, Mapping):
        raise HarnessEntryError("request_json_invalid", "request JSON root must be an object")
    return dict(payload)


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(dict(payload), handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def inspect_harness_release(
    project_root: str | Path,
    *,
    registry: HarnessAdapterRegistry | None = None,
) -> HarnessInspectResult:
    effective_registry = registry or default_harness_adapter_registry()
    release = load_active_harness_release(project_root)
    return HarnessInspectResult(
        capability_flags=HARNESS_KERNEL_CAPABILITY_FLAGS,
        release_digest=release.manifest.release_digest,
        release_manifest_digest=release.manifest.manifest_digest,
        epoch_id=release.manifest.epoch_id,
        epoch_manifest_digest=release.manifest.epoch_manifest_digest,
        deployment=release.manifest.deployment.model_dump(mode="json"),
        protocol_source_digest=release.manifest.protocol_source_digest,
        compiled_semantic_digest=release.manifest.compiled_semantic_digest,
        dependency_manifest_digest=release.manifest.dependency_manifest_digest,
        provider_adapters=effective_registry.provider_names,
        command_backend_adapters=effective_registry.command_backend_names,
    )


def execute_harness_solve_file(
    project_root: str | Path,
    request_path: str | Path,
    *,
    registry: HarnessAdapterRegistry | None = None,
) -> HarnessSolveResult:
    request_file = Path(request_path).expanduser().resolve()
    try:
        request = HarnessSolveFileRequest.model_validate(_read_json_object(request_file))
    except HarnessEntryError:
        raise
    except Exception as exc:
        raise HarnessEntryError(
            "solve_request_invalid",
            "structured harness solve request failed validation",
        ) from exc
    effective_registry = registry or default_harness_adapter_registry()
    release = load_active_harness_release(project_root)
    profile = HarnessDeploymentProfile.model_validate(release.profile.profile)
    plan = compile_composite_run_plan(request.task, release.protocol, release.dependencies)
    context = HarnessAdapterContext(
        project_root=Path(project_root).expanduser().resolve(),
        request_path=request_file,
        task=request.task,
        release=release,
        plan=plan,
        session_context=request.session_context,
    )
    provider_selection = (
        HarnessAdapterSelection(
            name="replay",
            config={"manifest_path": request.execution.provider_manifest_path},
        )
        if isinstance(request.execution, HarnessReplayExecution)
        else HarnessAdapterSelection(name=profile.provider, config={})
    )
    provider = effective_registry.build_provider(provider_selection, context)
    command_backend = (
        effective_registry.build_command_backend(
            HarnessAdapterSelection(
                name="command_replay",
                config={"manifest_path": request.execution.command_manifest_path},
            ),
            context,
        )
        if isinstance(request.execution, HarnessReplayExecution)
        else effective_registry.build_command_backend(
            HarnessAdapterSelection(name="frozen_docker", config={}),
            context,
        )
    )
    execution_kwargs: dict[str, Any] = {
        "provider": provider,
        "command_backend": command_backend,
        "run_artifact_workspace": request.run_artifact_workspace,
        "run_root": request.run_root,
        "snapshot_source_root": (
            request.workspace_snapshot_source_path
            or str(
                resolve_local_snapshot_uri(
                    request.task.workspace_snapshot.uri,
                    relative_to=request_file.parent,
                )
            )
        ),
        "credential_reference": (
            None
            if request.execution.mode == "replay"
            else _credential_reference_from_profile(profile)
        ),
        "public_session_context": request.session_context,
        "pair_key": request.pair_key,
    }
    if request.run_id is not None:
        execution_kwargs["run_id"] = request.run_id
        execution_kwargs["workspace_id"] = request.workspace_id
    try:
        result = execute_harness_solve(
            project_root,
            request.task,
            **execution_kwargs,
        )
    except TypeError as exc:
        if request.run_id is not None and "unexpected keyword argument" in str(exc):
            raise HarnessEntryError(
                "deterministic_execution_identity_unavailable",
                "runtime executor does not support explicit replay execution identities",
            ) from exc
        raise
    reconciliation_targets = (
        (
            provider,
            "replay_reconciliation_failed",
            "provider replay solve did not consume its exact manifest",
        ),
        (
            command_backend,
            "command_replay_reconciliation_failed",
            "command replay solve did not consume its exact manifest",
        ),
    )
    if result.status != "failed":
        for adapter, code, message in reconciliation_targets:
            reconcile = getattr(adapter, "assert_reconciled", None)
            if callable(reconcile):
                try:
                    reconcile()
                except Exception as exc:
                    raise HarnessEntryError(code, message) from exc
    return result


def _credential_reference_from_profile(profile: HarnessDeploymentProfile) -> CredentialReference:
    return CredentialReference(
        provider_name=profile.provider,
        api_key_env=profile.endpoint.api_key_env,
        api_key_file_env=profile.endpoint.api_key_file_env,
    )


def _error_result(exc: Exception) -> HarnessEntryErrorResult:
    if isinstance(exc, (HarnessEntryError, HarnessSolveError, HarnessReleaseLoadError)):
        return HarnessEntryErrorResult(
            code=exc.code,
            message=redact_sensitive_text(exc),
        )
    return HarnessEntryErrorResult(
        code="harness_entry_failed",
        message=f"{type(exc).__name__}: harness entry operation failed",
    )


def main(
    argv: list[str] | None = None,
    *,
    registry: HarnessAdapterRegistry | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="python -m agintor_runtime.runtime_entry")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("--project-root", required=True)
    inspect_parser.add_argument("--output-json", required=True)

    solve_parser = subparsers.add_parser("solve")
    solve_parser.add_argument("--project-root", required=True)
    solve_parser.add_argument("--request-json", required=True)
    solve_parser.add_argument("--output-json", required=True)

    args = parser.parse_args(argv)
    output_path = Path(args.output_json).expanduser().resolve()
    try:
        if args.command == "inspect":
            payload = inspect_harness_release(
                args.project_root,
                registry=registry,
            ).model_dump(mode="json")
        elif args.command == "solve":
            payload = execute_harness_solve_file(
                args.project_root,
                args.request_json,
                registry=registry,
            ).model_dump(mode="json")
        else:
            raise HarnessEntryError("unsupported_command", "unsupported harness entry command")
        _write_json_atomic(output_path, payload)
        return 0
    except Exception as exc:
        _write_json_atomic(output_path, _error_result(exc).model_dump(mode="json"))
        return 2


__all__ = [
    "HARNESS_ENTRY_ERROR_SCHEMA_VERSION",
    "HARNESS_ENTRY_INSPECT_SCHEMA_VERSION",
    "HARNESS_ENTRY_SOLVE_SCHEMA_VERSION",
    "CommandBackendAdapterFactory",
    "HarnessAdapterContext",
    "HarnessAdapterRegistry",
    "HarnessAdapterSelection",
    "HarnessEntryError",
    "HarnessEntryErrorResult",
    "HarnessExecutionSelection",
    "HarnessInspectResult",
    "HarnessLiveExecution",
    "HarnessReplayExecution",
    "HarnessSolveFileRequest",
    "ProviderAdapterFactory",
    "default_harness_adapter_registry",
    "execute_harness_solve_file",
    "inspect_harness_release",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
