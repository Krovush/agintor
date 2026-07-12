from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..core.identity import evidence_digest
from .outcomes import PairKey, pair_key_digest
from .run_evidence import assert_no_resolved_credentials


FEASIBILITY_SCHEMA_VERSION = "repo-repair-task-feasibility-v1"
D0_LIVE_BASELINE_PROOF_SCHEMA_VERSION = "repo-repair-d0-live-baseline-proof-v1"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class FeasibilityModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)


def d0_evaluation_contract_authority_digest(
    *,
    evaluation_contract_id: str,
    evaluation_contract_digest: str,
) -> str:
    contract_id = str(evaluation_contract_id or "").strip()
    contract_digest = str(evaluation_contract_digest or "").strip().lower()
    if not contract_id:
        raise ValueError("evaluation_contract_id may not be empty")
    if not _DIGEST_RE.fullmatch(contract_digest):
        raise ValueError(
            "evaluation_contract_digest must be a lowercase SHA-256 digest"
        )
    return evidence_digest(
        {
            "kind": "repo-repair-d0-evaluation-contract-authority-v1",
            "evaluation_contract_id": contract_id,
            "evaluation_contract_digest": contract_digest,
        }
    )


class FeasibilityControlResult(FeasibilityModel):
    control_id: str
    control_kind: Literal[
        "known_good",
        "empty",
        "plausible_wrong",
        "protected_tamper",
    ]
    artifact_digest: str
    expected_complete_repair: bool
    observed_complete_repair: bool
    evaluator_status: str
    outcome_fingerprint: str
    replay_fingerprint: str | None = None
    reproducible: bool | None = None
    source_snapshot_unchanged: bool
    scratch_snapshot_matched: bool
    fixture_identity_matched: bool
    protected_tamper_detected: bool
    passed: bool


class BaselineHeadroomAssessment(FeasibilityModel):
    status: Literal["not_measured", "has_headroom", "saturated", "uniform_failure"]
    receipt_count: int = Field(ge=0)
    complete_repairs: int = Field(ge=0)
    failures: int = Field(ge=0)
    protocol_digest: str | None = None
    receipt_digests: tuple[str, ...] = ()
    mean_model_calls: float | None = Field(default=None, ge=0.0)
    mean_wall_time_ms: float | None = Field(default=None, ge=0.0)
    mean_known_cost_usd: float | None = Field(default=None, ge=0.0)
    mean_estimated_cost_usd: float | None = Field(default=None, ge=0.0)

    @field_validator("protocol_digest")
    @classmethod
    def validate_optional_digest(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip().lower()
        if not _DIGEST_RE.fullmatch(normalized):
            raise ValueError(
                "baseline protocol_digest must be a lowercase SHA-256 digest"
            )
        return normalized


def baseline_headroom_assessment_digest(
    value: BaselineHeadroomAssessment,
) -> str:
    return evidence_digest(
        {
            "kind": "repo-repair-baseline-headroom-assessment-v1",
            **value.model_dump(mode="python"),
        }
    )


class D0LiveBaselineProof(FeasibilityModel):
    """Public D0 proof projection; evaluator-private evidence never enters it."""

    schema_version: Literal[D0_LIVE_BASELINE_PROOF_SCHEMA_VERSION] = (
        D0_LIVE_BASELINE_PROOF_SCHEMA_VERSION
    )
    proof_digest: str = ""
    authorization_digest: str
    report_digest: str
    provider_dry_run_digest: str
    epoch_id: str
    epoch_manifest_digest: str
    task_manifest_id: str
    task_manifest_digest: str
    deployment_id: str
    provider: str
    model: str
    provider_config_digest: str
    decoding_policy_digest: str
    price_schedule_digest: str
    command_container_policy_digest: str
    profile_digest: str
    evaluator_id: str
    evaluator_identity_digest: str
    evaluation_policy_digest: str
    evaluation_contract_authority_digest: str
    baseline_protocol_digest: str
    pair_keys: tuple[PairKey, ...] = Field(min_length=1)
    pair_key_digests: tuple[str, ...] = Field(min_length=1)
    scheduled_pair_count: int = Field(gt=0)
    completed_call_count: int = Field(gt=0)
    receipt_digests: tuple[str, ...] = Field(min_length=1)
    receipt_count: int = Field(gt=0)
    complete_repairs: int = Field(gt=0)
    failures: int = Field(gt=0)
    baseline_headroom_status: Literal["has_headroom"] = "has_headroom"
    baseline_headroom_digest: str
    execution_mode: Literal["live_provider"] = "live_provider"
    live_inference_status: Literal["completed"] = "completed"
    status: Literal["completed"] = "completed"
    real_inference_requests_sent: int = Field(gt=0)
    total_model_calls: int = Field(gt=0)
    unknown_usage_event_count: Literal[0] = 0
    unknown_cost_event_count: Literal[0] = 0
    total_known_cost_usd: float = Field(ge=0.0)
    total_estimated_cost_usd: float = Field(ge=0.0)
    provenance_digest: str = ""

    @field_validator(
        "epoch_id",
        "task_manifest_id",
        "deployment_id",
        "provider",
        "model",
        "evaluator_id",
    )
    @classmethod
    def validate_text(cls, value: str, info: object) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            field_name = getattr(info, "field_name", "field")
            raise ValueError(f"{field_name} may not be empty")
        return normalized

    @field_validator(
        "proof_digest",
        "authorization_digest",
        "report_digest",
        "provider_dry_run_digest",
        "epoch_manifest_digest",
        "task_manifest_digest",
        "provider_config_digest",
        "decoding_policy_digest",
        "price_schedule_digest",
        "command_container_policy_digest",
        "profile_digest",
        "evaluator_identity_digest",
        "evaluation_policy_digest",
        "evaluation_contract_authority_digest",
        "baseline_protocol_digest",
        "baseline_headroom_digest",
        "provenance_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: object) -> str:
        field_name = str(getattr(info, "field_name", "digest"))
        if field_name in {"proof_digest", "provenance_digest"} and not value:
            return ""
        normalized = str(value or "").strip().lower()
        if not _DIGEST_RE.fullmatch(normalized):
            raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
        return normalized

    @field_validator("pair_key_digests", "receipt_digests")
    @classmethod
    def validate_digest_sequence(
        cls,
        value: tuple[str, ...],
        info: object,
    ) -> tuple[str, ...]:
        field_name = str(getattr(info, "field_name", "digests"))
        normalized = tuple(str(item or "").strip().lower() for item in value)
        if any(not _DIGEST_RE.fullmatch(item) for item in normalized):
            raise ValueError(f"{field_name} must contain lowercase SHA-256 digests")
        if len(normalized) != len(set(normalized)):
            raise ValueError(f"{field_name} may not contain duplicates")
        return normalized

    @model_validator(mode="after")
    def bind_public_proof(self) -> "D0LiveBaselineProof":
        computed_pair_digests = tuple(pair_key_digest(pair) for pair in self.pair_keys)
        if self.pair_key_digests != computed_pair_digests:
            raise ValueError("D0 public proof PairKey digests crossed their ordered payloads")
        if any(
            pair.task_manifest_id != self.task_manifest_id
            or pair.provider_config_digest != self.provider_config_digest
            for pair in self.pair_keys
        ):
            raise ValueError("D0 public proof PairKeys crossed task or provider authority")
        if tuple(pair.sampling_replicate for pair in self.pair_keys) != tuple(
            range(len(self.pair_keys))
        ) or len({pair.environment_id for pair in self.pair_keys}) != 1:
            raise ValueError("D0 public proof PairKeys are not one canonical environment panel")
        if not (
            self.scheduled_pair_count
            == self.completed_call_count
            == self.receipt_count
            == len(self.pair_keys)
            == len(self.receipt_digests)
        ):
            raise ValueError("D0 public proof counts do not cover the exact PairKey schedule")
        if self.complete_repairs + self.failures != self.receipt_count:
            raise ValueError("D0 public proof headroom counts do not cover its receipts")
        if self.total_model_calls != self.real_inference_requests_sent:
            raise ValueError("D0 public proof model calls do not reconcile to live requests")
        provenance_payload = {
            "authorization_digest": self.authorization_digest,
            "report_digest": self.report_digest,
            "execution_mode": self.execution_mode,
            "live_inference_status": self.live_inference_status,
            "status": self.status,
            "real_inference_requests_sent": self.real_inference_requests_sent,
            "total_model_calls": self.total_model_calls,
            "unknown_usage_event_count": self.unknown_usage_event_count,
            "unknown_cost_event_count": self.unknown_cost_event_count,
            "total_known_cost_usd": self.total_known_cost_usd,
            "total_estimated_cost_usd": self.total_estimated_cost_usd,
        }
        computed_provenance = evidence_digest(
            {"kind": "repo-repair-d0-live-provenance-v1", **provenance_payload}
        )
        if self.provenance_digest and self.provenance_digest != computed_provenance:
            raise ValueError("D0 public proof provenance digest mismatch")
        if not self.provenance_digest:
            object.__setattr__(self, "provenance_digest", computed_provenance)
        payload = self.model_dump(mode="python", exclude={"proof_digest"})
        computed_proof = evidence_digest(
            {"kind": D0_LIVE_BASELINE_PROOF_SCHEMA_VERSION, **payload}
        )
        if self.proof_digest and self.proof_digest != computed_proof:
            raise ValueError("D0 public proof digest mismatch")
        if not self.proof_digest:
            object.__setattr__(self, "proof_digest", computed_proof)
        assert_no_resolved_credentials(self.model_dump(mode="json"))
        return self


class PairedSearchBudgetProjection(FeasibilityModel):
    task_count: Literal[1] = 1
    structural_candidate_capacity: int = Field(gt=0)
    frozen_candidate_budget: int = Field(gt=0)
    projected_candidate_evaluations: int = Field(gt=0)
    sampling_replicates: int = Field(gt=0)
    projected_paired_outcome_runs: int = Field(gt=0)
    projected_max_model_calls: int = Field(gt=0)
    projected_max_known_cost_usd: float = Field(ge=0.0)
    projected_max_estimated_cost_usd: float = Field(ge=0.0)
    frozen_max_model_calls: int = Field(gt=0)
    frozen_max_known_cost_usd: float = Field(ge=0.0)
    frozen_max_estimated_cost_usd: float = Field(ge=0.0)
    fits_frozen_epoch_budget: bool


class ProviderBaselineDryRun(FeasibilityModel):
    real_provider_baseline_status: Literal["not_run"] = "not_run"
    inference_authorized: Literal[False] = False
    deployment_id: str
    provider: str
    model: str
    provider_config_digest: str
    baseline_protocol_digest: str | None = None
    pair_keys: tuple[PairKey, ...]
    planned_provider_calls: int = Field(gt=0)
    projected_max_known_cost_usd: float = Field(ge=0.0)
    projected_max_estimated_cost_usd: float = Field(ge=0.0)


class DevelopmentTaskFeasibilityManifest(FeasibilityModel):
    schema_version: Literal[FEASIBILITY_SCHEMA_VERSION] = FEASIBILITY_SCHEMA_VERSION
    manifest_id: str
    manifest_digest: str = ""
    epoch_id: str
    epoch_manifest_digest: str
    task_manifest_id: str
    task_manifest_digest: str
    data_state: Literal["development"] = "development"
    permanently_development: Literal[True] = True
    evaluation_contract_id: str
    evaluation_contract_digest: str
    execution_backend_id: str
    execution_backend_digest: str
    controls: tuple[FeasibilityControlResult, ...]
    clean_replay_reproducible: bool
    protected_path_integrity: bool
    leakage_integrity: bool
    identity_integrity: bool
    offline_controls_passed: bool
    baseline_headroom: BaselineHeadroomAssessment
    paired_search_projection: PairedSearchBudgetProjection
    provider_baseline_dry_run: ProviderBaselineDryRun
    status: Literal["pass", "pending_real_provider_baseline", "fail"]
    search_authorized: bool
    reason_codes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def bind_manifest(self) -> "DevelopmentTaskFeasibilityManifest":
        if self.search_authorized != (self.status == "pass"):
            raise ValueError(
                "search_authorized must agree with a passing D0 manifest"
            )
        if self.status == "pass" and (
            not self.offline_controls_passed
            or self.baseline_headroom.status != "has_headroom"
            or not self.paired_search_projection.fits_frozen_epoch_budget
        ):
            raise ValueError(
                "D0 pass requires offline controls, measured headroom, and budget fit"
            )
        payload = self.model_dump(mode="python", exclude={"manifest_digest"})
        computed = evidence_digest({"kind": FEASIBILITY_SCHEMA_VERSION, **payload})
        if self.manifest_digest and self.manifest_digest != computed:
            raise ValueError(
                "feasibility manifest_digest does not match its evidence"
            )
        if not self.manifest_digest:
            object.__setattr__(self, "manifest_digest", computed)
        return self


__all__ = [
    "BaselineHeadroomAssessment",
    "D0_LIVE_BASELINE_PROOF_SCHEMA_VERSION",
    "D0LiveBaselineProof",
    "DevelopmentTaskFeasibilityManifest",
    "FEASIBILITY_SCHEMA_VERSION",
    "FeasibilityControlResult",
    "PairedSearchBudgetProjection",
    "ProviderBaselineDryRun",
    "baseline_headroom_assessment_digest",
    "d0_evaluation_contract_authority_digest",
]
