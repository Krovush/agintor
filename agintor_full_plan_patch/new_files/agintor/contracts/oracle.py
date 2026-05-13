from __future__ import annotations

from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..utils import now_ts, stable_hash
from .evidence import DomainEvidenceContract, EvidenceRef

AuthorityName = Literal["A0", "A1", "A2", "A3", "A4", "A5"]
ValidatorVisibility = Literal["public", "private", "sealed"]


class OracleModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ValidationIntent(OracleModel):
    task_classes: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    user_weights: dict[str, float] = Field(default_factory=dict)
    hard_failures: list[str] = Field(default_factory=list)
    acceptable_tradeoffs: list[str] = Field(default_factory=list)
    authority_floor: AuthorityName | str = "A4"
    unverifiable_residual_policy: Literal["abstain", "human_audit", "diagnostic_only"] = "abstain"


class ClaimSpec(OracleModel):
    claim_id: str
    text: str
    claim_type: Literal[
        "outcome",
        "state",
        "process",
        "safety",
        "factual",
        "semantic",
        "architecture",
        "cost",
    ] = "outcome"
    criticality: Literal["hard", "major", "minor", "diagnostic"] = "major"
    weight: float = 1.0
    minimum_authority: AuthorityName | str = "A4"
    dependencies: list[str] = Field(default_factory=list)
    unverifiable_reason: str = ""


class ClaimGraph(OracleModel):
    graph_id: str
    claims: list[ClaimSpec] = Field(default_factory=list)
    edges: list[dict[str, str]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_claim_graph(self) -> "ClaimGraph":
        claim_ids = [claim.claim_id for claim in self.claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("claim ids must be unique")
        missing = sorted({dep for claim in self.claims for dep in claim.dependencies} - set(claim_ids))
        if missing:
            raise ValueError(f"claims reference missing dependencies: {missing}")
        return self


class ProofObligation(OracleModel):
    obligation_id: str
    claim_ids: list[str]
    description: str
    required_authority: AuthorityName | str = "A4"
    validator_family_hints: list[str] = Field(default_factory=list)
    failure_action: Literal["reject", "abstain", "quarantine", "diagnostic"] = "abstain"


class ValidatorSpec(OracleModel):
    validator_id: str
    family_id: str
    claim_ids: list[str]
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs_schema: dict[str, Any] = Field(default_factory=dict)
    authority_ceiling: AuthorityName | str = "A4"
    visibility: ValidatorVisibility = "sealed"
    independence_group: str = "default"
    leakage_risk: str = "low"
    health_tests: list[str] = Field(default_factory=list)
    failure_action: Literal["reject", "abstain", "quarantine", "diagnostic"] = "abstain"


class OracleTask(OracleModel):
    task_id: str
    public_prompt: str
    public_inputs: dict[str, Any] = Field(default_factory=dict)
    public_fixture_refs: list[EvidenceRef] = Field(default_factory=list)
    sealed_inputs: dict[str, Any] = Field(default_factory=dict)
    sealed_fixture_refs: list[EvidenceRef] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    validator_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class OracleTaskSet(OracleModel):
    task_set_id: str
    partition: Literal["train", "validation", "confirmatory", "heldout", "proxy", "val", "test"] = "train"
    tasks: list[OracleTask] = Field(default_factory=list)
    public: bool = True
    frozen: bool = True


class FixtureBundleRef(OracleModel):
    bundle_id: str
    uri: str = ""
    digest: str = ""
    visibility: ValidatorVisibility = "sealed"
    description: str = ""


class ScoringProjection(OracleModel):
    projection_id: str
    axis_map: dict[str, list[str]] = Field(default_factory=dict)
    weights: dict[str, float] = Field(default_factory=dict)
    promotion_axes: list[str] = Field(default_factory=list)
    efficiency_axes: list[str] = Field(default_factory=list)


class AuthorityPolicy(OracleModel):
    authority_floor: AuthorityName | str = "A4"
    weak_validator_ceiling: AuthorityName | str = "A2"
    require_independent_groups_for_promotion: int = 1
    allow_model_judge_promotion_alone: bool = False
    critical_claim_policy: Literal["all_verified", "weighted", "diagnostic"] = "all_verified"


class LeakagePolicy(OracleModel):
    status_required: bool = True
    forbidden_public_keys: list[str] = Field(default_factory=lambda: [
        "private_expected",
        "private_answer",
        "private_answer_ref",
        "sealed_inputs",
        "sealed_fixture_refs",
        "hidden_tests",
        "promotion_thresholds",
        "private_rubric",
    ])
    sealed_validator_visibility: bool = True
    runtime_visible_projection_required: bool = True


class AbstentionPolicy(OracleModel):
    insufficient_authority_action: Literal["abstain", "human_audit", "diagnostic_only"] = "abstain"
    missing_critical_validator_action: Literal["abstain", "quarantine"] = "abstain"
    invalid_package_action: Literal["quarantine", "abstain"] = "quarantine"
    min_evidence_count: int = 1


class OraclePackage(OracleModel):
    package_id: str
    oracle_family_id: str
    package_hash: str = ""
    goal_id: str
    runtime_spec_digest: str = ""
    validation_intent: ValidationIntent
    claim_graph: ClaimGraph
    proof_obligations: list[ProofObligation] = Field(default_factory=list)
    validator_specs: list[ValidatorSpec] = Field(default_factory=list)
    task_sets: list[OracleTaskSet] = Field(default_factory=list)
    fixture_bundle_refs: list[FixtureBundleRef] = Field(default_factory=list)
    evidence_contract: DomainEvidenceContract
    scoring_projection: ScoringProjection
    authority_policy: AuthorityPolicy = Field(default_factory=AuthorityPolicy)
    leakage_policy: LeakagePolicy = Field(default_factory=LeakagePolicy)
    abstention_policy: AbstentionPolicy = Field(default_factory=AbstentionPolicy)
    qa_report_ref: str = ""
    public_view_hash: str = ""
    sealed_view_hash: str = ""
    frozen: bool = True
    created_at: float = Field(default_factory=now_ts)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_oracle_package(self) -> "OraclePackage":
        claim_ids = {claim.claim_id for claim in self.claim_graph.claims}
        validator_ids = [validator.validator_id for validator in self.validator_specs]
        if len(validator_ids) != len(set(validator_ids)):
            raise ValueError("validator ids must be unique")
        missing_validator_claims = sorted(
            {claim_id for validator in self.validator_specs for claim_id in validator.claim_ids} - claim_ids
        )
        if missing_validator_claims:
            raise ValueError(f"validators reference missing claims: {missing_validator_claims}")
        obligation_claims = sorted(
            {claim_id for obligation in self.proof_obligations for claim_id in obligation.claim_ids} - claim_ids
        )
        if obligation_claims:
            raise ValueError(f"proof obligations reference missing claims: {obligation_claims}")
        return self


class ValidatorResult(OracleModel):
    validator_id: str
    claim_ids: list[str]
    status: Literal["pass", "fail", "error", "abstain"]
    authority_used: AuthorityName | str = "A0"
    health_status: dict[str, Any] = Field(default_factory=dict)
    observations: dict[str, Any] = Field(default_factory=dict)
    evidence_digest: str = ""
    created_at: float = Field(default_factory=now_ts)

    @model_validator(mode="after")
    def fill_digest(self) -> "ValidatorResult":
        if not self.evidence_digest:
            self.evidence_digest = stable_hash(self.model_dump(mode="json", exclude_none=True))
        return self


class ClaimResult(OracleModel):
    claim_id: str
    satisfied: bool | None = None
    posterior_lower: float | None = None
    posterior_upper: float | None = None
    authority_mass: dict[str, float] = Field(default_factory=dict)
    coverage: float = 0.0
    residual_unverified: str = ""
    validator_result_refs: list[str] = Field(default_factory=list)


class OracleEvaluationSummary(OracleModel):
    package_id: str
    package_hash: str
    runtime_hash: str = ""
    runtime_spec_digest: str = ""
    task_ids: list[str] = Field(default_factory=list)
    validator_results: list[ValidatorResult] = Field(default_factory=list)
    claim_results: list[ClaimResult] = Field(default_factory=list)
    evidence_digest: str = ""
    critical_claims_verified: bool = False
    invalid_reason: str = ""

    @model_validator(mode="after")
    def fill_summary_digest(self) -> "OracleEvaluationSummary":
        if not self.evidence_digest:
            self.evidence_digest = stable_hash(self.model_dump(mode="json", exclude_none=True))
        return self


def oracle_package_identity_payload(package: OraclePackage | Mapping[str, Any]) -> dict[str, Any]:
    payload = package.model_dump(mode="json", exclude_none=True) if isinstance(package, OraclePackage) else dict(package)
    payload.pop("package_hash", None)
    payload.pop("public_view_hash", None)
    payload.pop("sealed_view_hash", None)
    return payload


__all__ = [
    "AbstentionPolicy",
    "AuthorityPolicy",
    "ClaimGraph",
    "ClaimResult",
    "ClaimSpec",
    "FixtureBundleRef",
    "LeakagePolicy",
    "OracleEvaluationSummary",
    "OraclePackage",
    "OracleTask",
    "OracleTaskSet",
    "ProofObligation",
    "ScoringProjection",
    "ValidationIntent",
    "ValidatorResult",
    "ValidatorSpec",
    "oracle_package_identity_payload",
]
