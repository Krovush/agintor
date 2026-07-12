from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..contracts.epochs import (
    REPO_REPAIR_CAPABILITY_EPOCH,
    REPO_REPAIR_TRUSTED_TOOL_IDS,
    ResearchEpochManifest,
    TaskEnvelope,
    assert_task_bound_to_epoch,
)
from ..contracts.feasibility import (
    D0LiveBaselineProof,
    DevelopmentTaskFeasibilityManifest,
    d0_evaluation_contract_authority_digest,
)
from ..contracts.harness import HarnessProtocol, RuntimeDependencyManifest
from ..core.identity import canonical_identity_digest, evidence_digest
from ..evaluation.gate0 import (
    Gate0ConformanceReport,
    Gate0DryRunManifest,
    Gate0LiveExecutionAuthorization,
)
from ..evaluation.gate0_runner import Gate0ExecutionReport
from ..runtime.api.composite_compiler import compile_composite_run_plan
from ..runtime.harness_profile import (
    HarnessDeploymentProfile,
    harness_deployment_profile_digest,
)
from ..factory.harness_release import (
    RELEASE_MANIFEST_PATH,
    load_active_release_pointer,
)
from ..factory.harness_release_contracts import (
    ActiveReleasePointer,
    Gate0CompletedReport,
    Gate0NotRunReport,
    Gate0PreregistrationPublic,
    HarnessReleaseManifest,
    HarnessReleaseRequest,
    HarnessRuntimeProfileProjection,
    PilotNotRunSummary,
    PublicSearchLineageRecord,
    PublicSelectionDecision,
)
from ..search.paired_harness import (
    EvaluatorCallback,
    PairedHarnessSearchConfig,
    PairedHarnessSearchResult,
    ProposalCallback,
    canonical_pair_keys,
    paired_task_panel_digest,
    run_paired_harness_search,
)
from ..storage.harness_factory_transaction import (
    HarnessFactoryMessage,
    HarnessFactoryTransactionStore,
)


HARNESS_FACTORY_BUILD_SCHEMA_VERSION = "harness-factory-build-service-v1"
HARNESS_FACTORY_BUILD_EVIDENCE_DIR = (
    "controlled_development_and_evaluator_evidence",
    "factory_builds",
)

HarnessFactoryExecutionMode = Literal[
    "dry_run",
    "offline_scripted",
    "live_provider",
]

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_SECRET_PATTERNS = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
_PROMPT_AUTHORITY_FRAGMENTS = (
    "api",
    "api_key",
    "authorization",
    "bearer",
    "capability",
    "credential",
    "deployment",
    "hidden",
    "key",
    "model",
    "password",
    "private_key",
    "profile",
    "provider",
    "sealed",
    "token",
)


class HarnessFactoryServiceError(RuntimeError):
    """Base class for provider-agnostic harness factory build failures."""


class HarnessFactoryServiceValidationError(HarnessFactoryServiceError, ValueError):
    """Raised when the service input crosses frozen A0/D0/G0/S1 authority."""


class HarnessFactoryExecutionModeError(HarnessFactoryServiceError, ValueError):
    """Raised when a caller requests live or otherwise unsupported execution."""


class HarnessFactoryServiceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _require_identifier(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if (
        not _IDENTIFIER_RE.fullmatch(normalized)
        or normalized.startswith(".")
        or "/" in normalized
        or "\\" in normalized
        or ".." in normalized
    ):
        raise ValueError(f"{field_name} must be a portable non-traversing identifier")
    return normalized


def _require_digest(value: str, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _DIGEST_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return normalized


def _profile_digest(profile: HarnessDeploymentProfile, deployment: Any) -> str:
    profile.validate_deployment_identity(deployment)
    return harness_deployment_profile_digest(profile)


def _sanitize_factory_prompt(
    prompt: str,
) -> str:
    normalized = str(prompt or "").strip()
    if not normalized:
        raise HarnessFactoryServiceValidationError("factory prompt may not be empty")
    if any(pattern.search(normalized) for pattern in _SECRET_PATTERNS):
        raise HarnessFactoryServiceValidationError(
            "factory prompt contains resolved credential material"
        )
    lowered = normalized.casefold()
    compact = re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")
    tokens = set(compact.split("_"))
    for fragment in _PROMPT_AUTHORITY_FRAGMENTS:
        if fragment in {"api_key", "private_key"}:
            crossed = fragment in compact
        elif fragment == "bearer":
            crossed = fragment in tokens or "bearer " in lowered
        else:
            crossed = fragment in tokens
        if crossed:
            raise HarnessFactoryServiceValidationError(
                "factory prompt may not request capability, provider, model, profile, "
                f"credential, or sealed-state changes: {fragment}"
            )
    return normalized


class HarnessFactoryBuildInput(HarnessFactoryServiceModel):
    schema_version: Literal[HARNESS_FACTORY_BUILD_SCHEMA_VERSION] = (
        HARNESS_FACTORY_BUILD_SCHEMA_VERSION
    )
    project_root: str
    factory_prompt: str
    execution_mode: HarnessFactoryExecutionMode
    epoch: ResearchEpochManifest
    task_panel: tuple[TaskEnvelope, ...] = Field(min_length=1)
    dependency_manifest: RuntimeDependencyManifest
    founding_protocol: HarnessProtocol
    deployment_profile: HarnessDeploymentProfile
    gate0_manifest: Gate0DryRunManifest
    gate0_conformance: Gate0ConformanceReport
    d0_manifests: tuple[DevelopmentTaskFeasibilityManifest, ...] = Field(min_length=1)
    gate0_live_authorization: Gate0LiveExecutionAuthorization | None = None
    gate0_execution_report: Gate0ExecutionReport | None = None
    d0_live_proofs: tuple[D0LiveBaselineProof, ...] = ()
    pilot_summary: PilotNotRunSummary
    limitations: tuple[str, ...] = Field(min_length=1)
    s1_config: PairedHarnessSearchConfig
    chat_id: str | None = None
    expected_parent_message_id: str | None = None
    expected_message_index: int | None = None

    @field_validator("project_root")
    @classmethod
    def validate_project_root(cls, value: str) -> str:
        if not str(value or "").strip():
            raise ValueError("project_root may not be empty")
        return str(value)

    @field_validator("chat_id", "expected_parent_message_id")
    @classmethod
    def validate_optional_identifier(cls, value: str | None, info: Any) -> str | None:
        return None if value is None else _require_identifier(value, info.field_name)

    @field_validator("limitations")
    @classmethod
    def validate_nonempty_unique_strings(
        cls,
        value: tuple[str, ...],
        info: Any,
    ) -> tuple[str, ...]:
        normalized = tuple(str(item).strip() for item in value)
        if any(not item for item in normalized):
            raise ValueError(f"{info.field_name} may not contain empty values")
        if len(normalized) != len(set(normalized)):
            raise ValueError(f"{info.field_name} may not contain duplicates")
        return normalized

    @model_validator(mode="after")
    def validate_build_input(self) -> "HarnessFactoryBuildInput":
        _sanitize_factory_prompt(self.factory_prompt)
        if self.s1_config.execution_mode != self.execution_mode:
            raise ValueError("S1 config execution_mode must match the factory build input")
        expected_profile_digest = _profile_digest(
            self.deployment_profile,
            self.epoch.deployment,
        )
        if self.s1_config.deployment_profile_digest != expected_profile_digest:
            raise ValueError("S1 config crossed the frozen deployment profile")
        if self.execution_mode == "dry_run" and (
            self.expected_parent_message_id is not None
            or self.expected_message_index is not None
        ):
            raise ValueError("dry-run factory builds may not target a factory follow-up")
        if self.expected_message_index is not None and self.expected_parent_message_id is None:
            raise ValueError("expected_message_index requires expected_parent_message_id")
        has_gate0_live = (
            self.gate0_live_authorization is not None
            or self.gate0_execution_report is not None
        )
        has_d0_live = bool(self.d0_live_proofs)
        if self.execution_mode == "live_provider":
            if (
                self.gate0_live_authorization is None
                or self.gate0_execution_report is None
                or not self.d0_live_proofs
            ):
                raise ValueError(
                    "live_provider factory input requires Gate0 and D0 live authority/report evidence"
                )
        elif has_gate0_live or has_d0_live:
            raise ValueError(
                "non-live factory input may not carry live Gate0 or D0 evidence"
            )
        return self


class FactoryOpportunity(HarnessFactoryServiceModel):
    opportunity_id: str
    opportunity_kind: Literal[
        "proposal",
        "search_evaluation",
        "control_evaluation",
        "gate0_provider_call",
    ]
    sequence_no: int = Field(ge=0)
    status: Literal["not_run"] = "not_run"
    live_status: Literal["not_run"] = "not_run"
    provider_config_digest: str | None = None
    task_manifest_id: str | None = None
    pair_key_digest: str | None = None

    @field_validator("opportunity_id")
    @classmethod
    def validate_opportunity_id(cls, value: str) -> str:
        return _require_identifier(value, "opportunity_id")

    @field_validator("provider_config_digest", "pair_key_digest")
    @classmethod
    def validate_optional_digest(cls, value: str | None, info: Any) -> str | None:
        return None if value is None else _require_digest(value, info.field_name)


class HarnessFactoryDryRunManifest(HarnessFactoryServiceModel):
    schema_version: Literal["harness-factory-dry-run-manifest-v1"] = (
        "harness-factory-dry-run-manifest-v1"
    )
    build_id: str
    build_digest: str = ""
    created_at_ms: int = Field(ge=0)
    execution_mode: Literal["dry_run"] = "dry_run"
    live_status: Literal["not_run"] = "not_run"
    search_result_digest: str
    search_status: str
    epoch_manifest_digest: str
    task_panel_digest: str
    dependency_manifest_digest: str
    runtime_profile_digest: str
    founding_protocol_digest: str
    gate0_manifest_digest: str
    gate0_conformance_digest: str
    d0_manifest_set_digest: str
    s1_config_digest: str
    prompt_digest: str
    callback_counts: dict[str, int]
    proposal_opportunities: tuple[FactoryOpportunity, ...]
    evaluator_opportunities: tuple[FactoryOpportunity, ...]
    provider_opportunities: tuple[FactoryOpportunity, ...]
    release_published: Literal[False] = False

    @field_validator(
        "search_result_digest",
        "epoch_manifest_digest",
        "task_panel_digest",
        "dependency_manifest_digest",
        "runtime_profile_digest",
        "founding_protocol_digest",
        "gate0_manifest_digest",
        "gate0_conformance_digest",
        "d0_manifest_set_digest",
        "s1_config_digest",
        "prompt_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @model_validator(mode="after")
    def bind_digest(self) -> "HarnessFactoryDryRunManifest":
        if set(self.callback_counts) != {"proposal", "evaluator"}:
            raise ValueError("dry-run callback_counts must name proposal and evaluator")
        if any(value != 0 for value in self.callback_counts.values()):
            raise ValueError("dry-run callback counts must be zero")
        payload = self.model_dump(mode="python", exclude={"build_digest"})
        computed = evidence_digest({"kind": self.schema_version, **payload})
        if self.build_digest and self.build_digest != computed:
            raise ValueError("dry-run build_digest does not match the manifest")
        if not self.build_digest:
            object.__setattr__(self, "build_digest", computed)
        return self


class HarnessFactoryBuildEvidence(HarnessFactoryServiceModel):
    schema_version: Literal["harness-factory-offline-build-evidence-v1"] = (
        "harness-factory-offline-build-evidence-v1"
    )
    build_id: str
    build_digest: str = ""
    created_at_ms: int = Field(ge=0)
    execution_mode: Literal["offline_scripted"] = "offline_scripted"
    live_status: Literal["not_run"] = "not_run"
    search_result_digest: str
    search_execution_status: str
    search_feasibility_status: str
    retained_structural_descendant_count: int = Field(ge=0)
    release_digest: str
    release_manifest_digest: str
    chat_id: str
    message_id: str
    message_index: int = Field(ge=0)
    epoch_manifest_digest: str
    selected_protocol_digest: str
    dependency_manifest_digest: str
    runtime_profile_digest: str
    prompt_digest: str

    @field_validator(
        "search_result_digest",
        "release_digest",
        "release_manifest_digest",
        "epoch_manifest_digest",
        "selected_protocol_digest",
        "dependency_manifest_digest",
        "runtime_profile_digest",
        "prompt_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @field_validator("chat_id", "message_id")
    @classmethod
    def validate_id(cls, value: str, info: Any) -> str:
        return _require_identifier(value, info.field_name)

    @model_validator(mode="after")
    def bind_digest(self) -> "HarnessFactoryBuildEvidence":
        payload = self.model_dump(mode="python", exclude={"build_digest"})
        computed = evidence_digest({"kind": self.schema_version, **payload})
        if self.build_digest and self.build_digest != computed:
            raise ValueError("offline build_digest does not match the evidence")
        if not self.build_digest:
            object.__setattr__(self, "build_digest", computed)
        return self


class HarnessFactoryLiveBuildEvidence(HarnessFactoryServiceModel):
    schema_version: Literal["harness-factory-live-build-evidence-v1"] = (
        "harness-factory-live-build-evidence-v1"
    )
    build_id: str
    build_digest: str = ""
    created_at_ms: int = Field(ge=0)
    execution_mode: Literal["live_provider"] = "live_provider"
    live_status: Literal["completed"] = "completed"
    gate0_authorization_digest: str
    gate0_execution_digest: str
    gate0_real_inference_requests_sent: int = Field(gt=0)
    d0_authorization_digests: dict[str, str] = Field(min_length=1)
    d0_report_digests: dict[str, str] = Field(min_length=1)
    d0_real_inference_requests_sent: int = Field(gt=0)
    search_authorization_digest: str
    search_result_digest: str
    search_real_inference_requests_sent: int = Field(gt=0)
    capability_promotion_authorized: Literal[True] = True
    retained_structural_descendant_count: int = Field(gt=0)
    release_digest: str
    release_manifest_digest: str
    chat_id: str
    message_id: str
    message_index: int = Field(ge=0)
    epoch_manifest_digest: str
    selected_protocol_digest: str
    dependency_manifest_digest: str
    runtime_profile_digest: str
    prompt_digest: str

    @field_validator(
        "gate0_authorization_digest",
        "gate0_execution_digest",
        "search_authorization_digest",
        "search_result_digest",
        "release_digest",
        "release_manifest_digest",
        "epoch_manifest_digest",
        "selected_protocol_digest",
        "dependency_manifest_digest",
        "runtime_profile_digest",
        "prompt_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @field_validator("d0_authorization_digests", "d0_report_digests")
    @classmethod
    def validate_task_digest_map(
        cls,
        value: dict[str, str],
        info: Any,
    ) -> dict[str, str]:
        return {
            _require_identifier(task_id, "task_manifest_id"): _require_digest(
                digest,
                info.field_name,
            )
            for task_id, digest in sorted(value.items())
        }

    @field_validator("chat_id", "message_id")
    @classmethod
    def validate_id(cls, value: str, info: Any) -> str:
        return _require_identifier(value, info.field_name)

    @model_validator(mode="after")
    def bind_digest(self) -> "HarnessFactoryLiveBuildEvidence":
        if set(self.d0_authorization_digests) != set(self.d0_report_digests):
            raise ValueError("live D0 authorization/report task sets differ")
        payload = self.model_dump(mode="python", exclude={"build_digest"})
        computed = evidence_digest({"kind": self.schema_version, **payload})
        if self.build_digest and self.build_digest != computed:
            raise ValueError("live build_digest does not match the evidence")
        if not self.build_digest:
            object.__setattr__(self, "build_digest", computed)
        return self


class HarnessFactoryBuildResult(HarnessFactoryServiceModel):
    schema_version: Literal["harness-factory-build-result-v1"] = (
        "harness-factory-build-result-v1"
    )
    build_id: str
    build_digest: str
    evidence_path: str
    execution_mode: HarnessFactoryExecutionMode
    live_status: Literal["not_run", "completed"] = "not_run"
    search_result_digest: str
    search_execution_status: str
    search_feasibility_status: str
    selected_protocol_digest: str
    dependency_manifest_digest: str
    runtime_profile_digest: str
    release_pointer: ActiveReleasePointer | None = None
    factory_message: HarnessFactoryMessage | None = None
    dry_run_manifest: HarnessFactoryDryRunManifest | None = None
    live_build_evidence: HarnessFactoryLiveBuildEvidence | None = None

    @model_validator(mode="after")
    def validate_mode_evidence(self) -> "HarnessFactoryBuildResult":
        if self.execution_mode == "live_provider":
            if self.live_status != "completed" or self.live_build_evidence is None:
                raise ValueError("live factory result requires completed live evidence")
        elif self.live_status != "not_run" or self.live_build_evidence is not None:
            raise ValueError("non-live factory result may not claim live evidence")
        return self


def build_harness_factory_release(
    build_input: HarnessFactoryBuildInput,
    *,
    proposal_callback: ProposalCallback | None = None,
    evaluator_callback: EvaluatorCallback | None = None,
) -> HarnessFactoryBuildResult:
    """Build a provider-agnostic F1 harness release or dry-run manifest.

    Dry-run and offline-scripted behavior remains provider-free. Live-provider
    mode only orchestrates explicitly supplied callbacks after validating
    independently completed Gate0 and D0 live evidence; it supplies no provider
    implementation or credential resolution path.
    """

    _sanitize_factory_prompt(build_input.factory_prompt)
    if build_input.execution_mode == "dry_run":
        if proposal_callback is not None or evaluator_callback is not None:
            raise HarnessFactoryExecutionModeError(
                "dry-run factory builds may not receive proposal or evaluator callbacks"
            )
        return _build_dry_run(build_input)
    if build_input.execution_mode == "offline_scripted":
        if proposal_callback is None or evaluator_callback is None:
            raise HarnessFactoryExecutionModeError(
                "offline_scripted factory builds require explicit scripted callbacks"
            )
        return _build_offline_scripted(
            build_input,
            proposal_callback=proposal_callback,
            evaluator_callback=evaluator_callback,
        )
    if build_input.execution_mode == "live_provider":
        if proposal_callback is None or evaluator_callback is None:
            raise HarnessFactoryExecutionModeError(
                "live_provider factory builds require explicit proposal and evaluator callbacks"
            )
        return _build_live_provider(
            build_input,
            proposal_callback=proposal_callback,
            evaluator_callback=evaluator_callback,
        )
    raise HarnessFactoryExecutionModeError(
        "unsupported harness factory execution mode"
    )


def _build_dry_run(build_input: HarnessFactoryBuildInput) -> HarnessFactoryBuildResult:
    _validate_common_authority(build_input)
    search_result = run_paired_harness_search(
        epoch=build_input.epoch,
        tasks=build_input.task_panel,
        dependency_manifest=build_input.dependency_manifest,
        founding_protocol=build_input.founding_protocol,
        config=build_input.s1_config,
    )
    manifest = _dry_run_manifest(build_input, search_result)
    evidence_path = _write_controlled_evidence(
        project_root=build_input.project_root,
        build_id=manifest.build_id,
        filename="dry_run_manifest.json",
        payload=manifest.model_dump(mode="json"),
    )
    return HarnessFactoryBuildResult(
        build_id=manifest.build_id,
        build_digest=manifest.build_digest,
        evidence_path=str(evidence_path),
        execution_mode="dry_run",
        search_result_digest=search_result.result_digest,
        search_execution_status=search_result.final_status.execution_status,
        search_feasibility_status=search_result.final_status.feasibility_status,
        selected_protocol_digest=search_result.final_protocol.source_digest(),
        dependency_manifest_digest=build_input.dependency_manifest.manifest_digest(),
        runtime_profile_digest=_profile_digest(
            build_input.deployment_profile,
            build_input.epoch.deployment,
        ),
        dry_run_manifest=manifest,
    )


def _build_offline_scripted(
    build_input: HarnessFactoryBuildInput,
    *,
    proposal_callback: ProposalCallback,
    evaluator_callback: EvaluatorCallback,
) -> HarnessFactoryBuildResult:
    _validate_common_authority(build_input)
    _validate_followup_identity(build_input)
    search_result = run_paired_harness_search(
        epoch=build_input.epoch,
        tasks=build_input.task_panel,
        dependency_manifest=build_input.dependency_manifest,
        founding_protocol=build_input.founding_protocol,
        config=build_input.s1_config,
        proposal_callback=proposal_callback,
        evaluator_callback=evaluator_callback,
    )
    _assert_search_retained_structural_evidence(search_result)
    request, selection_evidence = _release_request_from_search(build_input, search_result)
    store = HarnessFactoryTransactionStore(build_input.project_root)
    if build_input.expected_parent_message_id is None:
        message = store.create_initial_chat(
            request=request,
            user_prompt_text=build_input.factory_prompt,
            chat_id=build_input.chat_id,
            search_result_digest=search_result.result_digest,
            selection_evidence_digests=selection_evidence,
        )
    else:
        message = store.apply_followup(
            request=request,
            user_prompt_text=build_input.factory_prompt,
            expected_parent_message_id=build_input.expected_parent_message_id,
            expected_message_index=build_input.expected_message_index,
            search_result_digest=search_result.result_digest,
            selection_evidence_digests=selection_evidence,
        )
    pointer = load_active_release_pointer(build_input.project_root)
    if pointer is None:
        raise HarnessFactoryServiceError("factory transaction did not advance an active release pointer")
    evidence = HarnessFactoryBuildEvidence(
        build_id=_build_id("offline_scripted", build_input, search_result),
        created_at_ms=_now_ms(),
        search_result_digest=search_result.result_digest,
        search_execution_status=search_result.final_status.execution_status,
        search_feasibility_status=search_result.final_status.feasibility_status,
        retained_structural_descendant_count=_structural_retained_count(search_result),
        release_digest=message.new_release_digest,
        release_manifest_digest=message.new_manifest_digest,
        chat_id=message.chat_id,
        message_id=message.message_id,
        message_index=message.message_index,
        epoch_manifest_digest=build_input.epoch.epoch_manifest_digest,
        selected_protocol_digest=search_result.final_protocol.source_digest(),
        dependency_manifest_digest=build_input.dependency_manifest.manifest_digest(),
        runtime_profile_digest=_profile_digest(
            build_input.deployment_profile,
            build_input.epoch.deployment,
        ),
        prompt_digest=evidence_digest(
            {"kind": "harness-factory-prompt-v1", "text": build_input.factory_prompt}
        ),
    )
    evidence_path = _write_controlled_evidence(
        project_root=build_input.project_root,
        build_id=evidence.build_id,
        filename="offline_scripted_build.json",
        payload=evidence.model_dump(mode="json"),
    )
    return HarnessFactoryBuildResult(
        build_id=evidence.build_id,
        build_digest=evidence.build_digest,
        evidence_path=str(evidence_path),
        execution_mode="offline_scripted",
        search_result_digest=search_result.result_digest,
        search_execution_status=search_result.final_status.execution_status,
        search_feasibility_status=search_result.final_status.feasibility_status,
        selected_protocol_digest=search_result.final_protocol.source_digest(),
        dependency_manifest_digest=build_input.dependency_manifest.manifest_digest(),
        runtime_profile_digest=_profile_digest(
            build_input.deployment_profile,
            build_input.epoch.deployment,
        ),
        release_pointer=pointer,
        factory_message=message,
    )


def _build_live_provider(
    build_input: HarnessFactoryBuildInput,
    *,
    proposal_callback: ProposalCallback,
    evaluator_callback: EvaluatorCallback,
) -> HarnessFactoryBuildResult:
    _validate_common_authority(build_input)
    _validate_live_prerequisites(build_input)
    _validate_followup_identity(build_input)
    search_result = run_paired_harness_search(
        epoch=build_input.epoch,
        tasks=build_input.task_panel,
        dependency_manifest=build_input.dependency_manifest,
        founding_protocol=build_input.founding_protocol,
        config=build_input.s1_config,
        proposal_callback=proposal_callback,
        evaluator_callback=evaluator_callback,
    )
    _assert_live_search_release_authority(search_result)
    request, selection_evidence = _release_request_from_search(
        build_input,
        search_result,
    )
    store = HarnessFactoryTransactionStore(build_input.project_root)
    if build_input.expected_parent_message_id is None:
        message = store.create_initial_chat(
            request=request,
            user_prompt_text=build_input.factory_prompt,
            chat_id=build_input.chat_id,
            search_result_digest=search_result.result_digest,
            selection_evidence_digests=selection_evidence,
        )
    else:
        message = store.apply_followup(
            request=request,
            user_prompt_text=build_input.factory_prompt,
            expected_parent_message_id=build_input.expected_parent_message_id,
            expected_message_index=build_input.expected_message_index,
            search_result_digest=search_result.result_digest,
            selection_evidence_digests=selection_evidence,
        )
    pointer = load_active_release_pointer(build_input.project_root)
    if pointer is None:
        raise HarnessFactoryServiceError(
            "live factory transaction did not advance an active release pointer"
        )

    gate0_authorization = build_input.gate0_live_authorization
    gate0_report = build_input.gate0_execution_report
    live_search_authorization = build_input.s1_config.live_authorization
    if (
        gate0_authorization is None
        or gate0_report is None
        or live_search_authorization is None
    ):
        raise HarnessFactoryServiceValidationError(
            "validated live prerequisites disappeared before evidence persistence"
        )
    d0_proofs_by_task = _d0_live_proofs_by_task(build_input)
    evidence = HarnessFactoryLiveBuildEvidence(
        build_id=_build_id("live_provider", build_input, search_result),
        created_at_ms=_now_ms(),
        gate0_authorization_digest=gate0_authorization.authorization_digest,
        gate0_execution_digest=gate0_report.execution_digest,
        gate0_real_inference_requests_sent=gate0_report.completed_call_count,
        d0_authorization_digests={
            task_id: proof.authorization_digest
            for task_id, proof in d0_proofs_by_task.items()
        },
        d0_report_digests={
            task_id: proof.report_digest
            for task_id, proof in d0_proofs_by_task.items()
        },
        d0_real_inference_requests_sent=sum(
            proof.real_inference_requests_sent
            for proof in d0_proofs_by_task.values()
        ),
        search_authorization_digest=(
            live_search_authorization.authorization_digest
        ),
        search_result_digest=search_result.result_digest,
        search_real_inference_requests_sent=(
            search_result.final_status.inference_requests_sent
        ),
        retained_structural_descendant_count=_structural_retained_count(
            search_result
        ),
        release_digest=message.new_release_digest,
        release_manifest_digest=message.new_manifest_digest,
        chat_id=message.chat_id,
        message_id=message.message_id,
        message_index=message.message_index,
        epoch_manifest_digest=build_input.epoch.epoch_manifest_digest,
        selected_protocol_digest=search_result.final_protocol.source_digest(),
        dependency_manifest_digest=build_input.dependency_manifest.manifest_digest(),
        runtime_profile_digest=_profile_digest(
            build_input.deployment_profile,
            build_input.epoch.deployment,
        ),
        prompt_digest=evidence_digest(
            {"kind": "harness-factory-prompt-v1", "text": build_input.factory_prompt}
        ),
    )
    evidence_path = _write_controlled_evidence(
        project_root=build_input.project_root,
        build_id=evidence.build_id,
        filename="live_provider_build.json",
        payload=evidence.model_dump(mode="json"),
    )
    return HarnessFactoryBuildResult(
        build_id=evidence.build_id,
        build_digest=evidence.build_digest,
        evidence_path=str(evidence_path),
        execution_mode="live_provider",
        live_status="completed",
        search_result_digest=search_result.result_digest,
        search_execution_status=search_result.final_status.execution_status,
        search_feasibility_status=search_result.final_status.feasibility_status,
        selected_protocol_digest=search_result.final_protocol.source_digest(),
        dependency_manifest_digest=build_input.dependency_manifest.manifest_digest(),
        runtime_profile_digest=_profile_digest(
            build_input.deployment_profile,
            build_input.epoch.deployment,
        ),
        release_pointer=pointer,
        factory_message=message,
        live_build_evidence=evidence,
    )


def _validate_common_authority(build_input: HarnessFactoryBuildInput) -> None:
    epoch = build_input.epoch
    if epoch.capability_epoch != REPO_REPAIR_CAPABILITY_EPOCH or not epoch.promotion_capable:
        raise HarnessFactoryServiceValidationError("A0 epoch is not repo-repair promotion capable")
    for task in build_input.task_panel:
        try:
            assert_task_bound_to_epoch(task, epoch)
        except ValueError as exc:
            raise HarnessFactoryServiceValidationError(str(exc)) from exc
        if task.data_state != "development":
            raise HarnessFactoryServiceValidationError("sealed-confirmation tasks cannot enter F1")
        if tuple(task.allowed_capabilities) != REPO_REPAIR_TRUSTED_TOOL_IDS:
            raise HarnessFactoryServiceValidationError("task allowed capabilities crossed A0 authority")
    if build_input.s1_config.expected_pair_keys != canonical_pair_keys(
        build_input.s1_config.expected_pair_keys
    ):
        raise HarnessFactoryServiceValidationError("S1 PairKeys are not canonical")
    if paired_task_panel_digest(build_input.s1_config.expected_pair_keys) != epoch.search_envelope.task_panel_digest:
        raise HarnessFactoryServiceValidationError("S1 PairKey panel crossed the A0 epoch")
    pair_task_ids = {pair.task_manifest_id for pair in build_input.s1_config.expected_pair_keys}
    if pair_task_ids != {task.task_manifest_id for task in build_input.task_panel}:
        raise HarnessFactoryServiceValidationError("S1 PairKeys do not exactly cover the task panel")
    if any(
        pair.provider_config_digest != epoch.deployment.provider_config_digest
        for pair in build_input.s1_config.expected_pair_keys
    ):
        raise HarnessFactoryServiceValidationError("S1 PairKeys crossed the deployment provider config")
    _validate_dependency_authority(epoch, build_input.dependency_manifest)
    _validate_runtime_profile(epoch, build_input.deployment_profile)
    _validate_gate0(build_input)
    _validate_d0_manifests(build_input)
    _validate_pilot(build_input)


def _provider_dry_run_digest(value: Any) -> str:
    return evidence_digest(
        {
            "kind": "repo-repair-d0-provider-baseline-dry-run-v1",
            **value.model_dump(mode="python"),
        }
    )


def _d0_live_proofs_by_task(
    build_input: HarnessFactoryBuildInput,
) -> dict[str, D0LiveBaselineProof]:
    proofs: dict[str, D0LiveBaselineProof] = {}
    for proof in build_input.d0_live_proofs:
        task_id = proof.task_manifest_id
        if task_id in proofs:
            raise HarnessFactoryServiceValidationError(
                "D0 live proofs contain duplicate task identities"
            )
        proofs[task_id] = proof
    return proofs


def _validate_live_prerequisites(build_input: HarnessFactoryBuildInput) -> None:
    if build_input.execution_mode != "live_provider":
        raise HarnessFactoryServiceValidationError(
            "live prerequisite validation requires live_provider mode"
        )
    profile = build_input.deployment_profile
    profile_digest = _profile_digest(profile, build_input.epoch.deployment)
    gate0_authorization = build_input.gate0_live_authorization
    gate0_report = build_input.gate0_execution_report
    if gate0_authorization is None or gate0_report is None:
        raise HarnessFactoryServiceValidationError(
            "Gate0 live authorization and execution report are required"
        )
    if (
        gate0_authorization.manifest_digest
        != build_input.gate0_manifest.manifest_digest
        or gate0_authorization.provider_identity
        != build_input.gate0_manifest.provider_identity
        or gate0_authorization.deployment_profile != profile
        or gate0_authorization.profile_digest != profile_digest
    ):
        raise HarnessFactoryServiceValidationError(
            "Gate0 live authorization crossed manifest, provider, or profile authority"
        )
    analysis = gate0_report.analysis
    if (
        gate0_report.manifest_digest != build_input.gate0_manifest.manifest_digest
        or gate0_report.authorization_digest
        != gate0_authorization.authorization_digest
        or gate0_report.profile_digest != profile_digest
        or gate0_report.provenance != "authorized_live"
        or gate0_report.live_status != "executed"
        or gate0_report.status != "completed"
        or not gate0_report.numerical_gate_passed
        or analysis is None
        or not analysis.numerical_gate_passed
        or analysis.live_status != "executed"
        or gate0_report.scheduled_call_count
        != build_input.gate0_manifest.total_provider_calls
        or gate0_report.completed_call_count != gate0_report.scheduled_call_count
        or gate0_report.completed_arm_count != len(build_input.gate0_manifest.arms)
        or gate0_report.observed_priced_input_units
        != build_input.gate0_manifest.total_priced_input_units
        or len(gate0_report.call_observation_digests)
        != gate0_report.scheduled_call_count
        or len(gate0_report.arm_observation_digests)
        != gate0_report.completed_arm_count
        or gate0_report.unknown_usage_event_count
        or gate0_report.unknown_cost_event_count
    ):
        raise HarnessFactoryServiceValidationError(
            "Gate0 live execution did not complete its exact numerical-pass schedule"
        )

    tasks = {task.task_manifest_id: task for task in build_input.task_panel}
    d0_manifests = {
        manifest.task_manifest_id: manifest for manifest in build_input.d0_manifests
    }
    d0_proofs = _d0_live_proofs_by_task(build_input)
    if set(d0_proofs) != set(tasks) or set(d0_manifests) != set(tasks):
        raise HarnessFactoryServiceValidationError(
            "D0 live proofs must exactly cover the S1 task panel"
        )
    baseline_protocol_digest = build_input.founding_protocol.source_digest()
    deployment = build_input.epoch.deployment
    evaluator = build_input.epoch.evaluator_authority
    for task_id, task in tasks.items():
        manifest = d0_manifests[task_id]
        dry_run = manifest.provider_baseline_dry_run
        proof = d0_proofs[task_id]
        expected = {
            "epoch_id": build_input.epoch.epoch_id,
            "epoch_manifest_digest": build_input.epoch.epoch_manifest_digest,
            "task_manifest_digest": task.task_manifest_digest,
            "deployment_id": deployment.deployment_id,
            "provider": deployment.provider,
            "model": deployment.model,
            "provider_config_digest": deployment.provider_config_digest,
            "decoding_policy_digest": deployment.decoding_policy_digest,
            "price_schedule_digest": deployment.price_schedule_digest,
            "command_container_policy_digest": (
                deployment.command_container_policy_digest
            ),
            "profile_digest": profile_digest,
            "evaluator_id": evaluator.evaluator_id,
            "evaluator_identity_digest": evaluator.evaluator_identity_digest,
            "evaluation_policy_digest": evaluator.evaluation_policy_digest,
            "evaluation_contract_authority_digest": (
                d0_evaluation_contract_authority_digest(
                    evaluation_contract_id=manifest.evaluation_contract_id,
                    evaluation_contract_digest=manifest.evaluation_contract_digest,
                )
            ),
            "baseline_protocol_digest": baseline_protocol_digest,
            "provider_dry_run_digest": _provider_dry_run_digest(dry_run),
        }
        crossed = [
            field_name
            for field_name, expected_value in expected.items()
            if getattr(proof, field_name) != expected_value
        ]
        if crossed:
            raise HarnessFactoryServiceValidationError(
                f"D0 live proof crossed authority for {task_id}: "
                + ", ".join(crossed)
            )
        if (
            proof.task_manifest_id != task_id
            or proof.pair_keys != dry_run.pair_keys
            or proof.scheduled_pair_count != len(dry_run.pair_keys)
            or proof.baseline_headroom_status != "has_headroom"
            or proof.complete_repairs <= 0
            or proof.failures <= 0
            or proof.real_inference_requests_sent <= 0
            or proof.total_model_calls != proof.real_inference_requests_sent
            or proof.unknown_usage_event_count != 0
            or proof.unknown_cost_event_count != 0
            or proof.total_known_cost_usd
            > task.ceilings.max_known_cost_usd * proof.receipt_count
            or (
                proof.total_known_cost_usd + proof.total_estimated_cost_usd
                > task.ceilings.max_estimated_cost_usd * proof.receipt_count
            )
        ):
            raise HarnessFactoryServiceValidationError(
                f"D0 live proof did not establish bounded mixed headroom for {task_id}"
            )

    search_authorization = build_input.s1_config.live_authorization
    if (
        search_authorization is None
        or search_authorization.epoch_id != build_input.epoch.epoch_id
        or search_authorization.epoch_manifest_digest
        != build_input.epoch.epoch_manifest_digest
        or search_authorization.deployment_profile_digest != profile_digest
        or search_authorization.provider_config_digest
        != deployment.provider_config_digest
    ):
        raise HarnessFactoryServiceValidationError(
            "S1 live authorization crossed epoch, profile, or provider authority"
        )


def _validate_dependency_authority(
    epoch: ResearchEpochManifest,
    dependency_manifest: RuntimeDependencyManifest,
) -> None:
    epoch_tools = {
        tool.tool_id: (tool.implementation_digest, tool.policy_digest)
        for tool in epoch.trusted_tools
    }
    dependency_tools = {
        tool.tool_id: (tool.implementation_digest, tool.policy_digest)
        for tool in dependency_manifest.trusted_tools
    }
    if epoch_tools != dependency_tools:
        raise HarnessFactoryServiceValidationError(
            "runtime dependency manifest crossed A0 trusted-tool authority"
        )
    if dependency_manifest.runtime_contract_version != epoch.runtime_contract_version:
        raise HarnessFactoryServiceValidationError(
            "runtime dependency contract crossed the A0 epoch"
        )


def _validate_runtime_profile(epoch: ResearchEpochManifest, profile: HarnessDeploymentProfile) -> None:
    try:
        profile.validate_deployment_identity(epoch.deployment)
    except ValueError as exc:
        raise HarnessFactoryServiceValidationError(
            "deployment profile crossed the pinned deployment"
        ) from exc


def _validate_gate0(build_input: HarnessFactoryBuildInput) -> None:
    manifest = build_input.gate0_manifest
    conformance = build_input.gate0_conformance
    deployment = build_input.epoch.deployment
    if manifest.live_status != "not_run" or conformance.live_status != "not_run":
        raise HarnessFactoryServiceValidationError("Gate0 must remain not_run for F1 builds")
    if conformance.manifest_digest != manifest.manifest_digest or not conformance.passed:
        raise HarnessFactoryServiceValidationError("Gate0 deterministic conformance did not pass")
    provider = manifest.provider_identity
    if (
        provider.deployment_id != deployment.deployment_id
        or provider.provider.casefold() != deployment.provider.casefold()
        or provider.model != deployment.model
    ):
        raise HarnessFactoryServiceValidationError("Gate0 provider identity crossed deployment authority")


def _validate_d0_manifests(build_input: HarnessFactoryBuildInput) -> None:
    by_task = {task.task_manifest_id: task for task in build_input.task_panel}
    d0_by_task: dict[str, DevelopmentTaskFeasibilityManifest] = {}
    baseline_pair_keys = []
    for d0 in build_input.d0_manifests:
        if d0.task_manifest_id in d0_by_task:
            raise HarnessFactoryServiceValidationError("D0 prerequisite reports contain duplicate task ids")
        d0_by_task[d0.task_manifest_id] = d0
    if set(d0_by_task) != set(by_task):
        raise HarnessFactoryServiceValidationError("D0 prerequisite reports must exactly cover the search task panel")
    for task_id, d0 in d0_by_task.items():
        _validate_one_d0(build_input, d0, by_task[task_id])
        baseline_pair_keys.extend(d0.provider_baseline_dry_run.pair_keys)
    try:
        baseline_panel = canonical_pair_keys(tuple(baseline_pair_keys))
    except ValueError as exc:
        raise HarnessFactoryServiceValidationError("D0 baseline PairKeys are not canonical") from exc
    if baseline_panel != build_input.s1_config.expected_pair_keys:
        raise HarnessFactoryServiceValidationError("D0 baseline PairKey union crossed the S1 task panel")
    if paired_task_panel_digest(baseline_panel) != build_input.epoch.search_envelope.task_panel_digest:
        raise HarnessFactoryServiceValidationError("D0 baseline PairKey union crossed the A0 task panel")


def _validate_one_d0(
    build_input: HarnessFactoryBuildInput,
    d0: DevelopmentTaskFeasibilityManifest,
    task: TaskEnvelope,
) -> None:
    epoch = build_input.epoch
    if d0.status != "pending_real_provider_baseline" or d0.search_authorized:
        raise HarnessFactoryServiceValidationError(
            "factory requires product D0 pending live baseline without capability authority"
        )
    if (
        not d0.offline_controls_passed
        or not d0.clean_replay_reproducible
        or not d0.protected_path_integrity
        or not d0.leakage_integrity
        or not d0.identity_integrity
        or d0.baseline_headroom.status != "not_measured"
        or not d0.paired_search_projection.fits_frozen_epoch_budget
    ):
        raise HarnessFactoryServiceValidationError(
            "D0 deterministic controls, integrity, replay, or budget checks did not pass"
        )
    if (
        d0.epoch_id != epoch.epoch_id
        or d0.epoch_manifest_digest != epoch.epoch_manifest_digest
        or d0.task_manifest_id != task.task_manifest_id
        or d0.task_manifest_digest != task.task_manifest_digest
        or d0.data_state != "development"
    ):
        raise HarnessFactoryServiceValidationError("D0 prerequisite report crossed task or epoch authority")
    baseline = d0.provider_baseline_dry_run
    deployment = epoch.deployment
    if (
        baseline.real_provider_baseline_status != "not_run"
        or baseline.inference_authorized is not False
        or baseline.deployment_id != deployment.deployment_id
        or baseline.provider.casefold() != deployment.provider.casefold()
        or baseline.model != deployment.model
        or baseline.provider_config_digest != deployment.provider_config_digest
    ):
        raise HarnessFactoryServiceValidationError("D0 provider baseline crossed deployment authority")
    if baseline.baseline_protocol_digest != build_input.founding_protocol.source_digest():
        raise HarnessFactoryServiceValidationError(
            "D0 pending baseline did not pin the founding protocol"
        )
    if baseline.planned_provider_calls != len(baseline.pair_keys):
        raise HarnessFactoryServiceValidationError(
            "D0 provider dry-run call count crossed its PairKeys"
        )
    pair_task_ids = {pair.task_manifest_id for pair in baseline.pair_keys}
    if pair_task_ids != {task.task_manifest_id}:
        raise HarnessFactoryServiceValidationError("D0 PairKeys crossed their development task")
    if len(baseline.pair_keys) != epoch.search_envelope.sampling_replicates:
        raise HarnessFactoryServiceValidationError("D0 PairKeys crossed the frozen replicate count")


def _validate_pilot(build_input: HarnessFactoryBuildInput) -> None:
    pilot = build_input.pilot_summary
    if pilot.status != "not_run" or not pilot.non_confirmatory:
        raise HarnessFactoryServiceValidationError("pilot plan must be a non-confirmatory not_run plan")
    search_task_digests = {task.task_manifest_digest for task in build_input.task_panel}
    if pilot.planned_task_manifest_digest in search_task_digests:
        raise HarnessFactoryServiceValidationError("pilot task must be held out from the S1 search task panel")


def _validate_followup_identity(build_input: HarnessFactoryBuildInput) -> None:
    if build_input.expected_parent_message_id is None:
        return
    pointer = load_active_release_pointer(build_input.project_root)
    if pointer is None:
        raise HarnessFactoryServiceValidationError("factory follow-up requires an active release pointer")
    manifest_path = Path(build_input.project_root).resolve() / pointer.release_path / RELEASE_MANIFEST_PATH
    active = HarnessReleaseManifest.model_validate(
        json.loads(manifest_path.read_text(encoding="utf-8"))
    )
    profile_digest = _profile_digest(build_input.deployment_profile, build_input.epoch.deployment)
    if active.epoch_manifest_digest != build_input.epoch.epoch_manifest_digest:
        raise HarnessFactoryServiceValidationError("follow-up crossed the pinned research epoch")
    if active.deployment != build_input.epoch.deployment:
        raise HarnessFactoryServiceValidationError("follow-up crossed the pinned deployment")
    if active.dependency_manifest_digest != build_input.dependency_manifest.manifest_digest():
        raise HarnessFactoryServiceValidationError("follow-up crossed runtime dependencies")
    if active.profile_digest != profile_digest:
        raise HarnessFactoryServiceValidationError("follow-up crossed the frozen runtime profile")
    if active.protocol_source_digest != build_input.founding_protocol.source_digest():
        raise HarnessFactoryServiceValidationError("follow-up founding protocol crossed the active release")


def _assert_search_retained_structural_evidence(result: PairedHarnessSearchResult) -> None:
    if result.final_status.live_inference_status != "not_run":
        raise HarnessFactoryServiceValidationError("S1 attempted live inference")
    if result.final_status.feasibility_status != "search_viable":
        raise HarnessFactoryServiceValidationError(
            "S1 did not retain an A0b-authorized outcome-improving descendant"
        )
    if result.capability_promotion_authorized:
        raise HarnessFactoryServiceValidationError(
            "offline S1 may retain implementation evidence but cannot publish capability authority"
        )
    if _structural_retained_count(result) < 1:
        raise HarnessFactoryServiceValidationError(
            "F1 release requires at least one retained non-prompt descendant as implementation evidence"
        )


def _assert_live_search_release_authority(
    result: PairedHarnessSearchResult,
) -> None:
    if (
        result.execution_mode != "live_provider"
        or result.final_status.live_inference_status != "completed"
        or result.final_status.inference_requests_sent <= 0
        or not result.capability_promotion_authorized
    ):
        raise HarnessFactoryServiceValidationError(
            "live S1 did not produce completed positive-request capability authority"
        )
    if result.final_status.feasibility_status != "search_viable":
        raise HarnessFactoryServiceValidationError(
            "live S1 did not retain an outcome-improving descendant"
        )
    if _structural_retained_count(result) < 1:
        raise HarnessFactoryServiceValidationError(
            "live release requires a retained non-prompt structural descendant"
        )


def _structural_retained_count(result: PairedHarnessSearchResult) -> int:
    return sum(
        1
        for transaction in result.retained_transactions
        if transaction.treatment_class != "prompt_only_control"
    )


def _release_request_from_search(
    build_input: HarnessFactoryBuildInput,
    search_result: PairedHarnessSearchResult,
) -> tuple[HarnessReleaseRequest, tuple[str, ...]]:
    task = _representative_search_task(build_input.task_panel)
    selected_protocol = search_result.final_protocol
    representative_plan = compile_composite_run_plan(
        task,
        selected_protocol,
        build_input.dependency_manifest,
    )
    lineage = _public_lineage(search_result)
    decisions = _public_decisions(search_result)
    if not lineage:
        raise HarnessFactoryServiceValidationError("S1 produced no public lineage for F1 release")
    if not decisions:
        raise HarnessFactoryServiceValidationError("S1 produced no public selection decisions")
    selection_evidence = tuple(
        sorted(
            {
                digest
                for decision in decisions
                for digest in decision.evidence_digests
            }
        )
    )
    if not selection_evidence:
        selection_evidence = (search_result.result_digest,)
    preregistration = Gate0PreregistrationPublic(
        preregistration_id=build_input.gate0_manifest.manifest_id,
        panel_digest=build_input.gate0_manifest.panel.panel_digest,
        deterministic_suite_digest=evidence_digest(
            {
                "kind": "gate0-deterministic-conformance-public-v1",
                "conformance": build_input.gate0_conformance.model_dump(mode="json"),
            }
        ),
        planned_provider_calls=build_input.gate0_manifest.total_provider_calls,
        frozen_thresholds=build_input.gate0_manifest.thresholds.model_dump(mode="json"),
    )
    if build_input.execution_mode == "live_provider":
        authorization = build_input.gate0_live_authorization
        execution = build_input.gate0_execution_report
        if authorization is None or execution is None or execution.analysis is None:
            raise HarnessFactoryServiceValidationError(
                "validated Gate0 live evidence is unavailable for release projection"
            )
        gate0_report: Gate0NotRunReport | Gate0CompletedReport = (
            Gate0CompletedReport(
                preregistration_digest=preregistration.preregistration_digest,
                authorization_digest=authorization.authorization_digest,
                profile_digest=authorization.profile_digest,
                manifest_digest=execution.manifest_digest,
                execution_digest=execution.execution_digest,
                analysis_digest=execution.analysis.analysis_digest,
                scheduled_call_count=execution.scheduled_call_count,
                completed_call_count=execution.completed_call_count,
                real_inference_requests_sent=execution.completed_call_count,
                total_known_cost_usd=execution.total_known_cost_usd,
                total_estimated_cost_usd=execution.total_estimated_cost_usd,
            )
        )
    else:
        gate0_report = Gate0NotRunReport(
            preregistration_digest=preregistration.preregistration_digest,
        )
    request = HarnessReleaseRequest(
        epoch=build_input.epoch,
        selected_protocol=selected_protocol,
        representative_plan=representative_plan,
        dependency_manifest=build_input.dependency_manifest,
        deployment_profile=build_input.deployment_profile,
        deployment=build_input.epoch.deployment,
        search_lineage=lineage,
        selection_decisions=decisions,
        gate0_preregistration=preregistration,
        gate0_report=gate0_report,
        search_execution_mode=(
            "live_provider"
            if build_input.execution_mode == "live_provider"
            else "offline_scripted"
        ),
        capability_promotion_authorized=(
            search_result.capability_promotion_authorized
        ),
        capability_promotion_reason=search_result.capability_promotion_reason,
        pilot_summary=build_input.pilot_summary,
        limitations=build_input.limitations,
    )
    return request, selection_evidence


def _representative_search_task(tasks: Sequence[TaskEnvelope]) -> TaskEnvelope:
    return tuple(sorted(tasks, key=lambda task: task.task_manifest_id))[0]


def _public_lineage(
    search_result: PairedHarnessSearchResult,
) -> tuple[PublicSearchLineageRecord, ...]:
    records: list[PublicSearchLineageRecord] = []
    for record in search_result.candidate_lineage:
        transaction = record.transaction
        if transaction is None:
            continue
        records.append(
            PublicSearchLineageRecord(
                sequence_no=len(records),
                transaction_id=transaction.transaction_id,
                operator=transaction.operator,
                parent_protocol_digest=transaction.parent_source_protocol_digest,
                child_protocol_digest=transaction.child_source_protocol_digest,
                transaction_digest=transaction.transaction_record_digest,
                mechanism_hypothesis_digest=evidence_digest(
                    {
                        "kind": "public-mechanism-hypothesis-digest-v1",
                        "transaction_id": transaction.transaction_id,
                        "hypothesis": transaction.mechanism_hypothesis,
                    }
                ),
                status="accepted" if record.status == "promoted" else "rejected",
            )
        )
    return tuple(records)


def _public_decisions(
    search_result: PairedHarnessSearchResult,
) -> tuple[PublicSelectionDecision, ...]:
    candidate_protocols = {
        record.candidate_id: record.protocol.source_digest()
        for record in search_result.candidate_lineage
        if record.protocol is not None
    }
    candidate_evidence = {
        record.candidate_id: tuple(
            sorted(
                set(
                    (
                        *(
                            binding.outcome_receipt.receipt_digest
                            for binding in record.child_proof_bindings
                        ),
                        *(
                            (record.promotion_authorization.authorization_digest,)
                            if record.promotion_authorization is not None
                            else ()
                        ),
                        *(
                            (record.transaction.transaction_record_digest,)
                            if record.transaction is not None
                            else ()
                        ),
                    )
                )
            )
        )
        for record in search_result.candidate_lineage
    }
    decisions: list[PublicSelectionDecision] = []
    incumbent_digest = search_result.founding_protocol.source_digest()
    for decision in search_result.selection_decisions:
        selected_digest = candidate_protocols.get(
            decision.selected_candidate_id,
            incumbent_digest,
        )
        retained_candidate = decision.selected_candidate_id != decision.incumbent_before_id
        candidate_digest = selected_digest
        if not retained_candidate and decision.candidate_ids:
            candidate_digest = candidate_protocols.get(
                decision.candidate_ids[0],
                incumbent_digest,
            )
        evidence_digests = candidate_evidence.get(
            decision.selected_candidate_id,
            (search_result.result_digest,),
        )
        decisions.append(
            PublicSelectionDecision(
                sequence_no=len(decisions),
                decision_id=f"decision.{search_result.search_id}.{decision.step_index}",
                incumbent_protocol_digest=incumbent_digest,
                candidate_protocol_digest=candidate_digest,
                selected_protocol_digest=selected_digest,
                decision="retain_candidate" if retained_candidate else "retain_incumbent",
                reason_codes=(
                    "a0b_authorized_structural_retention"
                    if retained_candidate
                    else "no_authorized_improvement"
                ,),
                evidence_digests=evidence_digests,
            )
        )
        incumbent_digest = selected_digest
    return tuple(decisions)


def _dry_run_manifest(
    build_input: HarnessFactoryBuildInput,
    search_result: PairedHarnessSearchResult,
) -> HarnessFactoryDryRunManifest:
    proposal_opportunities = _proposal_opportunities(build_input)
    evaluator_opportunities = _evaluator_opportunities(build_input)
    provider_opportunities = _gate0_provider_opportunities(build_input)
    build_id = _build_id("dry_run", build_input, search_result)
    return HarnessFactoryDryRunManifest(
        build_id=build_id,
        created_at_ms=_now_ms(),
        search_result_digest=search_result.result_digest,
        search_status=search_result.final_status.execution_status,
        epoch_manifest_digest=build_input.epoch.epoch_manifest_digest,
        task_panel_digest=build_input.epoch.search_envelope.task_panel_digest,
        dependency_manifest_digest=build_input.dependency_manifest.manifest_digest(),
        runtime_profile_digest=_profile_digest(
            build_input.deployment_profile,
            build_input.epoch.deployment,
        ),
        founding_protocol_digest=build_input.founding_protocol.source_digest(),
        gate0_manifest_digest=build_input.gate0_manifest.manifest_digest,
        gate0_conformance_digest=evidence_digest(
            {
                "kind": "gate0-conformance-report-v1",
                "report": build_input.gate0_conformance.model_dump(mode="json"),
            }
        ),
        d0_manifest_set_digest=_d0_manifest_set_digest(build_input.d0_manifests),
        s1_config_digest=_s1_config_digest(build_input.s1_config),
        prompt_digest=evidence_digest(
            {"kind": "harness-factory-prompt-v1", "text": build_input.factory_prompt}
        ),
        callback_counts={"proposal": 0, "evaluator": 0},
        proposal_opportunities=proposal_opportunities,
        evaluator_opportunities=evaluator_opportunities,
        provider_opportunities=provider_opportunities,
    )


def _proposal_opportunities(
    build_input: HarnessFactoryBuildInput,
) -> tuple[FactoryOpportunity, ...]:
    max_opportunities = min(
        build_input.epoch.stop_rule.max_candidate_evaluations,
        build_input.epoch.search_envelope.max_steps
        * build_input.epoch.search_envelope.offspring_per_step,
    )
    return tuple(
        FactoryOpportunity(
            opportunity_id=f"proposal.{index}",
            opportunity_kind="proposal",
            sequence_no=index,
            provider_config_digest=build_input.epoch.deployment.provider_config_digest,
            task_manifest_id=None,
        )
        for index in range(max_opportunities)
    )


def _evaluator_opportunities(
    build_input: HarnessFactoryBuildInput,
) -> tuple[FactoryOpportunity, ...]:
    opportunities: list[FactoryOpportunity] = [
        FactoryOpportunity(
            opportunity_id="evaluation.search_parent.0",
            opportunity_kind="search_evaluation",
            sequence_no=0,
            provider_config_digest=build_input.epoch.deployment.provider_config_digest,
            task_manifest_id=None,
        )
    ]
    for control in build_input.s1_config.controls:
        for index in range(build_input.s1_config.control_opportunities_per_arm):
            opportunities.append(
                FactoryOpportunity(
                    opportunity_id=f"evaluation.control.{control.control_id}.{index}",
                    opportunity_kind="control_evaluation",
                    sequence_no=len(opportunities),
                    provider_config_digest=build_input.epoch.deployment.provider_config_digest,
                    task_manifest_id=None,
                )
            )
    for proposal in _proposal_opportunities(build_input):
        opportunities.append(
            FactoryOpportunity(
                opportunity_id=f"evaluation.search_child.{proposal.sequence_no}",
                opportunity_kind="search_evaluation",
                sequence_no=len(opportunities),
                provider_config_digest=proposal.provider_config_digest,
                task_manifest_id=proposal.task_manifest_id,
            )
        )
    return tuple(opportunities)


def _gate0_provider_opportunities(
    build_input: HarnessFactoryBuildInput,
) -> tuple[FactoryOpportunity, ...]:
    call_lookup = {
        call.call_id: call
        for arm in build_input.gate0_manifest.arms
        for call in arm.calls
    }
    opportunities: list[FactoryOpportunity] = []
    for sequence_no, call_id in enumerate(build_input.gate0_manifest.provider_call_schedule):
        call = call_lookup[call_id]
        opportunities.append(
            FactoryOpportunity(
                opportunity_id=f"gate0.{sequence_no}",
                opportunity_kind="gate0_provider_call",
                sequence_no=sequence_no,
                provider_config_digest=call.pair_key.provider_config_digest,
                pair_key_digest=call.pair_key_digest,
            )
        )
    return tuple(opportunities)


def _build_id(
    mode: str,
    build_input: HarnessFactoryBuildInput,
    search_result: PairedHarnessSearchResult,
) -> str:
    return (
        "factory-build."
        + evidence_digest(
            {
                "mode": mode,
                "prompt": build_input.factory_prompt,
                "epoch": build_input.epoch.epoch_manifest_digest,
                "search": search_result.result_digest,
                "parent": build_input.expected_parent_message_id,
            }
        )[:24]
    )


def _s1_config_digest(config: PairedHarnessSearchConfig) -> str:
    return evidence_digest(
        {
            "kind": "paired-harness-search-config-v1",
            "config": config.model_dump(mode="json"),
        }
    )


def _d0_manifest_set_digest(manifests: Sequence[DevelopmentTaskFeasibilityManifest]) -> str:
    return evidence_digest(
        {
            "kind": "development-task-feasibility-manifest-set-v1",
            "manifest_digests": sorted(manifest.manifest_digest for manifest in manifests),
        }
    )


def _write_controlled_evidence(
    *,
    project_root: str | Path,
    build_id: str,
    filename: str,
    payload: dict[str, Any],
) -> Path:
    root = Path(project_root).resolve()
    destination = root.joinpath(*HARNESS_FACTORY_BUILD_EVIDENCE_DIR, build_id, filename)
    destination.parent.mkdir(parents=True, exist_ok=True)
    resolved = destination.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise HarnessFactoryServiceValidationError(
            "controlled factory build evidence path escapes the project"
        ) from exc
    if f"/releases/" in resolved.as_posix() or "\\releases\\" in str(resolved):
        raise HarnessFactoryServiceValidationError(
            "controlled factory build evidence may not be written under releases"
        )
    payload = dict(payload)
    payload.setdefault(
        "evidence_digest",
        evidence_digest({"kind": "controlled-factory-build-evidence-v1", "payload": payload}),
    )
    tmp = resolved.with_name(f".{resolved.name}.{os.getpid()}.tmp")
    tmp.write_bytes(_canonical_bytes(payload))
    os.replace(tmp, resolved)
    return resolved


__all__ = [
    "HarnessFactoryBuildEvidence",
    "HarnessFactoryBuildInput",
    "HarnessFactoryBuildResult",
    "HarnessFactoryDryRunManifest",
    "HarnessFactoryExecutionModeError",
    "HarnessFactoryServiceError",
    "HarnessFactoryServiceValidationError",
    "build_harness_factory_release",
]
