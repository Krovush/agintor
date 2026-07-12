from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..contracts.outcomes import PairKey, pair_key_digest
from ..contracts.run_evidence import assert_no_resolved_credentials
from ..core.identity import evidence_digest
from ..core.versioning import RUNTIME_CONTRACT_VERSION
from ..runtime.harness_profile import (
    HarnessDeploymentProfile,
    harness_deployment_profile_digest,
)
from ..runtime.kernel.composite_provider import CredentialReference


GATE0_PANEL_SCHEMA_VERSION = "forced-exchange-gate0-panel-v1"
GATE0_MANIFEST_SCHEMA_VERSION = "forced-exchange-gate0-dry-run-manifest-v1"
GATE0_ANALYSIS_SCHEMA_VERSION = "forced-exchange-gate0-analysis-v1"
GATE0_CONFORMANCE_SCHEMA_VERSION = "forced-exchange-gate0-conformance-v1"
GATE0_PREREGISTRATION_SCHEMA_VERSION = "forced-exchange-gate0-preregistration-v1"
GATE0_LIVE_AUTHORIZATION_SCHEMA_VERSION = "forced-exchange-gate0-live-authorization-v1"

GATE0_PANEL_ID = "gate0-forced-exchange-panel-2026-07-locked"
GATE0_FROZEN_SEED = "gate0-forced-exchange-seed-2026-07-10"
GATE0_PANEL_ITEM_COUNT = 32
GATE0_REPLICATES_PER_ITEM = 4
GATE0_CLUSTER_T_CRITICAL_ONE_SIDED_95_DF31 = 1.695518789136255

Gate0TemplateId = Literal[
    "sum_mod",
    "xor_mask",
    "ordered_pair",
    "threshold_compare",
]
Gate0Arm = Literal[
    "intact_exchange",
    "matched_neutral_artifact",
    "private_a_only",
    "private_b_only",
    "full_information",
]
Gate0ActorId = Literal["producer_a", "responder_b"]
Gate0LiveStatus = Literal["not_run", "executed"]

GATE0_TEMPLATE_IDS: tuple[Gate0TemplateId, ...] = (
    "sum_mod",
    "xor_mask",
    "ordered_pair",
    "threshold_compare",
)
GATE0_ARMS: tuple[Gate0Arm, ...] = (
    "intact_exchange",
    "matched_neutral_artifact",
    "private_a_only",
    "private_b_only",
    "full_information",
)


class Gate0Error(ValueError):
    """Base class for forced-exchange gate validation failures."""


class Gate0AnalysisError(Gate0Error):
    """Raised when the result panel cannot be analyzed safely."""


class Gate0LiveExecutionBlocked(Gate0Error):
    """Raised when a caller tries to leave dry-run mode without authority."""


class Gate0Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)


def _nonempty(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} may not be empty")
    return normalized


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _priced_input_units(value: Any) -> int:
    return len(_canonical_json(value).encode("utf-8"))


def _expected_answer(template_id: str, private_a: Mapping[str, Any], private_b: Mapping[str, Any]) -> str:
    if template_id == "sum_mod":
        return f"S{(int(private_a['left']) + int(private_b['right'])) % 97:02d}"
    if template_id == "xor_mask":
        return f"X{int(private_a['mask']) ^ int(private_b['signal']):02x}"
    if template_id == "ordered_pair":
        return f"P{private_a['prefix']}-{private_b['suffix']}"
    if template_id == "threshold_compare":
        return "HIGH" if int(private_b["candidate"]) > int(private_a["baseline"]) else "LOW"
    raise ValueError(f"unknown Gate 0 template: {template_id}")


def _artifact_text(item_id: str, private_a_digest: str) -> str:
    return f"share:{item_id}:{private_a_digest[:24]}"


def _neutral_text(original: str) -> str:
    if not original:
        raise Gate0Error("cannot build a neutral replacement for an empty artifact")
    fill = "N" if set(original) != {"N"} else "Z"
    neutral = fill * len(original)
    if neutral == original:
        raise Gate0Error("neutral replacement must differ from the intact artifact")
    return neutral


def _stable_rank(*parts: str) -> int:
    return int(evidence_digest({"kind": "gate0-randomization", "parts": parts})[:16], 16)


class Gate0Thresholds(Gate0Model):
    max_hard_invalid_rate_per_arm: float = 0.02
    max_hard_invalid_rate_spread: float = 0.02
    min_full_information_success_rate: float = 0.80
    max_private_view_success_rate: float = 0.25
    min_intact_exchange_success_rate: float = 0.70
    min_intact_minus_null_effect: float = 0.30
    min_clustered_one_sided_95_lower_bound: float = 0.15


class Gate0PrivateView(Gate0Model):
    view_id: Literal["private_a", "private_b"]
    payload: dict[str, Any]
    payload_digest: str = ""

    @field_validator("payload")
    @classmethod
    def validate_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value:
            raise ValueError("private evidence view may not be empty")
        _canonical_json(value)
        return value

    @model_validator(mode="after")
    def bind_digest(self) -> "Gate0PrivateView":
        computed = evidence_digest(
            {
                "kind": "gate0-private-view",
                "view_id": self.view_id,
                "payload": self.payload,
            }
        )
        if self.payload_digest and self.payload_digest != computed:
            raise ValueError("private evidence view digest mismatch")
        if not self.payload_digest:
            object.__setattr__(self, "payload_digest", computed)
        return self


class Gate0PanelItem(Gate0Model):
    item_id: str
    template_id: Gate0TemplateId
    template_version: Literal["locked-v1"] = "locked-v1"
    private_a: Gate0PrivateView
    private_b: Gate0PrivateView
    expected_answer: str
    resolution_rule: str
    private_a_alone_sufficient: Literal[False] = False
    private_b_alone_sufficient: Literal[False] = False
    item_digest: str = ""

    @field_validator("item_id", "expected_answer", "resolution_rule")
    @classmethod
    def validate_nonempty(cls, value: str, info: Any) -> str:
        return _nonempty(value, info.field_name)

    @model_validator(mode="after")
    def validate_item(self) -> "Gate0PanelItem":
        if self.private_a.view_id != "private_a" or self.private_b.view_id != "private_b":
            raise ValueError("Gate 0 items require one private_a and one private_b view")
        expected = _expected_answer(self.template_id, self.private_a.payload, self.private_b.payload)
        if self.expected_answer != expected:
            raise ValueError("Gate 0 expected answer does not match its two private views")
        payload = self.model_dump(mode="python", exclude={"item_digest"})
        computed = evidence_digest({"kind": "gate0-panel-item", **payload})
        if self.item_digest and self.item_digest != computed:
            raise ValueError("Gate 0 item digest mismatch")
        if not self.item_digest:
            object.__setattr__(self, "item_digest", computed)
        return self


class Gate0Panel(Gate0Model):
    schema_version: Literal[GATE0_PANEL_SCHEMA_VERSION] = GATE0_PANEL_SCHEMA_VERSION
    runtime_contract_version: str = RUNTIME_CONTRACT_VERSION
    panel_id: str = GATE0_PANEL_ID
    frozen_seed: str = GATE0_FROZEN_SEED
    item_count: int = GATE0_PANEL_ITEM_COUNT
    replicates_per_item: int = GATE0_REPLICATES_PER_ITEM
    templates: tuple[Gate0TemplateId, ...] = GATE0_TEMPLATE_IDS
    items: tuple[Gate0PanelItem, ...]
    panel_digest: str = ""

    @field_validator("runtime_contract_version")
    @classmethod
    def validate_contract_version(cls, value: str) -> str:
        if str(value) != RUNTIME_CONTRACT_VERSION:
            raise ValueError("Gate 0 runtime contract version mismatch")
        return str(value)

    @field_validator("panel_id", "frozen_seed")
    @classmethod
    def validate_nonempty(cls, value: str, info: Any) -> str:
        return _nonempty(value, info.field_name)

    @model_validator(mode="after")
    def validate_panel(self) -> "Gate0Panel":
        if self.item_count != GATE0_PANEL_ITEM_COUNT:
            raise ValueError("Gate 0 panel item_count is locked at 32")
        if self.replicates_per_item != GATE0_REPLICATES_PER_ITEM:
            raise ValueError("Gate 0 replicates_per_item is locked at 4")
        if len(self.items) != GATE0_PANEL_ITEM_COUNT:
            raise ValueError("Gate 0 panel must contain exactly 32 items")
        item_ids = [item.item_id for item in self.items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("Gate 0 panel items must be independent unique identities")
        templates = tuple(sorted({item.template_id for item in self.items}))
        if templates != tuple(sorted(GATE0_TEMPLATE_IDS)):
            raise ValueError("Gate 0 panel must cover exactly the four locked templates")
        if tuple(self.templates) != GATE0_TEMPLATE_IDS:
            raise ValueError("Gate 0 template order is locked")
        payload = self.model_dump(mode="python", exclude={"panel_digest"})
        computed = evidence_digest({"kind": GATE0_PANEL_SCHEMA_VERSION, **payload})
        if self.panel_digest and self.panel_digest != computed:
            raise ValueError("Gate 0 panel digest mismatch")
        if not self.panel_digest:
            object.__setattr__(self, "panel_digest", computed)
        return self


class Gate0ProviderIdentity(Gate0Model):
    deployment_id: str
    provider: str
    model: str
    pricing_profile: Literal["matched-input-units-v1"] = "matched-input-units-v1"
    profile_digest: str
    provider_config_digest: str
    decoding_policy_digest: str
    price_schedule_digest: str
    command_container_policy_digest: str

    @field_validator("deployment_id", "provider", "model", "pricing_profile")
    @classmethod
    def validate_nonempty(cls, value: str, info: Any) -> str:
        return _nonempty(value, info.field_name)

    @field_validator(
        "profile_digest",
        "provider_config_digest",
        "decoding_policy_digest",
        "price_schedule_digest",
        "command_container_policy_digest",
    )
    @classmethod
    def validate_deployment_digest(cls, value: str, info: Any) -> str:
        normalized = str(value or "").strip().lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError(f"{info.field_name} must be a lowercase SHA-256 digest")
        return normalized


class Gate0ProviderCallPlan(Gate0Model):
    call_id: str
    sample_id: str
    item_id: str
    template_id: Gate0TemplateId
    replicate_index: int = Field(ge=0, lt=GATE0_REPLICATES_PER_ITEM)
    arm: Gate0Arm
    actor_id: Gate0ActorId
    sequence_in_sample: int = Field(ge=0, le=1)
    pair_key: PairKey
    pair_key_digest: str
    request_payload: dict[str, Any]
    request_digest: str = ""
    context_digest: str = ""
    input_character_count: int = Field(default=0, ge=0)
    priced_input_units: int = Field(default=0, ge=0)
    request_sent: Literal[False] = False

    @field_validator("call_id", "sample_id", "item_id")
    @classmethod
    def validate_nonempty(cls, value: str, info: Any) -> str:
        return _nonempty(value, info.field_name)

    @model_validator(mode="after")
    def validate_call(self) -> "Gate0ProviderCallPlan":
        computed_pair = pair_key_digest(self.pair_key)
        if self.pair_key_digest != computed_pair:
            raise ValueError("Gate 0 provider call PairKey digest mismatch")
        canonical_payload = json.loads(_canonical_json(self.request_payload))
        if canonical_payload != self.request_payload:
            object.__setattr__(self, "request_payload", canonical_payload)
        input_units = _priced_input_units(canonical_payload)
        if self.input_character_count and self.input_character_count != input_units:
            raise ValueError("Gate 0 input_character_count mismatch")
        if self.priced_input_units and self.priced_input_units != input_units:
            raise ValueError("Gate 0 priced_input_units mismatch")
        request_digest = evidence_digest(
            {
                "kind": "gate0-provider-call-request",
                "request_payload": canonical_payload,
            }
        )
        context_digest = evidence_digest(
            {
                "kind": "gate0-provider-call-context",
                "call_id": self.call_id,
                "request_digest": request_digest,
            }
        )
        if self.request_digest and self.request_digest != request_digest:
            raise ValueError("Gate 0 request digest mismatch")
        if self.context_digest and self.context_digest != context_digest:
            raise ValueError("Gate 0 context digest mismatch")
        if not self.request_digest:
            object.__setattr__(self, "request_digest", request_digest)
        if not self.context_digest:
            object.__setattr__(self, "context_digest", context_digest)
        if not self.input_character_count:
            object.__setattr__(self, "input_character_count", input_units)
        if not self.priced_input_units:
            object.__setattr__(self, "priced_input_units", input_units)
        return self


class Gate0ArmPlan(Gate0Model):
    sample_id: str
    sample_digest: str = ""
    item_id: str
    template_id: Gate0TemplateId
    replicate_index: int = Field(ge=0, lt=GATE0_REPLICATES_PER_ITEM)
    arm: Gate0Arm
    pair_key: PairKey
    pair_key_digest: str
    randomization_rank: int = Field(ge=0)
    calls: tuple[Gate0ProviderCallPlan, ...] = Field(min_length=2, max_length=2)
    provider_call_count: Literal[2] = 2
    total_input_character_count: int = Field(default=0, ge=0)
    total_priced_input_units: int = Field(default=0, ge=0)
    schema_matched_to_intact: bool | None = None
    serialized_length_matched_to_intact: bool | None = None
    call_count_matched_to_intact: bool | None = None
    priced_input_matched_to_intact: bool | None = None

    @field_validator("sample_id", "item_id")
    @classmethod
    def validate_nonempty(cls, value: str, info: Any) -> str:
        return _nonempty(value, info.field_name)

    @model_validator(mode="after")
    def validate_sample(self) -> "Gate0ArmPlan":
        computed_pair = pair_key_digest(self.pair_key)
        if self.pair_key_digest != computed_pair:
            raise ValueError("Gate 0 arm PairKey digest mismatch")
        sequence = tuple(call.sequence_in_sample for call in self.calls)
        if sequence != (0, 1):
            raise ValueError("Gate 0 arm calls must be producer then responder")
        if tuple(call.actor_id for call in self.calls) != ("producer_a", "responder_b"):
            raise ValueError("Gate 0 arm must contain producer_a and responder_b calls")
        for call in self.calls:
            if (
                call.sample_id != self.sample_id
                or call.item_id != self.item_id
                or call.template_id != self.template_id
                or call.replicate_index != self.replicate_index
                or call.arm != self.arm
                or call.pair_key != self.pair_key
            ):
                raise ValueError("Gate 0 call identity crossed its arm plan")
        total_chars = sum(call.input_character_count for call in self.calls)
        total_units = sum(call.priced_input_units for call in self.calls)
        if self.total_input_character_count and self.total_input_character_count != total_chars:
            raise ValueError("Gate 0 arm input character budget mismatch")
        if self.total_priced_input_units and self.total_priced_input_units != total_units:
            raise ValueError("Gate 0 arm priced input budget mismatch")
        payload = self.model_dump(
            mode="python",
            exclude={
                "sample_digest",
                "total_input_character_count",
                "total_priced_input_units",
            },
        )
        payload["total_input_character_count"] = total_chars
        payload["total_priced_input_units"] = total_units
        computed_digest = evidence_digest({"kind": "gate0-arm-plan", **payload})
        if self.sample_digest and self.sample_digest != computed_digest:
            raise ValueError("Gate 0 arm digest mismatch")
        if not self.sample_digest:
            object.__setattr__(self, "sample_digest", computed_digest)
        if not self.total_input_character_count:
            object.__setattr__(self, "total_input_character_count", total_chars)
        if not self.total_priced_input_units:
            object.__setattr__(self, "total_priced_input_units", total_units)
        return self


class Gate0DryRunManifest(Gate0Model):
    schema_version: Literal[GATE0_MANIFEST_SCHEMA_VERSION] = GATE0_MANIFEST_SCHEMA_VERSION
    runtime_contract_version: str = RUNTIME_CONTRACT_VERSION
    manifest_id: str
    manifest_digest: str = ""
    panel: Gate0Panel
    provider_identity: Gate0ProviderIdentity
    evidence_destination: str
    thresholds: Gate0Thresholds = Field(default_factory=Gate0Thresholds)
    live_status: Literal["not_run"] = "not_run"
    arms: tuple[Gate0ArmPlan, ...]
    provider_call_schedule: tuple[str, ...]
    total_provider_calls: int = Field(default=0, ge=0)
    total_priced_input_units: int = Field(default=0, ge=0)

    @field_validator("runtime_contract_version")
    @classmethod
    def validate_contract_version(cls, value: str) -> str:
        if str(value) != RUNTIME_CONTRACT_VERSION:
            raise ValueError("Gate 0 manifest runtime contract version mismatch")
        return str(value)

    @field_validator("manifest_id", "evidence_destination")
    @classmethod
    def validate_nonempty(cls, value: str, info: Any) -> str:
        return _nonempty(value, info.field_name)

    @model_validator(mode="after")
    def validate_manifest(self) -> "Gate0DryRunManifest":
        expected_arm_count = (
            GATE0_PANEL_ITEM_COUNT * GATE0_REPLICATES_PER_ITEM * len(GATE0_ARMS)
        )
        if len(self.arms) != expected_arm_count:
            raise ValueError("Gate 0 manifest must plan every item/replicate/arm")
        item_ids = {item.item_id for item in self.panel.items}
        expected_keys = {
            (item_id, replicate_index, arm)
            for item_id in item_ids
            for replicate_index in range(GATE0_REPLICATES_PER_ITEM)
            for arm in GATE0_ARMS
        }
        actual_keys = {
            (arm.item_id, arm.replicate_index, arm.arm)
            for arm in self.arms
        }
        if actual_keys != expected_keys:
            raise ValueError("Gate 0 manifest arm coverage mismatch")
        if len(actual_keys) != len(self.arms):
            raise ValueError("Gate 0 manifest contains duplicate arm identities")
        call_ids = [call.call_id for arm in self.arms for call in arm.calls]
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("Gate 0 provider call identities must be unique")
        if tuple(sorted(call_ids)) != tuple(sorted(self.provider_call_schedule)):
            raise ValueError("Gate 0 provider call schedule must exactly cover planned calls")
        if len(self.provider_call_schedule) != len(call_ids):
            raise ValueError("Gate 0 provider call schedule contains duplicates")
        for arm in self.arms:
            if arm.pair_key.provider_config_digest != self.provider_identity.provider_config_digest:
                raise ValueError("Gate 0 arm PairKey crossed provider configuration")
            for call in arm.calls:
                if call.pair_key.provider_config_digest != self.provider_identity.provider_config_digest:
                    raise ValueError("Gate 0 call PairKey crossed provider configuration")
        total_calls = len(call_ids)
        total_units = sum(arm.total_priced_input_units for arm in self.arms)
        if self.total_provider_calls and self.total_provider_calls != total_calls:
            raise ValueError("Gate 0 total provider call count mismatch")
        if self.total_priced_input_units and self.total_priced_input_units != total_units:
            raise ValueError("Gate 0 total priced input mismatch")
        if not self.total_provider_calls:
            object.__setattr__(self, "total_provider_calls", total_calls)
        if not self.total_priced_input_units:
            object.__setattr__(self, "total_priced_input_units", total_units)
        computed = gate0_dry_run_manifest_digest(self)
        if self.manifest_digest and self.manifest_digest != computed:
            raise ValueError("Gate 0 dry-run manifest digest mismatch")
        if not self.manifest_digest:
            object.__setattr__(self, "manifest_digest", computed)
        return self


class Gate0ConformanceCheck(Gate0Model):
    name: str
    passed: bool
    details: str


class Gate0ConformanceReport(Gate0Model):
    schema_version: Literal[GATE0_CONFORMANCE_SCHEMA_VERSION] = GATE0_CONFORMANCE_SCHEMA_VERSION
    manifest_digest: str
    live_status: Literal["not_run"] = "not_run"
    checks: tuple[Gate0ConformanceCheck, ...]
    passed: bool


class Gate0LiveExecutionAuthorization(Gate0Model):
    schema_version: Literal[GATE0_LIVE_AUTHORIZATION_SCHEMA_VERSION] = (
        GATE0_LIVE_AUTHORIZATION_SCHEMA_VERSION
    )
    authorization_id: str
    authorization_digest: str = ""
    manifest_digest: str
    live_authorized: Literal[True] = True
    provider_identity: Gate0ProviderIdentity
    deployment_profile: HarnessDeploymentProfile
    profile_digest: str
    credential_reference: CredentialReference
    credential_reference_digest: str

    @field_validator(
        "authorization_id",
        "manifest_digest",
        "profile_digest",
        "credential_reference_digest",
    )
    @classmethod
    def validate_authorization_text(cls, value: str, info: Any) -> str:
        return _nonempty(value, info.field_name)

    @model_validator(mode="after")
    def bind_frozen_live_authority(self) -> "Gate0LiveExecutionAuthorization":
        profile = self.deployment_profile
        if self.profile_digest != harness_deployment_profile_digest(profile):
            raise ValueError("Gate0 live authorization crossed its frozen deployment profile")
        if (
            self.provider_identity.deployment_id != profile.deployment_id
            or self.provider_identity.provider != profile.provider
            or self.provider_identity.model != profile.model
            or self.provider_identity.profile_digest != self.profile_digest
            or self.provider_identity.provider_config_digest
            != profile.provider_config_digest
            or self.provider_identity.decoding_policy_digest
            != profile.decoding_policy_digest
            or self.provider_identity.price_schedule_digest
            != profile.price_schedule_digest
            or self.provider_identity.command_container_policy_digest
            != profile.command_container_policy_digest
        ):
            raise ValueError("Gate0 manifest provider identity crossed the frozen deployment profile")
        if self.credential_reference.provider_name != profile.provider:
            raise ValueError("Gate0 credential reference crossed the frozen provider")
        if (
            self.credential_reference.api_key_env != profile.endpoint.api_key_env
            or self.credential_reference.api_key_file_env
            != profile.endpoint.api_key_file_env
        ):
            raise ValueError("Gate0 credential reference differs from the frozen endpoint policy")
        computed_credential_digest = evidence_digest(
            {
                "kind": "repo-repair-credential-reference-v1",
                **self.credential_reference.model_dump(mode="python", exclude_none=True),
            }
        )
        if self.credential_reference_digest != computed_credential_digest:
            raise ValueError("Gate0 credential-reference digest mismatch")
        payload = self.model_dump(mode="python", exclude={"authorization_digest"})
        computed_authorization_digest = evidence_digest(
            {"kind": GATE0_LIVE_AUTHORIZATION_SCHEMA_VERSION, **payload}
        )
        if (
            self.authorization_digest
            and self.authorization_digest != computed_authorization_digest
        ):
            raise ValueError("Gate0 live authorization digest mismatch")
        if not self.authorization_digest:
            object.__setattr__(
                self,
                "authorization_digest",
                computed_authorization_digest,
            )
        assert_no_resolved_credentials(self.model_dump(mode="json"))
        return self


class Gate0Observation(Gate0Model):
    observation_id: str
    manifest_digest: str
    item_id: str
    template_id: Gate0TemplateId
    replicate_index: int = Field(ge=0, lt=GATE0_REPLICATES_PER_ITEM)
    arm: Gate0Arm
    pair_key: PairKey
    pair_key_digest: str
    provider_config_digest: str
    source_kind: Literal["deterministic_fixture", "provider_result"] = "deterministic_fixture"
    hard_invalid: bool
    correct_answer: bool

    @field_validator("observation_id", "manifest_digest", "item_id", "provider_config_digest")
    @classmethod
    def validate_nonempty(cls, value: str, info: Any) -> str:
        return _nonempty(value, info.field_name)

    @model_validator(mode="after")
    def validate_observation(self) -> "Gate0Observation":
        if self.pair_key_digest != pair_key_digest(self.pair_key):
            raise ValueError("Gate 0 observation PairKey digest mismatch")
        if self.pair_key.provider_config_digest != self.provider_config_digest:
            raise ValueError("Gate 0 observation provider configuration mismatch")
        return self


class Gate0ArmMetrics(Gate0Model):
    arm: Gate0Arm
    total: int
    hard_invalid_count: int
    hard_invalid_rate: float
    success_count: int
    success_rate: float


class Gate0ThresholdResult(Gate0Model):
    name: str
    value: float
    threshold: float
    comparator: Literal["<=", ">=", ">"]
    passed: bool


class Gate0AnalysisReport(Gate0Model):
    schema_version: Literal[GATE0_ANALYSIS_SCHEMA_VERSION] = GATE0_ANALYSIS_SCHEMA_VERSION
    runtime_contract_version: str = RUNTIME_CONTRACT_VERSION
    manifest_digest: str
    live_status: Gate0LiveStatus = "not_run"
    observation_source_kind: Literal["deterministic_fixture", "provider_result"]
    arm_metrics: tuple[Gate0ArmMetrics, ...]
    intact_minus_null_effect: float
    clustered_one_sided_95_lower_bound: float
    threshold_results: tuple[Gate0ThresholdResult, ...]
    numerical_gate_passed: bool
    analysis_digest: str = ""

    @model_validator(mode="after")
    def bind_digest(self) -> "Gate0AnalysisReport":
        computed = evidence_digest(
            {
                "kind": GATE0_ANALYSIS_SCHEMA_VERSION,
                "report": self.model_dump(mode="python", exclude={"analysis_digest"}),
            }
        )
        if self.analysis_digest and self.analysis_digest != computed:
            raise ValueError("Gate 0 analysis digest mismatch")
        if not self.analysis_digest:
            object.__setattr__(self, "analysis_digest", computed)
        return self


def build_gate0_panel() -> Gate0Panel:
    items: list[Gate0PanelItem] = []
    for index in range(GATE0_PANEL_ITEM_COUNT):
        template_id = GATE0_TEMPLATE_IDS[index % len(GATE0_TEMPLATE_IDS)]
        item_no = index + 1
        if template_id == "sum_mod":
            private_a = {"left": (17 * item_no + 3) % 97, "unit": f"A{item_no:02d}"}
            private_b = {"right": (29 * item_no + 11) % 97, "unit": f"B{item_no:02d}"}
            rule = "sum private left and right modulo 97"
        elif template_id == "xor_mask":
            private_a = {"mask": (37 * item_no + 5) % 256, "lane": f"A{item_no:02d}"}
            private_b = {"signal": (53 * item_no + 19) % 256, "lane": f"B{item_no:02d}"}
            rule = "xor private mask with private signal"
        elif template_id == "ordered_pair":
            private_a = {"prefix": chr(ord("A") + (item_no % 26)), "slot": f"A{item_no:02d}"}
            private_b = {"suffix": chr(ord("a") + ((item_no * 7) % 26)), "slot": f"B{item_no:02d}"}
            rule = "join private prefix and private suffix in order"
        else:
            private_a = {"baseline": 20 + ((item_no * 13) % 61), "case": f"A{item_no:02d}"}
            private_b = {"candidate": 20 + ((item_no * 31 + 7) % 61), "case": f"B{item_no:02d}"}
            rule = "answer HIGH when candidate exceeds baseline, otherwise LOW"
        item_id = f"gate0-item-{item_no:02d}"
        view_a = Gate0PrivateView(view_id="private_a", payload=private_a)
        view_b = Gate0PrivateView(view_id="private_b", payload=private_b)
        items.append(
            Gate0PanelItem(
                item_id=item_id,
                template_id=template_id,
                private_a=view_a,
                private_b=view_b,
                expected_answer=_expected_answer(template_id, private_a, private_b),
                resolution_rule=rule,
            )
        )
    return Gate0Panel(items=tuple(items))


def build_gate0_provider_identity(
    *,
    deployment_profile: HarnessDeploymentProfile,
) -> Gate0ProviderIdentity:
    profile = HarnessDeploymentProfile.model_validate(
        deployment_profile.model_dump(mode="python")
    )
    return Gate0ProviderIdentity(
        deployment_id=profile.deployment_id,
        provider=profile.provider,
        model=profile.model,
        profile_digest=harness_deployment_profile_digest(profile),
        provider_config_digest=profile.provider_config_digest,
        decoding_policy_digest=profile.decoding_policy_digest,
        price_schedule_digest=profile.price_schedule_digest,
        command_container_policy_digest=profile.command_container_policy_digest,
    )


def gate0_dry_run_manifest_identity_payload(
    manifest: Gate0DryRunManifest | Mapping[str, Any],
) -> dict[str, Any]:
    if isinstance(manifest, Gate0DryRunManifest):
        payload = manifest.model_dump(mode="python", exclude_none=True)
    else:
        payload = dict(manifest)
    payload.pop("manifest_digest", None)
    return payload


def gate0_dry_run_manifest_digest(manifest: Gate0DryRunManifest | Mapping[str, Any]) -> str:
    return evidence_digest(
        {
            "kind": GATE0_MANIFEST_SCHEMA_VERSION,
            "manifest": gate0_dry_run_manifest_identity_payload(manifest),
        }
    )


def _sample_pair_key(
    *,
    panel: Gate0Panel,
    provider_identity: Gate0ProviderIdentity,
    item_id: str,
    replicate_index: int,
    arm: Gate0Arm,
) -> PairKey:
    return PairKey(
        task_manifest_id=f"{panel.panel_id}:{item_id}:{arm}",
        environment_id=f"{panel.panel_id}:forced-exchange",
        sampling_replicate=replicate_index,
        provider_config_digest=provider_identity.provider_config_digest,
    )


def _producer_payload(panel: Gate0Panel, item: Gate0PanelItem, replicate_index: int) -> dict[str, Any]:
    return {
        "schema_version": GATE0_MANIFEST_SCHEMA_VERSION,
        "panel_id": panel.panel_id,
        "item_id": item.item_id,
        "template_id": item.template_id,
        "replicate_index": replicate_index,
        "actor_id": "producer_a",
        "instruction": "Emit only the private A share artifact in the declared schema.",
        "private_evidence": item.private_a.payload,
        "response_schema": {
            "type": "object",
            "required": ("artifact_text",),
            "properties": {"artifact_text": {"type": "string"}},
        },
    }


def _responder_payload(
    *,
    panel: Gate0Panel,
    item: Gate0PanelItem,
    replicate_index: int,
    arm: Gate0Arm,
) -> dict[str, Any]:
    artifact = _artifact_text(item.item_id, item.private_a.payload_digest)
    neutral = _neutral_text(artifact)
    payload: dict[str, Any] = {
        "schema_version": GATE0_MANIFEST_SCHEMA_VERSION,
        "panel_id": panel.panel_id,
        "item_id": item.item_id,
        "template_id": item.template_id,
        "replicate_index": replicate_index,
        "actor_id": "responder_b",
        "instruction": "Answer using only the evidence supplied in this request.",
        "answer_schema": {
            "type": "object",
            "required": ("answer",),
            "properties": {"answer": {"type": "string"}},
        },
    }
    if arm == "intact_exchange":
        payload.update(
            {
                "condition": "artifact_exchange",
                "private_evidence_b": item.private_b.payload,
                "delivered_artifact": {
                    "artifact_role": "producer_share",
                    "artifact_schema": "text",
                    "artifact_text": artifact,
                },
            }
        )
    elif arm == "matched_neutral_artifact":
        payload.update(
            {
                "condition": "artifact_exchange",
                "private_evidence_b": item.private_b.payload,
                "delivered_artifact": {
                    "artifact_role": "producer_share",
                    "artifact_schema": "text",
                    "artifact_text": neutral,
                },
            }
        )
    elif arm == "private_a_only":
        payload.update(
            {
                "condition": "private_a_only",
                "delivered_artifact": {
                    "artifact_role": "producer_share",
                    "artifact_schema": "text",
                    "artifact_text": artifact,
                }
            }
        )
    elif arm == "private_b_only":
        payload.update(
            {
                "condition": "private_b_only",
                "private_evidence_b": item.private_b.payload,
            }
        )
    else:
        payload.update(
            {
                "condition": "full_information",
                "private_evidence_a": item.private_a.payload,
                "private_evidence_b": item.private_b.payload,
            }
        )
    return payload


def _build_call(
    *,
    sample_id: str,
    item: Gate0PanelItem,
    replicate_index: int,
    arm: Gate0Arm,
    actor_id: Gate0ActorId,
    sequence_in_sample: int,
    pair_key: PairKey,
    request_payload: dict[str, Any],
) -> Gate0ProviderCallPlan:
    call_id = f"{sample_id}:{actor_id}"
    return Gate0ProviderCallPlan(
        call_id=call_id,
        sample_id=sample_id,
        item_id=item.item_id,
        template_id=item.template_id,
        replicate_index=replicate_index,
        arm=arm,
        actor_id=actor_id,
        sequence_in_sample=sequence_in_sample,
        pair_key=pair_key,
        pair_key_digest=pair_key_digest(pair_key),
        request_payload=request_payload,
    )


def _build_arm_plan(
    *,
    panel: Gate0Panel,
    provider_identity: Gate0ProviderIdentity,
    item: Gate0PanelItem,
    replicate_index: int,
    arm: Gate0Arm,
) -> Gate0ArmPlan:
    sample_id = f"{panel.panel_id}:{item.item_id}:r{replicate_index}:{arm}"
    pair_key = _sample_pair_key(
        panel=panel,
        provider_identity=provider_identity,
        item_id=item.item_id,
        replicate_index=replicate_index,
        arm=arm,
    )
    producer = _build_call(
        sample_id=sample_id,
        item=item,
        replicate_index=replicate_index,
        arm=arm,
        actor_id="producer_a",
        sequence_in_sample=0,
        pair_key=pair_key,
        request_payload=_producer_payload(panel, item, replicate_index),
    )
    responder = _build_call(
        sample_id=sample_id,
        item=item,
        replicate_index=replicate_index,
        arm=arm,
        actor_id="responder_b",
        sequence_in_sample=1,
        pair_key=pair_key,
        request_payload=_responder_payload(
            panel=panel,
            item=item,
            replicate_index=replicate_index,
            arm=arm,
        ),
    )
    return Gate0ArmPlan(
        sample_id=sample_id,
        item_id=item.item_id,
        template_id=item.template_id,
        replicate_index=replicate_index,
        arm=arm,
        pair_key=pair_key,
        pair_key_digest=pair_key_digest(pair_key),
        randomization_rank=_stable_rank(panel.frozen_seed, sample_id),
        calls=(producer, responder),
    )


def build_gate0_dry_run_manifest(
    *,
    provider_identity: Gate0ProviderIdentity,
    evidence_destination: str,
    panel: Gate0Panel | None = None,
) -> Gate0DryRunManifest:
    frozen_panel = panel or build_gate0_panel()
    arms = [
        _build_arm_plan(
            panel=frozen_panel,
            provider_identity=provider_identity,
            item=item,
            replicate_index=replicate_index,
            arm=arm,
        )
        for item in frozen_panel.items
        for replicate_index in range(GATE0_REPLICATES_PER_ITEM)
        for arm in GATE0_ARMS
    ]
    arms_by_key = {
        (arm_plan.item_id, arm_plan.replicate_index, arm_plan.arm): arm_plan
        for arm_plan in arms
    }
    matched_arms: list[Gate0ArmPlan] = []
    for arm_plan in arms:
        if arm_plan.arm in {"intact_exchange", "matched_neutral_artifact"}:
            intact = arms_by_key[
                (arm_plan.item_id, arm_plan.replicate_index, "intact_exchange")
            ]
            flags = {
                "schema_matched_to_intact": True,
                "serialized_length_matched_to_intact": True,
                "call_count_matched_to_intact": arm_plan.provider_call_count == intact.provider_call_count,
                "priced_input_matched_to_intact": (
                    arm_plan.total_priced_input_units == intact.total_priced_input_units
                ),
            }
            payload = arm_plan.model_dump(mode="python")
            payload.update(flags)
            payload["sample_digest"] = ""
            matched_arms.append(Gate0ArmPlan.model_validate(payload))
        else:
            matched_arms.append(arm_plan)
    ordered_for_schedule = sorted(
        matched_arms,
        key=lambda sample: (sample.randomization_rank, sample.sample_id),
    )
    schedule = tuple(
        call.call_id
        for sample in ordered_for_schedule
        for call in sample.calls
    )
    manifest_id = "gate0-dry-run." + evidence_digest(
        {
            "panel": frozen_panel.panel_digest,
            "provider": provider_identity.provider_config_digest,
            "destination": evidence_destination,
        }
    )[:24]
    return Gate0DryRunManifest(
        manifest_id=manifest_id,
        panel=frozen_panel,
        provider_identity=provider_identity,
        evidence_destination=evidence_destination,
        arms=tuple(matched_arms),
        provider_call_schedule=schedule,
    )


def require_gate0_live_authorization(
    manifest: Gate0DryRunManifest,
    *,
    deployment_profile: HarnessDeploymentProfile,
    live_authorized: bool,
    credential_reference: CredentialReference | None,
) -> Gate0LiveExecutionAuthorization:
    if not live_authorized:
        raise Gate0LiveExecutionBlocked("Gate 0 live execution requires an explicit live authorization flag")
    if not credential_reference:
        raise Gate0LiveExecutionBlocked("Gate 0 live execution requires a credential reference")
    try:
        profile_payload = (
            deployment_profile.model_dump(mode="python")
            if isinstance(deployment_profile, HarnessDeploymentProfile)
            else deployment_profile
        )
        credential_payload = (
            credential_reference.model_dump(mode="python")
            if isinstance(credential_reference, CredentialReference)
            else credential_reference
        )
        profile = HarnessDeploymentProfile.model_validate(profile_payload)
        credential = CredentialReference.model_validate(credential_payload)
        profile_digest = harness_deployment_profile_digest(profile)
        credential_digest = evidence_digest(
            {
                "kind": "repo-repair-credential-reference-v1",
                **credential.model_dump(mode="python", exclude_none=True),
            }
        )
        return Gate0LiveExecutionAuthorization(
            authorization_id="gate0-live-auth."
            + evidence_digest(
                {
                    "manifest": manifest.manifest_digest,
                    "provider_identity": manifest.provider_identity.provider_config_digest,
                    "profile": profile_digest,
                    "credential_reference": credential_digest,
                }
            )[:24],
            manifest_digest=manifest.manifest_digest,
            provider_identity=manifest.provider_identity,
            deployment_profile=profile,
            profile_digest=profile_digest,
            credential_reference=credential,
            credential_reference_digest=credential_digest,
        )
    except (TypeError, ValueError) as exc:
        raise Gate0LiveExecutionBlocked(str(exc)) from exc


def _responder_call(sample: Gate0ArmPlan) -> Gate0ProviderCallPlan:
    return sample.calls[1]


def validate_gate0_dry_run_conformance(manifest: Gate0DryRunManifest) -> Gate0ConformanceReport:
    checks: list[Gate0ConformanceCheck] = []

    def add(name: str, passed: bool, details: str) -> None:
        checks.append(Gate0ConformanceCheck(name=name, passed=passed, details=details))

    add(
        "frozen_panel_shape",
        len(manifest.panel.items) == 32
        and manifest.panel.replicates_per_item == 4
        and tuple(manifest.panel.templates) == GATE0_TEMPLATE_IDS,
        "32 independent items, four templates, four replicates per item",
    )
    arm_keys = [(sample.item_id, sample.replicate_index, sample.arm) for sample in manifest.arms]
    add(
        "arm_coverage",
        len(arm_keys) == len(set(arm_keys)) == 32 * 4 * len(GATE0_ARMS),
        "every item/replicate has all five locked arms exactly once",
    )
    call_ids = [call.call_id for sample in manifest.arms for call in sample.calls]
    add(
        "call_schedule",
        tuple(sorted(call_ids)) == tuple(sorted(manifest.provider_call_schedule))
        and len(manifest.provider_call_schedule) == len(set(manifest.provider_call_schedule)),
        "randomized schedule exactly covers planned provider calls",
    )
    add(
        "digest_semantics",
        manifest.manifest_digest == gate0_dry_run_manifest_digest(manifest)
        and all(sample.sample_digest for sample in manifest.arms)
        and all(call.request_digest and call.context_digest for sample in manifest.arms for call in sample.calls),
        "manifest, sample, request, and context digests are bound to canonical payloads",
    )
    add(
        "budget_semantics",
        manifest.total_provider_calls == len(call_ids)
        and manifest.total_priced_input_units
        == sum(sample.total_priced_input_units for sample in manifest.arms),
        "manifest totals equal per-call reserve-before-dispatch budgets",
    )
    leaked_expected = any(
        item.expected_answer in _canonical_json(call.request_payload)
        for item in manifest.panel.items
        for sample in manifest.arms
        if sample.item_id == item.item_id
        for call in sample.calls
    )
    add(
        "answer_leakage",
        not leaked_expected,
        "provider call payloads exclude expected answers",
    )

    by_key = {
        (sample.item_id, sample.replicate_index, sample.arm): sample
        for sample in manifest.arms
    }
    matched = True
    delivery_valid = True
    for item in manifest.panel.items:
        for replicate_index in range(GATE0_REPLICATES_PER_ITEM):
            intact = by_key[(item.item_id, replicate_index, "intact_exchange")]
            neutral = by_key[(item.item_id, replicate_index, "matched_neutral_artifact")]
            intact_responder = _responder_call(intact).request_payload
            neutral_responder = _responder_call(neutral).request_payload
            intact_artifact = intact_responder.get("delivered_artifact", {})
            neutral_artifact = neutral_responder.get("delivered_artifact", {})
            matched = matched and (
                intact.provider_call_count == neutral.provider_call_count
                and intact.total_priced_input_units == neutral.total_priced_input_units
                and len(str(intact_artifact.get("artifact_text", "")))
                == len(str(neutral_artifact.get("artifact_text", "")))
                and intact_artifact.get("artifact_schema") == neutral_artifact.get("artifact_schema")
                and intact_artifact.get("artifact_text") != neutral_artifact.get("artifact_text")
            )
            private_a = by_key[(item.item_id, replicate_index, "private_a_only")]
            private_b = by_key[(item.item_id, replicate_index, "private_b_only")]
            full = by_key[(item.item_id, replicate_index, "full_information")]
            delivery_valid = delivery_valid and (
                "private_evidence_b" in intact_responder
                and "delivered_artifact" in intact_responder
                and "private_evidence_b" in neutral_responder
                and "delivered_artifact" in neutral_responder
                and "delivered_artifact" in _responder_call(private_a).request_payload
                and "private_evidence_b" not in _responder_call(private_a).request_payload
                and "private_evidence_b" in _responder_call(private_b).request_payload
                and "delivered_artifact" not in _responder_call(private_b).request_payload
                and "private_evidence_a" in _responder_call(full).request_payload
                and "private_evidence_b" in _responder_call(full).request_payload
            )
    add(
        "intact_neutral_matching",
        matched,
        "neutral artifact arm is schema-, length-, call-, and priced-input-matched to intact",
    )
    add(
        "delivery_semantics",
        delivery_valid,
        "intact, neutral, private-only, and full-information payloads expose only their arm evidence",
    )
    passed = all(check.passed for check in checks)
    return Gate0ConformanceReport(
        manifest_digest=manifest.manifest_digest,
        checks=tuple(checks),
        passed=passed,
    )


def _observation_key(observation: Gate0Observation) -> tuple[str, int, Gate0Arm]:
    return (observation.item_id, observation.replicate_index, observation.arm)


def _observation_success(observation: Gate0Observation) -> int:
    return int((not observation.hard_invalid) and observation.correct_answer)


def analyze_gate0_observations(
    *,
    manifest: Gate0DryRunManifest,
    observations: tuple[Gate0Observation, ...] | list[Gate0Observation],
) -> Gate0AnalysisReport:
    expected = {
        (sample.item_id, sample.replicate_index, sample.arm): sample
        for sample in manifest.arms
    }
    seen: dict[tuple[str, int, Gate0Arm], Gate0Observation] = {}
    source_kinds = {observation.source_kind for observation in observations}
    if len(source_kinds) != 1:
        raise Gate0AnalysisError("Gate 0 observations must not mix source kinds")
    for observation in observations:
        key = _observation_key(observation)
        sample = expected.get(key)
        if sample is None:
            raise Gate0AnalysisError(f"unexpected Gate 0 observation: {key}")
        if key in seen:
            raise Gate0AnalysisError(f"duplicate Gate 0 observation: {key}")
        if observation.manifest_digest != manifest.manifest_digest:
            raise Gate0AnalysisError("Gate 0 observation crossed dry-run manifest")
        if observation.template_id != sample.template_id:
            raise Gate0AnalysisError("Gate 0 observation crossed template identity")
        if observation.pair_key != sample.pair_key or observation.pair_key_digest != sample.pair_key_digest:
            raise Gate0AnalysisError("Gate 0 observation crossed PairKey identity")
        if observation.provider_config_digest != manifest.provider_identity.provider_config_digest:
            raise Gate0AnalysisError("Gate 0 observation crossed provider configuration")
        seen[key] = observation
    missing = sorted(set(expected) - set(seen))
    if missing:
        raise Gate0AnalysisError(f"missing Gate 0 observations: {missing[:3]}")

    metrics: list[Gate0ArmMetrics] = []
    by_arm: dict[Gate0Arm, list[Gate0Observation]] = {arm: [] for arm in GATE0_ARMS}
    for observation in seen.values():
        by_arm[observation.arm].append(observation)
    for arm in GATE0_ARMS:
        arm_observations = by_arm[arm]
        total = len(arm_observations)
        hard_invalid_count = sum(int(item.hard_invalid) for item in arm_observations)
        success_count = sum(_observation_success(item) for item in arm_observations)
        metrics.append(
            Gate0ArmMetrics(
                arm=arm,
                total=total,
                hard_invalid_count=hard_invalid_count,
                hard_invalid_rate=hard_invalid_count / total,
                success_count=success_count,
                success_rate=success_count / total,
            )
        )
    metrics_by_arm = {metric.arm: metric for metric in metrics}
    invalid_rates = [metric.hard_invalid_rate for metric in metrics]
    intact_rate = metrics_by_arm["intact_exchange"].success_rate
    neutral_rate = metrics_by_arm["matched_neutral_artifact"].success_rate
    intact_minus_null = intact_rate - neutral_rate

    cluster_diffs: list[float] = []
    for item in manifest.panel.items:
        diffs = []
        for replicate_index in range(GATE0_REPLICATES_PER_ITEM):
            intact = seen[(item.item_id, replicate_index, "intact_exchange")]
            neutral = seen[(item.item_id, replicate_index, "matched_neutral_artifact")]
            diffs.append(_observation_success(intact) - _observation_success(neutral))
        cluster_diffs.append(sum(diffs) / len(diffs))
    mean_diff = sum(cluster_diffs) / len(cluster_diffs)
    if len(cluster_diffs) < 2:
        lower_bound = mean_diff
    else:
        variance = sum((value - mean_diff) ** 2 for value in cluster_diffs) / (len(cluster_diffs) - 1)
        standard_error = math.sqrt(variance) / math.sqrt(len(cluster_diffs))
        lower_bound = mean_diff - GATE0_CLUSTER_T_CRITICAL_ONE_SIDED_95_DF31 * standard_error

    thresholds = manifest.thresholds

    def threshold_result(name: str, value: float, threshold: float, comparator: Literal["<=", ">=", ">"]) -> Gate0ThresholdResult:
        if comparator == "<=":
            passed = value <= threshold
        elif comparator == ">=":
            passed = value >= threshold
        else:
            passed = value > threshold
        return Gate0ThresholdResult(
            name=name,
            value=value,
            threshold=threshold,
            comparator=comparator,
            passed=passed,
        )

    threshold_results = [
        threshold_result(
            "max_hard_invalid_rate_per_arm",
            max(invalid_rates),
            thresholds.max_hard_invalid_rate_per_arm,
            "<=",
        ),
        threshold_result(
            "hard_invalid_rate_spread",
            max(invalid_rates) - min(invalid_rates),
            thresholds.max_hard_invalid_rate_spread,
            "<=",
        ),
        threshold_result(
            "full_information_success_rate",
            metrics_by_arm["full_information"].success_rate,
            thresholds.min_full_information_success_rate,
            ">=",
        ),
        threshold_result(
            "private_a_only_success_rate",
            metrics_by_arm["private_a_only"].success_rate,
            thresholds.max_private_view_success_rate,
            "<=",
        ),
        threshold_result(
            "private_b_only_success_rate",
            metrics_by_arm["private_b_only"].success_rate,
            thresholds.max_private_view_success_rate,
            "<=",
        ),
        threshold_result(
            "intact_exchange_success_rate",
            intact_rate,
            thresholds.min_intact_exchange_success_rate,
            ">=",
        ),
        threshold_result(
            "intact_minus_null_effect",
            intact_minus_null,
            thresholds.min_intact_minus_null_effect,
            ">=",
        ),
        threshold_result(
            "clustered_one_sided_95_lower_bound",
            lower_bound,
            thresholds.min_clustered_one_sided_95_lower_bound,
            ">",
        ),
    ]
    return Gate0AnalysisReport(
        manifest_digest=manifest.manifest_digest,
        live_status="executed" if source_kinds == {"provider_result"} else "not_run",
        observation_source_kind=next(iter(source_kinds)) if source_kinds else "deterministic_fixture",
        arm_metrics=tuple(metrics),
        intact_minus_null_effect=intact_minus_null,
        clustered_one_sided_95_lower_bound=lower_bound,
        threshold_results=tuple(threshold_results),
        numerical_gate_passed=all(result.passed for result in threshold_results),
    )


def write_gate0_json_atomic(
    path: str | Path,
    payload: Mapping[str, Any] | BaseModel,
    *,
    overwrite: bool = False,
) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not overwrite:
        raise FileExistsError(f"Gate 0 evidence path already exists: {target}")
    if isinstance(payload, BaseModel):
        json_payload: Any = payload.model_dump(mode="json", exclude_none=True)
    else:
        json_payload = payload
    data = (_canonical_json(json_payload) + "\n").encode("utf-8")
    temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with open(temp, "xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    finally:
        if temp.exists():
            temp.unlink()
    return target


def write_gate0_preregistration(path: str | Path, manifest: Gate0DryRunManifest) -> Path:
    conformance = validate_gate0_dry_run_conformance(manifest)
    payload = {
        "schema_version": GATE0_PREREGISTRATION_SCHEMA_VERSION,
        "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
        "live_status": "not_run",
        "manifest": manifest.model_dump(mode="json", exclude_none=True),
        "conformance": conformance.model_dump(mode="json", exclude_none=True),
    }
    payload["preregistration_digest"] = evidence_digest(
        {
            "kind": GATE0_PREREGISTRATION_SCHEMA_VERSION,
            "payload": payload,
        }
    )
    return write_gate0_json_atomic(path, payload)


__all__ = [
    "GATE0_ARMS",
    "GATE0_LIVE_AUTHORIZATION_SCHEMA_VERSION",
    "GATE0_PANEL_ITEM_COUNT",
    "GATE0_REPLICATES_PER_ITEM",
    "GATE0_TEMPLATE_IDS",
    "Gate0AnalysisError",
    "Gate0AnalysisReport",
    "Gate0Arm",
    "Gate0ArmMetrics",
    "Gate0ArmPlan",
    "Gate0ConformanceReport",
    "Gate0DryRunManifest",
    "Gate0Error",
    "Gate0LiveExecutionAuthorization",
    "Gate0LiveExecutionBlocked",
    "Gate0Observation",
    "Gate0Panel",
    "Gate0PanelItem",
    "Gate0PrivateView",
    "Gate0ProviderCallPlan",
    "Gate0ProviderIdentity",
    "Gate0ThresholdResult",
    "Gate0Thresholds",
    "analyze_gate0_observations",
    "build_gate0_dry_run_manifest",
    "build_gate0_panel",
    "build_gate0_provider_identity",
    "gate0_dry_run_manifest_digest",
    "require_gate0_live_authorization",
    "validate_gate0_dry_run_conformance",
    "write_gate0_json_atomic",
    "write_gate0_preregistration",
]
