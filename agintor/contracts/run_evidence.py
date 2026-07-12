from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..core.identity import canonical_identity_digest, evidence_digest
from ..core.versioning import RUNTIME_CONTRACT_VERSION
from .epochs import CapabilityEpoch, DataState, REPO_REPAIR_CAPABILITY_EPOCH
from .outcomes import OutcomeCost, OutcomeReceipt, PairKey


RUN_EVIDENCE_SCHEMA_VERSION = "repo-repair-run-evidence-v1"
RUN_PROOF_RECORD_SCHEMA_VERSION = "repo-repair-run-proof-record-v1"

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_RESPONSE_ID_RE = re.compile(r"^[^\s]{1,256}$")
_SECRET_VALUE_PATTERNS = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
_SECRET_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "credential",
    "credentials",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "token",
}


class RunEvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)


def _require_nonempty(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} may not be empty")
    return normalized


def _require_digest(value: str, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _DIGEST_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return normalized


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def _normalize_json_value(value: Any, *, path: str = "value") -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite float")
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str) or not raw_key:
                raise ValueError(f"{path} mapping keys must be nonempty strings")
            key = _normalize_key(raw_key)
            if key in _SECRET_KEYS and item not in (None, "", [], {}):
                raise ValueError(f"{path} contains resolved credential material")
            normalized[raw_key] = _normalize_json_value(item, path=f"{path}.{raw_key}")
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _normalize_json_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise ValueError(f"{path} must contain only canonical JSON values")


def assert_no_resolved_credentials(value: Any) -> None:
    normalized = _normalize_json_value(value)

    def scan(item: Any) -> None:
        if isinstance(item, str):
            if any(pattern.search(item) for pattern in _SECRET_VALUE_PATTERNS):
                raise ValueError("run evidence contains resolved credential material")
            return
        if isinstance(item, Mapping):
            for child in item.values():
                scan(child)
            return
        if isinstance(item, list):
            for child in item:
                scan(child)

    scan(normalized)


def _json_size(value: Any) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


class ObservedValue(RunEvidenceModel):
    value: Any
    value_digest: str = ""
    serialized_bytes: int = Field(default=0, ge=0)

    @field_validator("value", mode="before")
    @classmethod
    def validate_value(cls, value: Any) -> Any:
        normalized = _normalize_json_value(value)
        assert_no_resolved_credentials(normalized)
        return normalized

    @model_validator(mode="after")
    def bind_value_identity(self) -> "ObservedValue":
        computed_digest = evidence_digest(
            {"kind": "observed-value", "value": self.value}
        )
        computed_size = _json_size(self.value)
        if self.value_digest and self.value_digest != computed_digest:
            raise ValueError("ObservedValue.value_digest does not match its value")
        if self.serialized_bytes and self.serialized_bytes != computed_size:
            raise ValueError("ObservedValue.serialized_bytes does not match its value")
        if not self.value_digest:
            object.__setattr__(self, "value_digest", computed_digest)
        if not self.serialized_bytes:
            object.__setattr__(self, "serialized_bytes", computed_size)
        return self


class ContextEntry(RunEvidenceModel):
    entry_id: str
    source_kind: Literal[
        "instruction",
        "task",
        "workspace",
        "artifact",
        "prior_actor_output",
        "session",
    ]
    source_ref: str
    observed: ObservedValue

    @field_validator("entry_id", "source_ref")
    @classmethod
    def validate_nonempty(cls, value: str, info: Any) -> str:
        return _require_nonempty(value, info.field_name)


class PreCallContextEvidence(RunEvidenceModel):
    context_id: str
    context_digest: str = ""
    sequence_no: int = Field(ge=0)
    call_id: str
    actor_id: str
    entries: tuple[ContextEntry, ...] = Field(min_length=1)

    @field_validator("context_id", "call_id", "actor_id")
    @classmethod
    def validate_nonempty(cls, value: str, info: Any) -> str:
        return _require_nonempty(value, info.field_name)

    @model_validator(mode="after")
    def validate_context(self) -> "PreCallContextEvidence":
        entry_ids = [entry.entry_id for entry in self.entries]
        if len(entry_ids) != len(set(entry_ids)):
            raise ValueError("pre-call context entry_id values must be unique")
        payload = self.model_dump(mode="python", exclude={"context_digest"})
        computed = evidence_digest({"kind": "pre-call-context", **payload})
        if self.context_digest and self.context_digest != computed:
            raise ValueError("context_digest does not match the exact pre-call context")
        if not self.context_digest:
            object.__setattr__(self, "context_digest", computed)
        return self


class ArtifactEvidence(RunEvidenceModel):
    artifact_id: str
    channel_id: str
    producer_call_id: str
    artifact_schema: Literal["text", "structured"]
    observed: ObservedValue
    payload_bytes: int = Field(ge=0)
    intended_consumer_call_ids: tuple[str, ...]
    actual_consumer_call_ids: tuple[str, ...]
    immutable: Literal[True] = True

    @field_validator("artifact_id", "channel_id", "producer_call_id")
    @classmethod
    def validate_nonempty(cls, value: str, info: Any) -> str:
        return _require_nonempty(value, info.field_name)

    @model_validator(mode="after")
    def validate_consumers(self) -> "ArtifactEvidence":
        for label, values in (
            ("intended", self.intended_consumer_call_ids),
            ("actual", self.actual_consumer_call_ids),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"artifact {label} consumers may not contain duplicates")
            if tuple(sorted(values)) != values:
                raise ValueError(f"artifact {label} consumers must be sorted")
        if not set(self.actual_consumer_call_ids).issubset(
            self.intended_consumer_call_ids
        ):
            raise ValueError("actual artifact consumers must be declared consumers")
        if self.artifact_schema == "text" and not isinstance(self.observed.value, str):
            raise ValueError("text artifacts require a string ObservedValue")
        expected_bytes = len(str(self.observed.value).encode("utf-8")) if self.artifact_schema == "text" else self.observed.serialized_bytes
        if self.payload_bytes != expected_bytes:
            raise ValueError("artifact payload_bytes does not match its exact value")
        return self


class ArtifactDeliveryEvidence(RunEvidenceModel):
    delivery_id: str
    sequence_no: int = Field(ge=0)
    artifact_id: str
    channel_id: str
    producer_call_id: str
    consumer_call_id: str
    delivery_kind: Literal["intact", "neutral_replacement"] = "intact"
    intervention_digest: str | None = None
    observed: ObservedValue
    payload_bytes: int = Field(ge=0)

    @field_validator(
        "delivery_id",
        "artifact_id",
        "channel_id",
        "producer_call_id",
        "consumer_call_id",
    )
    @classmethod
    def validate_nonempty(cls, value: str, info: Any) -> str:
        return _require_nonempty(value, info.field_name)

    @field_validator("intervention_digest")
    @classmethod
    def validate_optional_digest(cls, value: str | None) -> str | None:
        return None if value is None else _require_digest(value, "intervention_digest")

    @model_validator(mode="after")
    def validate_delivery_kind(self) -> "ArtifactDeliveryEvidence":
        if (self.delivery_kind == "neutral_replacement") != bool(
            self.intervention_digest
        ):
            raise ValueError(
                "neutral replacement delivery must bind exactly one intervention digest"
            )
        expected_bytes = len(str(self.observed.value).encode("utf-8"))
        if self.payload_bytes != expected_bytes:
            raise ValueError("delivery payload_bytes does not match its exact value")
        return self


class ArtifactReadEvidence(RunEvidenceModel):
    read_id: str
    sequence_no: int = Field(ge=0)
    artifact_id: str
    channel_id: str
    consumer_call_id: str
    context_id: str
    context_entry_id: str
    read_kind: Literal["direct_delivery", "retained_read"] = "direct_delivery"
    delivery_id: str | None = None
    observed: ObservedValue
    payload_bytes: int = Field(ge=0)

    @field_validator(
        "read_id",
        "artifact_id",
        "channel_id",
        "consumer_call_id",
        "context_id",
        "context_entry_id",
    )
    @classmethod
    def validate_nonempty(cls, value: str, info: Any) -> str:
        return _require_nonempty(value, info.field_name)

    @model_validator(mode="after")
    def validate_read_kind(self) -> "ArtifactReadEvidence":
        if self.read_kind == "direct_delivery" and not self.delivery_id:
            raise ValueError("direct artifact reads require delivery_id")
        if self.read_kind == "retained_read" and self.delivery_id is not None:
            raise ValueError("retained artifact reads may not claim a new delivery")
        expected_bytes = len(str(self.observed.value).encode("utf-8"))
        if self.payload_bytes != expected_bytes:
            raise ValueError("artifact read payload_bytes does not match its exact value")
        return self


class RouteEvidence(RunEvidenceModel):
    route_id: str
    sequence_no: int = Field(ge=0)
    route_kind: Literal["sequential", "fork", "join", "revision"]
    from_call_id: str | None = None
    to_call_id: str
    stage_id: str
    trigger: Literal[
        "plan_start",
        "producer_completed",
        "join_completed",
        "public_verification_failed",
    ]
    taken: Literal[True] = True

    @field_validator("route_id", "to_call_id", "stage_id")
    @classmethod
    def validate_nonempty(cls, value: str, info: Any) -> str:
        return _require_nonempty(value, info.field_name)


class ProviderCallEvidence(RunEvidenceModel):
    provider_call_id: str
    sequence_no: int = Field(ge=0)
    call_id: str
    actor_id: str
    turn_index: int = Field(ge=0)
    attempt_index: int = Field(ge=0)
    context_id: str
    context_digest: str
    deployment_id: str
    provider: str
    model: str
    provider_config_digest: str
    request_digest: str
    status: Literal["succeeded", "failed_pre_send", "failed_post_send"]
    request_sent: bool
    response_id: str | None = None
    response_digest: str | None = None
    response_kind: Literal["tool_request", "terminal"] | None = None
    tool_request_id: str | None = None
    started_at_ms: int = Field(ge=0)
    finished_at_ms: int = Field(ge=0)

    @field_validator(
        "provider_call_id",
        "call_id",
        "actor_id",
        "context_id",
        "deployment_id",
        "provider",
        "model",
    )
    @classmethod
    def validate_nonempty(cls, value: str, info: Any) -> str:
        return _require_nonempty(value, info.field_name)

    @field_validator("context_digest", "provider_config_digest", "request_digest")
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @field_validator("response_digest")
    @classmethod
    def validate_optional_digest(cls, value: str | None) -> str | None:
        return None if value is None else _require_digest(value, "response_digest")

    @field_validator("response_id")
    @classmethod
    def validate_response_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        if not _RESPONSE_ID_RE.fullmatch(normalized):
            raise ValueError("provider response_id must be a non-secret opaque identifier")
        assert_no_resolved_credentials(normalized)
        return normalized

    @model_validator(mode="after")
    def validate_call_result(self) -> "ProviderCallEvidence":
        if self.finished_at_ms < self.started_at_ms:
            raise ValueError("provider call finish precedes its start")
        if self.status == "failed_pre_send" and self.request_sent:
            raise ValueError("failed_pre_send calls may not report request_sent")
        if self.status != "failed_pre_send" and not self.request_sent:
            raise ValueError("sent/successful provider statuses require request_sent")
        if self.status == "succeeded" and not self.response_id:
            raise ValueError("successful provider calls require response_id")
        if self.status == "succeeded" and (
            self.response_digest is None or self.response_kind is None
        ):
            raise ValueError("successful provider calls require response identity and kind")
        if self.status != "succeeded" and (
            self.response_digest is not None
            or self.response_kind is not None
            or self.tool_request_id is not None
        ):
            raise ValueError("failed provider calls may not claim response content")
        if (self.response_kind == "tool_request") != bool(self.tool_request_id):
            raise ValueError("tool-request provider calls require exactly one tool_request_id")
        if self.response_id and not self.request_sent:
            raise ValueError("pre-send failures cannot have provider response IDs")
        return self


class RetryEvidence(RunEvidenceModel):
    retry_id: str
    sequence_no: int = Field(ge=0)
    call_id: str
    failed_provider_call_id: str
    next_attempt_index: int = Field(gt=0)
    reason_code: Literal[
        "rate_limit",
        "timeout",
        "provider_error",
        "transport_error",
        "invalid_response",
    ]
    prior_request_sent: bool
    prior_response_id: str | None = None

    @field_validator("retry_id", "call_id", "failed_provider_call_id")
    @classmethod
    def validate_nonempty(cls, value: str, info: Any) -> str:
        return _require_nonempty(value, info.field_name)


class ToolReceiptEvidence(RunEvidenceModel):
    tool_call_id: str
    sequence_no: int = Field(ge=0)
    call_id: str
    tool_id: str
    phase: Literal["actor_tool", "terminal_public_verification"]
    tool_request_id: str | None = None
    verification_step_id: str | None = None
    invocation_digest: str
    receipt_id: str
    receipt_digest: str
    status: Literal["succeeded", "failed", "timed_out", "blocked"]
    output_digest: str
    output_bytes: int = Field(ge=0)
    retry_index: int = Field(ge=0)
    started_at_ms: int = Field(ge=0)
    finished_at_ms: int = Field(ge=0)

    @field_validator("tool_call_id", "call_id", "tool_id", "receipt_id")
    @classmethod
    def validate_nonempty(cls, value: str, info: Any) -> str:
        return _require_nonempty(value, info.field_name)

    @field_validator(
        "invocation_digest",
        "receipt_digest",
        "output_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @model_validator(mode="after")
    def validate_timing(self) -> "ToolReceiptEvidence":
        if self.finished_at_ms < self.started_at_ms:
            raise ValueError("tool receipt finish precedes its start")
        if self.phase == "actor_tool":
            if not self.tool_request_id or self.verification_step_id is not None:
                raise ValueError("actor-tool evidence requires only a tool_request_id")
        elif (
            self.tool_id != "repo.public_test"
            or self.tool_request_id is not None
            or not self.verification_step_id
        ):
            raise ValueError(
                "terminal verification evidence requires only a verification_step_id"
            )
        return self


class CostLedgerEvidence(RunEvidenceModel):
    ledger_digest: str = ""
    cost: OutcomeCost
    provider_deadline_ms: int = Field(gt=0)
    deadline_exceeded: bool
    active_reservations: int = Field(ge=0)
    reconciled: bool

    @model_validator(mode="after")
    def bind_ledger_digest(self) -> "CostLedgerEvidence":
        payload = self.model_dump(mode="python", exclude={"ledger_digest"})
        computed = evidence_digest({"kind": "aggregate-cost-ledger", **payload})
        if self.ledger_digest and self.ledger_digest != computed:
            raise ValueError("ledger_digest does not match aggregate cost evidence")
        if not self.ledger_digest:
            object.__setattr__(self, "ledger_digest", computed)
        if self.reconciled and self.active_reservations:
            raise ValueError("a reconciled cost ledger cannot have active reservations")
        return self


def runtime_environment_evidence_digest(value: Mapping[str, Any]) -> str:
    payload = dict(value)
    payload.pop("runtime_environment_digest", None)
    return evidence_digest(
        {"kind": "runtime-environment-evidence-v1", "environment": payload}
    )


class EnvironmentEvidence(RunEvidenceModel):
    environment_id: str
    runtime_environment_digest: str
    command_container_policy_digest: str
    python_identity: str
    platform_identity: str
    workspace_snapshot_digest: str
    container_image_digest: str | None = None
    network_policy: Literal["none"] = "none"
    filesystem_policy: Literal["scratch-workspace-only"] = "scratch-workspace-only"

    @field_validator("environment_id", "python_identity", "platform_identity")
    @classmethod
    def validate_nonempty(cls, value: str, info: Any) -> str:
        return _require_nonempty(value, info.field_name)

    @field_validator(
        "runtime_environment_digest",
        "command_container_policy_digest",
        "workspace_snapshot_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @field_validator("container_image_digest")
    @classmethod
    def validate_optional_digest(cls, value: str | None) -> str | None:
        return None if value is None else _require_digest(value, "container_image_digest")

    @model_validator(mode="after")
    def validate_runtime_environment_identity(self) -> "EnvironmentEvidence":
        payload = self.model_dump(
            mode="python",
            exclude={"runtime_environment_digest"},
        )
        computed = runtime_environment_evidence_digest(payload)
        if self.runtime_environment_digest != computed:
            raise ValueError("runtime_environment_digest does not match runtime evidence")
        return self


class PatchEvidence(RunEvidenceModel):
    status: Literal["emitted", "not_emitted"]
    observed: ObservedValue | None = None
    patch_digest: str | None = None
    patch_bytes: int = Field(ge=0)
    artifact_id: str | None = None
    public_verification_passed: bool | None = None

    @field_validator("patch_digest")
    @classmethod
    def validate_optional_digest(cls, value: str | None) -> str | None:
        return None if value is None else _require_digest(value, "patch_digest")

    @model_validator(mode="after")
    def validate_patch(self) -> "PatchEvidence":
        if self.status == "emitted":
            if (
                not self.patch_digest
                or self.observed is None
                or not isinstance(self.observed.value, str)
                or self.patch_bytes <= 0
            ):
                raise ValueError("emitted patches require exact text, digest, and bytes")
            if self.patch_bytes != len(self.observed.value.encode("utf-8")):
                raise ValueError("patch_bytes does not match exact patch text")
            expected_digest = canonical_identity_digest(
                self.observed.value,
                domain="final-unified-diff",
            )
            if self.patch_digest != expected_digest:
                raise ValueError("patch_digest does not match exact patch text")
        elif self.observed is not None or self.patch_digest or self.artifact_id or self.patch_bytes:
            raise ValueError("not_emitted patch evidence cannot contain patch material")
        return self


class TerminationEvidence(RunEvidenceModel):
    reason: Literal[
        "success",
        "public_verification_failed",
        "budget_exhausted",
        "hard_failure",
    ]
    final_call_id: str | None = None
    final_patch_digest: str | None = None
    completed_at_ms: int = Field(ge=0)
    success: bool

    @field_validator("final_call_id")
    @classmethod
    def validate_final_call_id(cls, value: str | None) -> str | None:
        return None if value is None else _require_nonempty(value, "final_call_id")

    @field_validator("final_patch_digest")
    @classmethod
    def validate_optional_digest(cls, value: str | None) -> str | None:
        return None if value is None else _require_digest(value, "final_patch_digest")

    @model_validator(mode="after")
    def validate_success(self) -> "TerminationEvidence":
        if self.success != (self.reason == "success"):
            raise ValueError("termination success flag must agree with its reason")
        if self.success and (not self.final_patch_digest or not self.final_call_id):
            raise ValueError("successful repair termination requires final call and patch identities")
        return self


class RunHealth(RunEvidenceModel):
    process_integrity: bool
    no_leakage: bool
    context_integrity: bool
    artifact_integrity: bool
    tool_integrity: bool
    accounting_complete: bool
    environment_integrity: bool

    @property
    def healthy(self) -> bool:
        return all(
            (
                self.process_integrity,
                self.no_leakage,
                self.context_integrity,
                self.artifact_integrity,
                self.tool_integrity,
                self.accounting_complete,
                self.environment_integrity,
            )
        )

class RunEvidence(RunEvidenceModel):
    schema_version: Literal[RUN_EVIDENCE_SCHEMA_VERSION] = RUN_EVIDENCE_SCHEMA_VERSION
    runtime_contract_version: str = RUNTIME_CONTRACT_VERSION
    evidence_id: str
    evidence_digest: str = ""
    run_id: str
    execution_mode: Literal["deterministic_replay", "live_provider"]
    live_inference_status: Literal["not_run", "completed", "failed"]
    real_inference_requests_sent: int = Field(ge=0)
    arm: Literal["intact", "neutral_artifact"] = "intact"
    intervention_digest: str | None = None
    capability_epoch: CapabilityEpoch = REPO_REPAIR_CAPABILITY_EPOCH
    data_state: DataState
    epoch_id: str
    epoch_manifest_digest: str
    release_digest: str
    release_manifest_digest: str
    profile_digest: str
    split_manifest_digest: str
    pair_key: PairKey
    task_manifest_digest: str
    protocol_digest: str
    compiled_semantic_digest: str
    dependency_manifest_digest: str
    compiler_digest: str
    kernel_digest: str
    tool_manifest_digest: str
    provider_config_digest: str
    decoding_policy_digest: str
    price_schedule_digest: str
    command_container_policy_digest: str
    deployment_id: str
    provider: str
    model: str
    contexts: tuple[PreCallContextEvidence, ...]
    artifacts: tuple[ArtifactEvidence, ...]
    deliveries: tuple[ArtifactDeliveryEvidence, ...]
    reads: tuple[ArtifactReadEvidence, ...]
    routes: tuple[RouteEvidence, ...]
    provider_calls: tuple[ProviderCallEvidence, ...]
    tool_receipts: tuple[ToolReceiptEvidence, ...]
    retries: tuple[RetryEvidence, ...]
    cost_ledger: CostLedgerEvidence
    environment: EnvironmentEvidence
    patch: PatchEvidence
    termination: TerminationEvidence
    health: RunHealth

    @field_validator("runtime_contract_version")
    @classmethod
    def validate_contract_version(cls, value: str) -> str:
        if str(value) != RUNTIME_CONTRACT_VERSION:
            raise ValueError("RunEvidence runtime contract version mismatch")
        return str(value)

    @field_validator("evidence_id", "run_id", "epoch_id", "deployment_id", "provider", "model")
    @classmethod
    def validate_nonempty(cls, value: str, info: Any) -> str:
        return _require_nonempty(value, info.field_name)

    @field_validator(
        "epoch_manifest_digest",
        "release_digest",
        "release_manifest_digest",
        "profile_digest",
        "split_manifest_digest",
        "task_manifest_digest",
        "protocol_digest",
        "compiled_semantic_digest",
        "dependency_manifest_digest",
        "compiler_digest",
        "kernel_digest",
        "tool_manifest_digest",
        "provider_config_digest",
        "decoding_policy_digest",
        "price_schedule_digest",
        "command_container_policy_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _require_digest(value, info.field_name)

    @field_validator("intervention_digest")
    @classmethod
    def validate_optional_digest(cls, value: str | None) -> str | None:
        return None if value is None else _require_digest(value, "intervention_digest")

    @model_validator(mode="after")
    def validate_run_evidence(self) -> "RunEvidence":
        if self.execution_mode == "deterministic_replay" and (
            self.live_inference_status != "not_run"
            or self.real_inference_requests_sent != 0
        ):
            raise ValueError("deterministic replay cannot claim live inference")
        if self.execution_mode == "live_provider" and (
            self.live_inference_status == "not_run"
        ) != (self.real_inference_requests_sent == 0):
            raise ValueError("live inference status disagrees with real request count")
        if self.pair_key.task_manifest_id == "":
            raise ValueError("RunEvidence requires a task PairKey")
        if self.pair_key.provider_config_digest != self.provider_config_digest:
            raise ValueError("RunEvidence provider configuration does not match PairKey")
        self._validate_provider_configurations()
        if self.environment.environment_id != self.pair_key.environment_id:
            raise ValueError("RunEvidence environment_id does not match PairKey")
        if (
            self.environment.command_container_policy_digest
            != self.command_container_policy_digest
        ):
            raise ValueError("RunEvidence runtime environment crossed command policy")
        if (self.arm == "neutral_artifact") != bool(self.intervention_digest):
            raise ValueError("neutral-artifact arm must bind exactly one intervention digest")
        self._validate_unique_and_ordered()
        self._validate_context_calls()
        self._validate_artifact_flow()
        self._validate_retry_links()
        self._validate_patch_termination()
        computed = run_evidence_digest(self)
        if self.evidence_digest and self.evidence_digest != computed:
            raise ValueError("RunEvidence.evidence_digest does not match its payload")
        if not self.evidence_digest:
            object.__setattr__(self, "evidence_digest", computed)
        return self

    def _validate_provider_configurations(self) -> None:
        configs = {call.provider_config_digest for call in self.provider_calls}
        if len(configs) > 1:
            raise ValueError("provider calls contain crossed provider configurations")
        if configs and next(iter(configs)) != self.provider_config_digest:
            raise ValueError("provider calls do not match RunEvidence provider configuration")

    def _validate_unique_and_ordered(self) -> None:
        collections = (
            ("contexts", self.contexts, "context_id", "sequence_no"),
            ("artifacts", self.artifacts, "artifact_id", None),
            ("deliveries", self.deliveries, "delivery_id", "sequence_no"),
            ("reads", self.reads, "read_id", "sequence_no"),
            ("routes", self.routes, "route_id", "sequence_no"),
            ("provider_calls", self.provider_calls, "provider_call_id", "sequence_no"),
            ("tool_receipts", self.tool_receipts, "tool_call_id", "sequence_no"),
            ("retries", self.retries, "retry_id", "sequence_no"),
        )
        for label, records, id_field, sequence_field in collections:
            identifiers = [getattr(record, id_field) for record in records]
            if len(identifiers) != len(set(identifiers)):
                raise ValueError(f"RunEvidence {label} contain duplicate identities")
            if sequence_field is not None:
                sequence = [getattr(record, sequence_field) for record in records]
                if sequence != sorted(sequence) or len(sequence) != len(set(sequence)):
                    raise ValueError(f"RunEvidence {label} must have unique chronological sequence_no values")

    def _validate_context_calls(self) -> None:
        contexts = {context.context_id: context for context in self.contexts}
        call_attempts: set[tuple[str, int]] = set()
        response_ids: set[str] = set()
        for call in self.provider_calls:
            context = contexts.get(call.context_id)
            if context is None or context.call_id != call.call_id:
                raise ValueError("provider call references a missing or crossed pre-call context")
            if context.context_digest != call.context_digest:
                raise ValueError("provider call context digest does not match exact context")
            if (call.deployment_id, call.provider, call.model) != (
                self.deployment_id,
                self.provider,
                self.model,
            ):
                raise ValueError("provider call crossed the pinned deployment/model")
            key = (call.call_id, call.turn_index, call.attempt_index)
            if key in call_attempts:
                raise ValueError("duplicate provider attempt index for logical call")
            call_attempts.add(key)
            if call.response_id:
                if call.response_id in response_ids:
                    raise ValueError("provider response IDs must be unique")
                response_ids.add(call.response_id)
        request_digests = [call.request_digest for call in self.provider_calls]
        if len(request_digests) != len(set(request_digests)):
            raise ValueError("provider request digests must be unique across rounds")
        tool_request_ids = [
            call.tool_request_id for call in self.provider_calls if call.tool_request_id
        ]
        if len(tool_request_ids) != len(set(tool_request_ids)):
            raise ValueError("provider tool_request_id values must be unique")
        by_call: dict[str, list[ProviderCallEvidence]] = {}
        for call in self.provider_calls:
            by_call.setdefault(call.call_id, []).append(call)
        for call_id, rounds in by_call.items():
            succeeded = [call for call in rounds if call.status == "succeeded"]
            if not succeeded:
                continue
            turn_indexes = sorted({call.turn_index for call in succeeded})
            if turn_indexes != list(range(len(turn_indexes))):
                raise ValueError(f"provider rounds for {call_id} must be contiguous from zero")
            terminals = [call for call in succeeded if call.response_kind == "terminal"]
            if terminals:
                if len(terminals) != 1 or terminals[0].turn_index != turn_indexes[-1]:
                    raise ValueError(
                        f"provider rounds for {call_id} require one final terminal response"
                    )
                nonfinal = [
                    call for call in succeeded if call.turn_index != turn_indexes[-1]
                ]
            else:
                if not any(call.status != "succeeded" for call in rounds):
                    raise ValueError(
                        f"provider rounds for {call_id} ended without terminal or failure"
                    )
                nonfinal = succeeded
            if any(call.response_kind != "tool_request" for call in nonfinal):
                raise ValueError(f"non-final provider rounds for {call_id} must request tools")
        provider_tool_requests = {
            call.tool_request_id
            for call in self.provider_calls
            if call.response_kind == "tool_request"
        }
        receipt_tool_requests = {
            receipt.tool_request_id
            for receipt in self.tool_receipts
            if receipt.phase == "actor_tool"
        }
        if provider_tool_requests != receipt_tool_requests:
            raise ValueError("actor tool receipts do not exactly match provider tool requests")

    def _validate_artifact_flow(self) -> None:
        artifacts = {artifact.artifact_id: artifact for artifact in self.artifacts}
        contexts = {context.context_id: context for context in self.contexts}
        contexts_by_call = {context.call_id: context for context in self.contexts}
        deliveries: dict[tuple[str, str], ArtifactDeliveryEvidence] = {}
        neutral_deliveries = 0
        for delivery in self.deliveries:
            artifact = artifacts.get(delivery.artifact_id)
            if artifact is None:
                raise ValueError("delivery references an unobserved artifact")
            if (
                artifact.channel_id != delivery.channel_id
                or artifact.producer_call_id != delivery.producer_call_id
            ):
                raise ValueError("delivery identity differs from its produced artifact")
            if delivery.delivery_kind == "intact":
                if artifact.observed != delivery.observed:
                    raise ValueError("intact delivery differs from immutable produced value")
            else:
                neutral_deliveries += 1
                if self.arm != "neutral_artifact":
                    raise ValueError("neutral replacement delivery requires neutral run arm")
                if delivery.intervention_digest != self.intervention_digest:
                    raise ValueError("delivery crossed the run intervention digest")
                if artifact.observed == delivery.observed:
                    raise ValueError("neutral replacement must change delivered content")
            key = (delivery.artifact_id, delivery.consumer_call_id)
            if key in deliveries:
                raise ValueError("artifact was delivered more than once to the same call")
            deliveries[key] = delivery
        if self.arm == "neutral_artifact" and neutral_deliveries != 1:
            raise ValueError("neutral-artifact run requires exactly one replacement delivery")
        if self.arm == "intact" and neutral_deliveries:
            raise ValueError("intact run cannot contain replacement deliveries")
        reads: dict[tuple[str, str], ArtifactReadEvidence] = {}
        direct_reads: dict[tuple[str, str], ArtifactReadEvidence] = {}
        for read in self.reads:
            artifact = artifacts.get(read.artifact_id)
            if artifact is None:
                raise ValueError("artifact read references an unobserved artifact")
            delivery = deliveries.get((read.artifact_id, read.consumer_call_id))
            if read.read_kind == "direct_delivery":
                if delivery is None or read.delivery_id != delivery.delivery_id:
                    raise ValueError("declared-but-undelivered artifact cannot appear consumed")
                if read.channel_id != delivery.channel_id or read.observed != delivery.observed or read.payload_bytes != delivery.payload_bytes:
                    raise ValueError("artifact read differs from exact delivered value")
                direct_reads[(read.artifact_id, read.consumer_call_id)] = read
            else:
                if delivery is not None:
                    raise ValueError("retained artifact read cannot duplicate a direct delivery")
                prior_deliveries = [item for (artifact_id, _), item in deliveries.items() if artifact_id == read.artifact_id]
                if not prior_deliveries:
                    raise ValueError("retained artifact was never delivered")
                context_for_read = contexts.get(read.context_id)
                if context_for_read is None:
                    raise ValueError("retained artifact read references a missing context")
                same_actor_delivery = any(
                    contexts_by_call.get(item.consumer_call_id) is not None
                    and contexts_by_call[item.consumer_call_id].actor_id
                    == context_for_read.actor_id
                    for item in prior_deliveries
                )
                if not same_actor_delivery:
                    raise ValueError("retained artifact was not previously delivered to this actor")
                if read.channel_id != artifact.channel_id or read.observed != artifact.observed or read.payload_bytes != artifact.payload_bytes:
                    raise ValueError("retained artifact read differs from immutable produced value")
            context = contexts.get(read.context_id)
            if context is None or context.call_id != read.consumer_call_id:
                raise ValueError("artifact read references a missing consumer context")
            entry = next(
                (item for item in context.entries if item.entry_id == read.context_entry_id),
                None,
            )
            if (
                entry is None
                or entry.source_kind != "artifact"
                or entry.source_ref != read.artifact_id
                or entry.observed != read.observed
            ):
                raise ValueError("artifact read is absent from the exact pre-call context")
            reads[(read.artifact_id, read.consumer_call_id)] = read
        if set(direct_reads) != set(deliveries):
            raise ValueError("every delivered artifact must have one observed consumer read")
        for artifact in self.artifacts:
            observed_consumers = tuple(
                sorted(
                    consumer
                    for artifact_id, consumer in reads
                    if artifact_id == artifact.artifact_id
                )
            )
            if observed_consumers != artifact.actual_consumer_call_ids:
                raise ValueError("artifact actual consumers disagree with delivery/read evidence")
        for context in self.contexts:
            artifact_entries = {
                (entry.source_ref, context.call_id)
                for entry in context.entries
                if entry.source_kind == "artifact"
            }
            missing_reads = artifact_entries - set(reads)
            if missing_reads:
                raise ValueError("pre-call context contains artifact without a delivery/read record")

    def _validate_retry_links(self) -> None:
        calls = {call.provider_call_id: call for call in self.provider_calls}
        attempts = {(call.call_id, call.attempt_index) for call in self.provider_calls}
        for retry in self.retries:
            failed = calls.get(retry.failed_provider_call_id)
            if failed is None or failed.call_id != retry.call_id or failed.status == "succeeded":
                raise ValueError("retry does not reference a failed provider call")
            if failed.request_sent != retry.prior_request_sent:
                raise ValueError("retry request-sent state disagrees with failed call")
            if failed.response_id != retry.prior_response_id:
                raise ValueError("retry response ID disagrees with failed call")
            if (retry.call_id, retry.next_attempt_index) not in attempts:
                raise ValueError("retry does not lead to a recorded next provider attempt")
        if len(self.retries) != self.cost_ledger.cost.retries:
            raise ValueError("retry evidence count does not reconcile with aggregate cost")

    def _validate_patch_termination(self) -> None:
        if self.patch.status == "emitted":
            artifact_ids = {artifact.artifact_id for artifact in self.artifacts}
            if self.patch.artifact_id is not None and self.patch.artifact_id not in artifact_ids:
                raise ValueError("patch evidence references an unobserved artifact")
            if self.termination.final_patch_digest != self.patch.patch_digest:
                raise ValueError("termination and patch evidence digests do not match")
        elif self.termination.final_patch_digest is not None:
            raise ValueError("termination cannot name a patch that was not emitted")
        if self.cost_ledger.cost.patch_bytes != self.patch.patch_bytes:
            raise ValueError("patch bytes do not reconcile with aggregate cost")
        if self.cost_ledger.cost.model_calls != sum(call.request_sent for call in self.provider_calls):
            raise ValueError("provider calls do not reconcile with aggregate cost")
        if self.cost_ledger.cost.tool_calls != len(self.tool_receipts):
            raise ValueError("tool receipts do not reconcile with aggregate cost")
        if self.health.accounting_complete != self.cost_ledger.reconciled:
            raise ValueError("run health accounting status disagrees with cost ledger")


def run_evidence_identity_payload(
    evidence: RunEvidence | Mapping[str, Any],
) -> dict[str, Any]:
    if isinstance(evidence, RunEvidence):
        payload = evidence.model_dump(mode="python", exclude_none=True)
    else:
        payload = dict(evidence)
    payload.pop("evidence_digest", None)
    return payload


def run_evidence_digest(evidence: RunEvidence | Mapping[str, Any]) -> str:
    return evidence_digest(
        {
            "kind": RUN_EVIDENCE_SCHEMA_VERSION,
            "evidence": run_evidence_identity_payload(evidence),
        }
    )


class ProofPathPolicy(RunEvidenceModel):
    checkpoint_publication: Literal[False] = False
    derived_state_indexing: Literal[False] = False
    trace_rematerialization: Literal[False] = False


class RunProofRecord(RunEvidenceModel):
    schema_version: Literal[RUN_PROOF_RECORD_SCHEMA_VERSION] = RUN_PROOF_RECORD_SCHEMA_VERSION
    runtime_contract_version: str = RUNTIME_CONTRACT_VERSION
    proof_record_id: str
    proof_record_digest: str = ""
    path_policy: ProofPathPolicy = Field(default_factory=ProofPathPolicy)
    run_evidence: RunEvidence
    outcome_receipt: OutcomeReceipt | None = None

    @field_validator("proof_record_id")
    @classmethod
    def validate_proof_record_id(cls, value: str) -> str:
        return _require_nonempty(value, "proof_record_id")

    @model_validator(mode="after")
    def validate_proof_record(self) -> "RunProofRecord":
        receipt = self.outcome_receipt
        evidence = self.run_evidence
        if receipt is not None:
            identity_fields = (
                "capability_epoch",
                "data_state",
                "epoch_id",
                "epoch_manifest_digest",
                "release_digest",
                "release_manifest_digest",
                "profile_digest",
                "split_manifest_digest",
                "task_manifest_digest",
                "protocol_digest",
                "compiler_digest",
                "kernel_digest",
                "tool_manifest_digest",
                "provider_config_digest",
                "decoding_policy_digest",
                "price_schedule_digest",
                "command_container_policy_digest",
                "execution_mode",
                "live_inference_status",
                "real_inference_requests_sent",
            )
            crossed = [
                field_name
                for field_name in identity_fields
                if getattr(receipt, field_name) != getattr(evidence, field_name)
            ]
            if crossed:
                raise ValueError("outcome receipt crossed run evidence: " + ", ".join(crossed))
            if receipt.pair_key != evidence.pair_key:
                raise ValueError("outcome receipt PairKey crossed run evidence")
            if receipt.patch_digest != evidence.patch.patch_digest:
                raise ValueError("outcome receipt patch digest crossed run evidence")
            if receipt.cost != evidence.cost_ledger.cost:
                raise ValueError("outcome receipt cost crossed run evidence")
            expected_process_integrity = all(
                (
                    evidence.health.process_integrity,
                    evidence.health.context_integrity,
                    evidence.health.artifact_integrity,
                    evidence.health.tool_integrity,
                )
            )
            if (
                receipt.health.process_integrity != expected_process_integrity
                or receipt.health.no_leakage != evidence.health.no_leakage
                or receipt.health.environment_integrity
                != evidence.health.environment_integrity
                or receipt.health.accounting_complete
                != evidence.health.accounting_complete
            ):
                raise ValueError("outcome receipt health crossed run evidence")
        payload = self.model_dump(mode="python", exclude={"proof_record_digest"})
        computed = evidence_digest({"kind": RUN_PROOF_RECORD_SCHEMA_VERSION, **payload})
        if self.proof_record_digest and self.proof_record_digest != computed:
            raise ValueError("proof_record_digest does not match immutable proof record")
        if not self.proof_record_digest:
            object.__setattr__(self, "proof_record_digest", computed)
        return self


def run_evidence_public_projection(evidence: RunEvidence) -> dict[str, Any]:
    """Public-safe digest projection; exact values remain in the proof store."""

    return {
        "schema_version": evidence.schema_version,
        "runtime_contract_version": evidence.runtime_contract_version,
        "evidence_id": evidence.evidence_id,
        "evidence_digest": evidence.evidence_digest,
        "run_id": evidence.run_id,
        "execution_mode": evidence.execution_mode,
        "live_inference_status": evidence.live_inference_status,
        "real_inference_requests_sent": evidence.real_inference_requests_sent,
        "arm": evidence.arm,
        "intervention_digest": evidence.intervention_digest,
        "capability_epoch": evidence.capability_epoch,
        "data_state": evidence.data_state,
        "epoch_id": evidence.epoch_id,
        "epoch_manifest_digest": evidence.epoch_manifest_digest,
        "release_digest": evidence.release_digest,
        "release_manifest_digest": evidence.release_manifest_digest,
        "profile_digest": evidence.profile_digest,
        "split_manifest_digest": evidence.split_manifest_digest,
        "pair_key": evidence.pair_key.model_dump(mode="json"),
        "task_manifest_digest": evidence.task_manifest_digest,
        "protocol_digest": evidence.protocol_digest,
        "compiled_semantic_digest": evidence.compiled_semantic_digest,
        "dependency_manifest_digest": evidence.dependency_manifest_digest,
        "compiler_digest": evidence.compiler_digest,
        "kernel_digest": evidence.kernel_digest,
        "tool_manifest_digest": evidence.tool_manifest_digest,
        "provider_config_digest": evidence.provider_config_digest,
        "decoding_policy_digest": evidence.decoding_policy_digest,
        "price_schedule_digest": evidence.price_schedule_digest,
        "command_container_policy_digest": evidence.command_container_policy_digest,
        "deployment_id": evidence.deployment_id,
        "provider": evidence.provider,
        "model": evidence.model,
        "context_digests": [context.context_digest for context in evidence.contexts],
        "artifact_digests": [artifact.observed.value_digest for artifact in evidence.artifacts],
        "delivery_count": len(evidence.deliveries),
        "read_count": len(evidence.reads),
        "route_ids": [route.route_id for route in evidence.routes],
        "provider_response_ids": [
            call.response_id for call in evidence.provider_calls if call.response_id
        ],
        "tool_receipt_digests": [receipt.receipt_digest for receipt in evidence.tool_receipts],
        "retry_count": len(evidence.retries),
        "cost_ledger_digest": evidence.cost_ledger.ledger_digest,
        "runtime_environment_digest": evidence.environment.runtime_environment_digest,
        "patch_digest": evidence.patch.patch_digest,
        "termination_reason": evidence.termination.reason,
        "healthy": evidence.health.healthy,
    }


__all__ = [
    "ArtifactDeliveryEvidence",
    "ArtifactEvidence",
    "ArtifactReadEvidence",
    "ContextEntry",
    "CostLedgerEvidence",
    "EnvironmentEvidence",
    "ObservedValue",
    "PatchEvidence",
    "PreCallContextEvidence",
    "ProofPathPolicy",
    "ProviderCallEvidence",
    "RUN_EVIDENCE_SCHEMA_VERSION",
    "RUN_PROOF_RECORD_SCHEMA_VERSION",
    "RetryEvidence",
    "RouteEvidence",
    "RunEvidence",
    "RunEvidenceModel",
    "RunHealth",
    "RunProofRecord",
    "TerminationEvidence",
    "ToolReceiptEvidence",
    "assert_no_resolved_credentials",
    "run_evidence_digest",
    "run_evidence_identity_payload",
    "run_evidence_public_projection",
    "runtime_environment_evidence_digest",
]
