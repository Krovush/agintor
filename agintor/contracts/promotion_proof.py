from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..core.identity import evidence_digest
from ..core.versioning import RUNTIME_CONTRACT_VERSION
from .outcomes import OutcomeReceipt, PairKey, outcome_receipt_digest, pair_key_digest
from .run_evidence import (
    RUN_EVIDENCE_SCHEMA_VERSION,
    RunEvidence,
    RunProofRecord,
)


EVALUATOR_OUTCOME_PROOF_BINDING_SCHEMA_VERSION = (
    "repo-repair-evaluator-outcome-proof-binding-v1"
)
PUBLIC_PROMOTION_PROOF_SCHEMA_VERSION = "repo-repair-public-promotion-proof-v1"

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


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


def _controlled_ref(value: str, field_name: str) -> str:
    normalized = str(value or "").strip().replace("\\", "/")
    path = Path(normalized)
    if (
        not normalized
        or path.is_absolute()
        or ".." in path.parts
        or normalized.startswith("/")
    ):
        raise ValueError(f"{field_name} must be a controlled store-relative path")
    return normalized


class PromotionProofModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)


class PromotionRunEvidenceProjection(PromotionProofModel):
    """Public identities needed to bind an outcome to its stored RunEvidence."""

    schema_version: Literal[RUN_EVIDENCE_SCHEMA_VERSION] = RUN_EVIDENCE_SCHEMA_VERSION
    runtime_contract_version: str = RUNTIME_CONTRACT_VERSION
    evidence_id: str
    evidence_digest: str
    run_id: str
    execution_mode: Literal["deterministic_replay", "live_provider"]
    live_inference_status: Literal["not_run", "completed", "failed"]
    real_inference_requests_sent: int = Field(ge=0)
    arm: Literal["intact", "neutral_artifact"]
    intervention_digest: str | None = None
    capability_epoch: str
    data_state: str
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
    cost_ledger_digest: str
    runtime_environment_digest: str
    patch_digest: str
    healthy: bool

    @field_validator(
        "evidence_id",
        "run_id",
        "capability_epoch",
        "data_state",
        "epoch_id",
        "deployment_id",
        "provider",
        "model",
    )
    @classmethod
    def validate_nonempty(cls, value: str, info: Any) -> str:
        return _nonempty(value, info.field_name)

    @field_validator(
        "evidence_digest",
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
        "cost_ledger_digest",
        "runtime_environment_digest",
        "patch_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _digest(value, info.field_name)

    @field_validator("intervention_digest")
    @classmethod
    def validate_optional_digest(cls, value: str | None) -> str | None:
        return None if value is None else _digest(value, "intervention_digest")

    @model_validator(mode="after")
    def validate_projection(self) -> "PromotionRunEvidenceProjection":
        if self.runtime_contract_version != RUNTIME_CONTRACT_VERSION:
            raise ValueError("run-evidence projection runtime contract version mismatch")
        if self.pair_key.provider_config_digest != self.provider_config_digest:
            raise ValueError("run-evidence projection crossed PairKey provider configuration")
        if (self.arm == "neutral_artifact") != bool(self.intervention_digest):
            raise ValueError("run-evidence projection intervention identity is inconsistent")
        if self.execution_mode == "deterministic_replay" and (
            self.live_inference_status != "not_run"
            or self.real_inference_requests_sent != 0
        ):
            raise ValueError("replay projection may not claim live inference")
        if self.execution_mode == "live_provider" and (
            self.live_inference_status == "not_run"
        ) != (self.real_inference_requests_sent == 0):
            raise ValueError("live projection inference status and sent count disagree")
        return self

    @classmethod
    def from_run_evidence(cls, evidence: RunEvidence) -> "PromotionRunEvidenceProjection":
        canonical = RunEvidence.model_validate(evidence.model_dump(mode="python"))
        return cls(
            schema_version=canonical.schema_version,
            runtime_contract_version=canonical.runtime_contract_version,
            evidence_id=canonical.evidence_id,
            evidence_digest=canonical.evidence_digest,
            run_id=canonical.run_id,
            execution_mode=canonical.execution_mode,
            live_inference_status=canonical.live_inference_status,
            real_inference_requests_sent=canonical.real_inference_requests_sent,
            arm=canonical.arm,
            intervention_digest=canonical.intervention_digest,
            capability_epoch=canonical.capability_epoch,
            data_state=canonical.data_state,
            epoch_id=canonical.epoch_id,
            epoch_manifest_digest=canonical.epoch_manifest_digest,
            release_digest=canonical.release_digest,
            release_manifest_digest=canonical.release_manifest_digest,
            profile_digest=canonical.profile_digest,
            split_manifest_digest=canonical.split_manifest_digest,
            pair_key=canonical.pair_key,
            task_manifest_digest=canonical.task_manifest_digest,
            protocol_digest=canonical.protocol_digest,
            compiled_semantic_digest=canonical.compiled_semantic_digest,
            dependency_manifest_digest=canonical.dependency_manifest_digest,
            compiler_digest=canonical.compiler_digest,
            kernel_digest=canonical.kernel_digest,
            tool_manifest_digest=canonical.tool_manifest_digest,
            provider_config_digest=canonical.provider_config_digest,
            decoding_policy_digest=canonical.decoding_policy_digest,
            price_schedule_digest=canonical.price_schedule_digest,
            command_container_policy_digest=canonical.command_container_policy_digest,
            deployment_id=canonical.deployment_id,
            provider=canonical.provider,
            model=canonical.model,
            cost_ledger_digest=canonical.cost_ledger.ledger_digest,
            runtime_environment_digest=(
                canonical.environment.runtime_environment_digest
            ),
            patch_digest=canonical.patch.patch_digest,
            healthy=canonical.health.healthy,
        )


class EvaluatorOutcomeProofBinding(PromotionProofModel):
    """Public-safe handle to evaluator-owned proof-store authority.

    The digests make accidental crossing detectable; they do not replace the
    evaluator process or immutable proof store as the outcome authority.
    """

    schema_version: Literal[EVALUATOR_OUTCOME_PROOF_BINDING_SCHEMA_VERSION] = (
        EVALUATOR_OUTCOME_PROOF_BINDING_SCHEMA_VERSION
    )
    runtime_contract_version: str = RUNTIME_CONTRACT_VERSION
    binding_digest: str = ""
    outcome_receipt: OutcomeReceipt
    proof_record_id: str
    proof_record_digest: str
    public_proof_digest: str = ""
    run_evidence: PromotionRunEvidenceProjection
    run_evidence_digest: str
    store_manifest_ref: str = "store_manifest.json"
    proof_record_ref: str
    outcome_link_ref: str

    @field_validator("proof_record_id")
    @classmethod
    def validate_nonempty(cls, value: str) -> str:
        return _nonempty(value, "proof_record_id")

    @field_validator(
        "binding_digest",
        "proof_record_digest",
        "public_proof_digest",
        "run_evidence_digest",
    )
    @classmethod
    def validate_optional_or_required_digest(cls, value: str, info: Any) -> str:
        if info.field_name in {"binding_digest", "public_proof_digest"} and not value:
            return ""
        return _digest(value, info.field_name)

    @field_validator("store_manifest_ref", "proof_record_ref", "outcome_link_ref")
    @classmethod
    def validate_controlled_ref(cls, value: str, info: Any) -> str:
        return _controlled_ref(value, info.field_name)

    @model_validator(mode="after")
    def validate_binding(self) -> "EvaluatorOutcomeProofBinding":
        if self.runtime_contract_version != RUNTIME_CONTRACT_VERSION:
            raise ValueError("proof binding runtime contract version mismatch")
        receipt = self.outcome_receipt
        evidence = self.run_evidence
        if receipt.receipt_digest != outcome_receipt_digest(receipt):
            raise ValueError("proof binding contains a non-canonical OutcomeReceipt")
        if self.run_evidence_digest != evidence.evidence_digest:
            raise ValueError("proof binding crossed its RunEvidence digest")

        identity_fields = (
            "runtime_contract_version",
            "execution_mode",
            "live_inference_status",
            "real_inference_requests_sent",
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
            "patch_digest",
        )
        crossed = [
            field_name
            for field_name in identity_fields
            if getattr(receipt, field_name) != getattr(evidence, field_name)
        ]
        if crossed:
            raise ValueError(
                "OutcomeReceipt crossed public RunEvidence identities: "
                + ", ".join(crossed)
            )
        if receipt.pair_key != evidence.pair_key:
            raise ValueError("OutcomeReceipt PairKey crossed public RunEvidence")
        if receipt.task_manifest_id != evidence.pair_key.task_manifest_id:
            raise ValueError("OutcomeReceipt task identity crossed public RunEvidence")

        expected_record_ref = (
            f"runs/{pair_key_digest(evidence.pair_key)}/"
            f"{evidence.protocol_digest}/{evidence.evidence_digest}.json"
        )
        expected_outcome_ref = f"outcome_links/{receipt.receipt_digest}.json"
        if self.store_manifest_ref != "store_manifest.json":
            raise ValueError("proof binding crossed the proof-store manifest")
        if self.proof_record_ref != expected_record_ref:
            raise ValueError("proof binding record ref crossed PairKey/protocol/RunEvidence")
        if self.outcome_link_ref != expected_outcome_ref:
            raise ValueError("proof binding outcome ref crossed OutcomeReceipt")

        computed_public = public_promotion_proof_digest(self)
        if self.public_proof_digest and self.public_proof_digest != computed_public:
            raise ValueError("public_proof_digest does not match the public proof projection")
        if not self.public_proof_digest:
            object.__setattr__(self, "public_proof_digest", computed_public)

        computed_binding = evaluator_outcome_proof_binding_digest(self)
        if self.binding_digest and self.binding_digest != computed_binding:
            raise ValueError("binding_digest does not match evaluator outcome proof binding")
        if not self.binding_digest:
            object.__setattr__(self, "binding_digest", computed_binding)
        return self


def public_promotion_proof_payload(
    binding: EvaluatorOutcomeProofBinding,
) -> dict[str, Any]:
    return {
        "schema_version": PUBLIC_PROMOTION_PROOF_SCHEMA_VERSION,
        "runtime_contract_version": binding.runtime_contract_version,
        "proof_record_id": binding.proof_record_id,
        "proof_record_digest": binding.proof_record_digest,
        "run_evidence": binding.run_evidence.model_dump(mode="python", exclude_none=True),
        "run_evidence_digest": binding.run_evidence_digest,
        "outcome_receipt_digest": binding.outcome_receipt.receipt_digest,
        "store_manifest_ref": binding.store_manifest_ref,
        "proof_record_ref": binding.proof_record_ref,
        "outcome_link_ref": binding.outcome_link_ref,
    }


def public_promotion_proof_digest(binding: EvaluatorOutcomeProofBinding) -> str:
    return evidence_digest(public_promotion_proof_payload(binding))


def evaluator_outcome_proof_binding_digest(
    binding: EvaluatorOutcomeProofBinding,
) -> str:
    return evidence_digest(
        {
            "kind": EVALUATOR_OUTCOME_PROOF_BINDING_SCHEMA_VERSION,
            "outcome_receipt_digest": binding.outcome_receipt.receipt_digest,
            "public_proof_digest": binding.public_proof_digest,
        }
    )


def bind_evaluator_outcome_proof(
    record: RunProofRecord,
    *,
    proof_record_ref: str,
    outcome_link_ref: str,
    store_manifest_ref: str = "store_manifest.json",
) -> EvaluatorOutcomeProofBinding:
    """Create a public binding only after validating the canonical stored record."""

    canonical = RunProofRecord.model_validate(record.model_dump(mode="python"))
    receipt = canonical.outcome_receipt
    if receipt is None:
        raise ValueError("promotion proof binding requires an evaluator OutcomeReceipt")
    return EvaluatorOutcomeProofBinding(
        outcome_receipt=receipt,
        proof_record_id=canonical.proof_record_id,
        proof_record_digest=canonical.proof_record_digest,
        run_evidence=PromotionRunEvidenceProjection.from_run_evidence(
            canonical.run_evidence
        ),
        run_evidence_digest=canonical.run_evidence.evidence_digest,
        store_manifest_ref=store_manifest_ref,
        proof_record_ref=proof_record_ref,
        outcome_link_ref=outcome_link_ref,
    )


__all__ = [
    "EVALUATOR_OUTCOME_PROOF_BINDING_SCHEMA_VERSION",
    "PUBLIC_PROMOTION_PROOF_SCHEMA_VERSION",
    "EvaluatorOutcomeProofBinding",
    "PromotionRunEvidenceProjection",
    "bind_evaluator_outcome_proof",
    "evaluator_outcome_proof_binding_digest",
    "public_promotion_proof_digest",
    "public_promotion_proof_payload",
]
