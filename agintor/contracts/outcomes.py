from __future__ import annotations

import re
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..core.identity import evidence_digest
from ..core.versioning import RUNTIME_CONTRACT_VERSION
from .epochs import CapabilityEpoch, DataState, REPO_REPAIR_CAPABILITY_EPOCH


_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class OutcomeContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)


def _digest(value: str, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _DIGEST_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return normalized


def _nonempty(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} may not be empty")
    return normalized


class PairKey(OutcomeContractModel):
    task_manifest_id: str
    environment_id: str
    sampling_replicate: int = Field(ge=0)
    provider_config_digest: str

    @field_validator("task_manifest_id", "environment_id")
    @classmethod
    def validate_nonempty(cls, value: str, info: Any) -> str:
        return _nonempty(value, info.field_name)

    @field_validator("provider_config_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _digest(value, "provider_config_digest")


class OutcomeHealth(OutcomeContractModel):
    process_integrity: bool
    no_leakage: bool
    environment_integrity: bool
    evaluator_integrity: bool
    accounting_complete: bool

    @property
    def passes_promotion_floor(self) -> bool:
        return all(
            (
                self.process_integrity,
                self.no_leakage,
                self.environment_integrity,
                self.evaluator_integrity,
                self.accounting_complete,
            )
        )


class OutcomeCost(OutcomeContractModel):
    model_calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_tokens: int = Field(ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    tool_calls: int = Field(ge=0)
    tool_output_bytes: int = Field(ge=0)
    artifact_bytes: int = Field(ge=0)
    patch_bytes: int = Field(ge=0)
    retries: int = Field(ge=0)
    wall_time_ms: int = Field(ge=0)
    known_cost_usd: float = Field(ge=0.0)
    estimated_cost_usd: float = Field(ge=0.0)
    unknown_dollars: bool = False
    within_epoch_envelope: bool

    @model_validator(mode="after")
    def validate_input_subcategories(self) -> "OutcomeCost":
        if self.cached_tokens + self.cache_write_tokens > self.input_tokens:
            raise ValueError("cached and cache-write tokens must be input-token subcategories")
        return self

class DiagnosticScore(OutcomeContractModel):
    name: str
    value: float

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _nonempty(value, "diagnostic score name")


class OutcomeReceipt(OutcomeContractModel):
    runtime_contract_version: str = RUNTIME_CONTRACT_VERSION
    receipt_id: str
    receipt_digest: str = ""
    execution_mode: Literal["deterministic_replay", "live_provider"]
    live_inference_status: Literal["not_run", "completed", "failed"]
    real_inference_requests_sent: int = Field(ge=0)
    authority: Literal["evaluator_owned"] = "evaluator_owned"
    outcome_kind: Literal["complete_repair"] = "complete_repair"
    capability_epoch: CapabilityEpoch = REPO_REPAIR_CAPABILITY_EPOCH
    data_state: DataState
    epoch_id: str
    epoch_manifest_digest: str
    release_digest: str
    release_manifest_digest: str
    profile_digest: str
    split_manifest_digest: str
    task_manifest_id: str
    task_manifest_digest: str
    evaluation_contract_id: str
    evaluation_contract_digest: str
    evaluator_id: str
    evaluator_identity_digest: str
    evaluation_policy_digest: str
    pair_key: PairKey
    protocol_digest: str
    compiler_digest: str
    kernel_digest: str
    tool_manifest_digest: str
    provider_config_digest: str
    decoding_policy_digest: str
    price_schedule_digest: str
    command_container_policy_digest: str
    evaluator_environment_digest: str
    patch_digest: str
    complete_repair: bool
    health: OutcomeHealth
    exclusions: tuple[str, ...] = ()
    cost: OutcomeCost
    diagnostics: tuple[DiagnosticScore, ...] = ()
    issued_at_ms: int = Field(ge=0)

    @field_validator("runtime_contract_version")
    @classmethod
    def validate_contract_version(cls, value: str) -> str:
        if str(value) != RUNTIME_CONTRACT_VERSION:
            raise ValueError("outcome receipt runtime contract version mismatch")
        return str(value)

    @field_validator(
        "receipt_id",
        "epoch_id",
        "task_manifest_id",
        "evaluation_contract_id",
        "evaluator_id",
    )
    @classmethod
    def validate_nonempty(cls, value: str, info: Any) -> str:
        return _nonempty(value, info.field_name)

    @field_validator(
        "epoch_manifest_digest",
        "release_digest",
        "release_manifest_digest",
        "profile_digest",
        "split_manifest_digest",
        "task_manifest_digest",
        "evaluation_contract_digest",
        "evaluator_identity_digest",
        "evaluation_policy_digest",
        "protocol_digest",
        "compiler_digest",
        "kernel_digest",
        "tool_manifest_digest",
        "provider_config_digest",
        "decoding_policy_digest",
        "price_schedule_digest",
        "command_container_policy_digest",
        "evaluator_environment_digest",
        "patch_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _digest(value, info.field_name)

    @model_validator(mode="after")
    def validate_receipt(self) -> "OutcomeReceipt":
        if self.execution_mode == "deterministic_replay" and (
            self.live_inference_status != "not_run"
            or self.real_inference_requests_sent != 0
        ):
            raise ValueError("deterministic evaluator receipt cannot claim live inference")
        if self.execution_mode == "live_provider" and (
            self.live_inference_status == "not_run"
        ) != (self.real_inference_requests_sent == 0):
            raise ValueError("live evaluator receipt status disagrees with request count")
        if self.task_manifest_id != self.pair_key.task_manifest_id:
            raise ValueError("outcome receipt task_manifest_id does not match its PairKey")
        if len(self.exclusions) != len(set(self.exclusions)):
            raise ValueError("outcome receipt exclusions may not contain duplicates")
        if any(not str(reason).strip() for reason in self.exclusions):
            raise ValueError("outcome receipt exclusions may not contain empty reasons")
        computed = outcome_receipt_digest(self)
        if self.receipt_digest and self.receipt_digest != computed:
            raise ValueError("receipt_digest does not match the outcome receipt")
        if not self.receipt_digest:
            object.__setattr__(self, "receipt_digest", computed)
        return self


def pair_key_payload(pair_key: PairKey) -> dict[str, Any]:
    return {
        "task_manifest_id": pair_key.task_manifest_id,
        "environment_id": pair_key.environment_id,
        "sampling_replicate": pair_key.sampling_replicate,
        "provider_config_digest": pair_key.provider_config_digest,
    }


def pair_key_digest(pair_key: PairKey) -> str:
    return evidence_digest({"kind": "pair-key", **pair_key_payload(pair_key)})


def outcome_receipt_identity_payload(
    receipt: OutcomeReceipt | Mapping[str, Any],
) -> dict[str, Any]:
    if isinstance(receipt, OutcomeReceipt):
        payload = receipt.model_dump(mode="python", exclude_none=True)
    else:
        payload = dict(receipt)
    payload.pop("receipt_digest", None)
    return payload


def outcome_receipt_digest(receipt: OutcomeReceipt | Mapping[str, Any]) -> str:
    return evidence_digest(
        {
            "kind": "repo-repair-outcome-receipt-v1",
            "receipt": outcome_receipt_identity_payload(receipt),
        }
    )


__all__ = [
    "DiagnosticScore",
    "OutcomeContractModel",
    "OutcomeCost",
    "OutcomeHealth",
    "OutcomeReceipt",
    "PairKey",
    "outcome_receipt_digest",
    "outcome_receipt_identity_payload",
    "pair_key_digest",
    "pair_key_payload",
]
