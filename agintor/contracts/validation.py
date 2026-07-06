from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..utils import now_ts, stable_hash
from .evidence import AuthorityLevel


ValidationAuthority = AuthorityLevel | str
ClaimCriticality = Literal["hard", "major", "minor", "diagnostic"]
ClaimType = Literal["outcome", "state", "process", "safety", "factual", "semantic", "architecture", "cost"]
EvidenceStatus = Literal["pass", "fail", "score", "abstain", "contradiction", "error", "quarantine"]

_HASH_EXCLUDE_KEYS = {
    "created_at",
    "completed_at",
    "updated_at",
    "metadata",
    "report_id",
    "ledger_id",
    "comparison_id",
    "signal_id",
    "budget_id",
    "evidence_digest",
}


class ValidationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


def _as_plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    return value


def _hash_payload(value: Any) -> Any:
    value = _as_plain(value)
    if isinstance(value, Mapping):
        return {
            str(key): _hash_payload(item)
            for key, item in sorted(value.items())
            if str(key) not in _HASH_EXCLUDE_KEYS
        }
    if isinstance(value, list):
        return [_hash_payload(item) for item in value]
    return value


def _authority_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _promotion_rank(value: Any) -> int:
    text = _authority_value(value)
    return int(text[1:]) if len(text) == 2 and text.startswith("M") and text[1:].isdigit() else 0


class ValidationClaim(ValidationModel):
    claim_id: str
    text: str = ""
    claim_type: ClaimType | str = "outcome"
    criticality: ClaimCriticality | str = "major"
    weight: float = 1.0
    authority_floor: ValidationAuthority = AuthorityLevel.PRIVATE_ORACLE
    observability: Literal["observable", "partially_observable", "unobservable"] = "observable"
    scope: dict[str, Any] = Field(default_factory=dict)
    dependencies: list[str] = Field(default_factory=list)
    proof_obligation_ids: list[str] = Field(default_factory=list)
    validator_ids: list[str] = Field(default_factory=list)
    residual_reason: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProofObligation(ValidationModel):
    obligation_id: str
    claim_ids: list[str] = Field(default_factory=list)
    description: str = ""
    required_validator_families: list[str] = Field(default_factory=list)
    validator_ids: list[str] = Field(default_factory=list)
    minimum_authority: ValidationAuthority = AuthorityLevel.PRIVATE_ORACLE
    failure_action: Literal["reject", "abstain", "quarantine", "diagnostic"] = "abstain"
    residual_reason: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ValidatorHealth(ValidationModel):
    validator_id: str = ""
    family_id: str = ""
    status: Literal["unknown", "pass", "fail", "diagnostic"] = "unknown"
    nonvacuity: float = 0.0
    sensitivity: float = 0.0
    specificity: float = 0.0
    coverage: float = 0.0
    reproducibility: float = 0.0
    calibration: float = 0.0
    independence: float = 0.0
    leakage_resistance: float = 0.0
    architecture_neutrality: float = 0.0
    cost_fairness: float = 0.0
    controls: dict[str, Any] = Field(default_factory=dict)
    created_at: float = Field(default_factory=now_ts)

    @property
    def score(self) -> float:
        channels = [
            self.nonvacuity,
            self.sensitivity,
            self.specificity,
            self.coverage,
            self.reproducibility,
            self.calibration,
            self.independence,
            self.leakage_resistance,
            self.architecture_neutrality,
            self.cost_fairness,
        ]
        return float(min(channels)) if channels else 0.0


class ValidatorReport(ValidationModel):
    report_id: str = ""
    validator_id: str
    family_id: str = ""
    claim_ids: list[str] = Field(default_factory=list)
    status: EvidenceStatus = "abstain"
    score: float | None = None
    interval_lower: float | None = None
    interval_upper: float | None = None
    authority_used: ValidationAuthority = AuthorityLevel.NONE
    authority_ceiling: ValidationAuthority = AuthorityLevel.NONE
    health: ValidatorHealth = Field(default_factory=ValidatorHealth)
    coverage: float = 0.0
    independence_group: str = "default"
    leakage_flags: list[str] = Field(default_factory=list)
    observations: dict[str, Any] = Field(default_factory=dict)
    evidence_digest: str = ""
    created_at: float = Field(default_factory=now_ts)

    @model_validator(mode="after")
    def fill_identity(self) -> "ValidatorReport":
        if not self.report_id:
            self.report_id = stable_hash(
                "agintor.validation.validator_report",
                self.validator_id,
                self.family_id,
                self.claim_ids,
                self.status,
                self.score,
                self.interval_lower,
                self.interval_upper,
                self.authority_used,
                self.authority_ceiling,
                self.coverage,
                self.independence_group,
                self.leakage_flags,
                self.observations,
            )[:24]
        if not self.evidence_digest:
            self.evidence_digest = stable_hash(
                "agintor.validation.validator_report.digest",
                _hash_payload(self.model_dump(mode="json", exclude_none=True)),
            )
        return self


class ClaimPosterior(ValidationModel):
    claim_id: str
    state: Literal["satisfied", "failed", "uncertain", "abstained", "quarantined", "unverifiable"] = "uncertain"
    posterior_lower: float | None = None
    posterior_upper: float | None = None
    authority_mass: dict[str, float] = Field(default_factory=dict)
    coverage: float = 0.0
    residual_mass: float = 0.0
    residual_reason: str = ""
    validator_report_ids: list[str] = Field(default_factory=list)
    evidence_digest: str = ""

    @model_validator(mode="after")
    def fill_digest(self) -> "ClaimPosterior":
        if not self.evidence_digest:
            self.evidence_digest = stable_hash(
                "agintor.validation.claim_posterior",
                self.claim_id,
                self.state,
                self.posterior_lower,
                self.posterior_upper,
                self.authority_mass,
                self.coverage,
                self.residual_mass,
                self.residual_reason,
                self.validator_report_ids,
            )
        return self


class ValidationPlan(ValidationModel):
    plan_id: str
    goal_id: str = ""
    oracle_package_id: str = ""
    oracle_package_hash: str = ""
    oracle_family_id: str = ""
    runtime_spec_digest: str = ""
    public_projection_hash: str = ""
    sealed_projection_hash: str = ""
    validator_bundle_hash: str = ""
    fixture_digests: dict[str, str] = Field(default_factory=dict)
    claims: list[ValidationClaim] = Field(default_factory=list)
    proof_obligations: list[ProofObligation] = Field(default_factory=list)
    validator_ids: list[str] = Field(default_factory=list)
    task_ids: list[str] = Field(default_factory=list)
    residuals: dict[str, str] = Field(default_factory=dict)
    promotion_policy: dict[str, Any] = Field(default_factory=dict)
    abstention_policy: dict[str, Any] = Field(default_factory=dict)
    leakage_policy: dict[str, Any] = Field(default_factory=dict)
    created_at: float = Field(default_factory=now_ts)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_coverage(self) -> "ValidationPlan":
        obligation_claim_ids = {claim_id for obligation in self.proof_obligations for claim_id in obligation.claim_ids}
        for claim in self.claims:
            if claim.claim_id in obligation_claim_ids or claim.residual_reason or self.residuals.get(claim.claim_id):
                continue
            raise ValueError(f"validation claim {claim.claim_id!r} needs a proof obligation or residual")
        return self


class EvidenceLedger(ValidationModel):
    ledger_id: str = ""
    oracle_package_hash: str = ""
    validation_plan_hash: str = ""
    public_projection_hash: str = ""
    sealed_projection_hash: str = ""
    runtime_hash: str = ""
    task_id: str = ""
    run_id: str = ""
    seed: int | None = None
    claim_manifest_digest: str = ""
    validator_reports: list[ValidatorReport] = Field(default_factory=list)
    claim_posteriors: list[ClaimPosterior] = Field(default_factory=list)
    authority_mass: dict[str, float] = Field(default_factory=dict)
    coverage: dict[str, float] = Field(default_factory=dict)
    independence_partition: dict[str, list[str]] = Field(default_factory=dict)
    leakage_attestation: dict[str, Any] = Field(default_factory=dict)
    process_violations: list[str] = Field(default_factory=list)
    side_effect_violations: list[str] = Field(default_factory=list)
    unverifiable_residual: dict[str, Any] = Field(default_factory=dict)
    audit_status: Literal["pass", "fail", "abstain", "quarantine", "diagnostic"] = "diagnostic"
    scalar_score: float | None = None
    scalar_score_authority: ValidationAuthority = AuthorityLevel.PROMOTION_NONE
    promotion_authoritative: bool = False
    evidence_digest: str = ""
    created_at: float = Field(default_factory=now_ts)

    @model_validator(mode="after")
    def validate_authority(self) -> "EvidenceLedger":
        if self.promotion_authoritative and not self.validator_reports:
            raise ValueError("promotion-authoritative scalar scores require validator reports")
        if self.scalar_score is not None and _promotion_rank(self.scalar_score_authority) >= 2 and not self.validator_reports:
            raise ValueError("M2+ scalar score authority requires validator reports")
        if not self.ledger_id:
            self.ledger_id = stable_hash(
                "agintor.validation.ledger",
                self.oracle_package_hash,
                self.validation_plan_hash,
                self.runtime_hash,
                self.task_id,
                self.run_id,
                self.seed,
                [report.report_id for report in self.validator_reports],
                [posterior.claim_id for posterior in self.claim_posteriors],
            )[:24]
        if not self.evidence_digest:
            self.evidence_digest = stable_hash(
                "agintor.validation.ledger.digest",
                _hash_payload(self.model_dump(mode="json", exclude_none=True)),
            )
        return self


class ComparisonRecord(ValidationModel):
    comparison_id: str = ""
    parent_runtime_hash: str = ""
    child_runtime_hash: str = ""
    oracle_package_hash: str = ""
    validation_plan_hash: str = ""
    comparison_design_id: str = ""
    task_ids: list[str] = Field(default_factory=list)
    ledger_ids: list[str] = Field(default_factory=list)
    axis: str = "capability"
    decision: Literal["promote", "reject", "continue", "abstain", "quarantine"] = "continue"
    authority_profile: dict[str, float] = Field(default_factory=dict)
    effect_interval: dict[str, float] = Field(default_factory=dict)
    protected_regressions: dict[str, Any] = Field(default_factory=dict)
    alpha_spent: float = 0.0
    reason_codes: list[str] = Field(default_factory=list)
    evidence_digest: str = ""
    created_at: float = Field(default_factory=now_ts)

    @model_validator(mode="after")
    def fill_identity(self) -> "ComparisonRecord":
        if not self.comparison_id:
            self.comparison_id = stable_hash(
                "agintor.validation.comparison",
                self.parent_runtime_hash,
                self.child_runtime_hash,
                self.oracle_package_hash,
                self.validation_plan_hash,
                self.comparison_design_id,
                self.task_ids,
                self.ledger_ids,
                self.axis,
            )[:24]
        if not self.evidence_digest:
            self.evidence_digest = stable_hash(
                "agintor.validation.comparison.digest",
                _hash_payload(self.model_dump(mode="json", exclude_none=True)),
            )
        return self


class ArchitectureSignal(ValidationModel):
    signal_id: str = ""
    parent_runtime_hash: str = ""
    child_runtime_hash: str = ""
    oracle_package_hash: str = ""
    validation_plan_hash: str = ""
    comparison_design_id: str = ""
    axis: str = "capability"
    decision: Literal["promote", "reject", "continue", "abstain", "quarantine"] = "continue"
    authority_profile: dict[str, float] = Field(default_factory=dict)
    effect_interval: dict[str, float] = Field(default_factory=dict)
    protected_regressions: dict[str, Any] = Field(default_factory=dict)
    mutation_actions: list[str] = Field(default_factory=list)
    component_effects: dict[str, dict[str, float]] = Field(default_factory=dict)
    confounds: dict[str, list[str]] = Field(default_factory=dict)
    allowed_updates: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    evidence_digest: str = ""
    created_at: float = Field(default_factory=now_ts)

    @model_validator(mode="after")
    def fill_identity(self) -> "ArchitectureSignal":
        if not self.signal_id:
            self.signal_id = stable_hash(
                "agintor.validation.architecture_signal",
                self.parent_runtime_hash,
                self.child_runtime_hash,
                self.oracle_package_hash,
                self.validation_plan_hash,
                self.comparison_design_id,
                self.axis,
                self.decision,
                self.mutation_actions,
                self.allowed_updates,
            )[:24]
        if not self.evidence_digest:
            self.evidence_digest = stable_hash(
                "agintor.validation.architecture_signal.digest",
                _hash_payload(self.model_dump(mode="json", exclude_none=True)),
            )
        return self


class AlphaBudget(ValidationModel):
    budget_id: str = ""
    factory_chat_id: str = ""
    validation_plan_hash: str = ""
    alpha_global: float = 0.05
    alpha_spent: float = 0.0
    alpha_wealth: float = 0.05
    allocations: dict[str, float] = Field(default_factory=dict)
    closed: bool = False
    created_at: float = Field(default_factory=now_ts)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_budget(self) -> "AlphaBudget":
        allocated = sum(max(0.0, float(value)) for value in self.allocations.values())
        if self.alpha_spent < 0.0 or self.alpha_global < 0.0:
            raise ValueError("alpha budget values must be non-negative")
        if self.alpha_spent > self.alpha_global + 1e-12:
            raise ValueError("alpha_spent may not exceed alpha_global")
        if allocated > self.alpha_global + 1e-12:
            raise ValueError("alpha allocations may not exceed alpha_global")
        if not self.budget_id:
            self.budget_id = stable_hash(
                "agintor.validation.alpha_budget",
                self.factory_chat_id,
                self.validation_plan_hash,
                self.alpha_global,
                self.allocations,
            )[:24]
        return self

    @property
    def remaining_alpha(self) -> float:
        return max(0.0, float(self.alpha_global) - float(self.alpha_spent))


def validator_bundle_hash(validators: Sequence[Any]) -> str:
    rows = [
        _hash_payload(_as_plain(validator))
        for validator in validators
    ]
    return stable_hash("agintor.validation.validator_bundle", rows)


def validation_plan_hash(plan: ValidationPlan | Mapping[str, Any]) -> str:
    payload = plan.model_dump(mode="json", exclude_none=True) if isinstance(plan, ValidationPlan) else dict(plan)
    return stable_hash("agintor.validation.plan", _hash_payload(payload))


def validation_plan_from_oracle_package(package: Any) -> ValidationPlan:
    claim_to_validator_ids: dict[str, list[str]] = {}
    claim_to_validator_families: dict[str, set[str]] = {}
    for validator in getattr(package, "validator_specs", []) or []:
        for claim_id in getattr(validator, "claim_ids", []) or []:
            claim_to_validator_ids.setdefault(str(claim_id), []).append(str(getattr(validator, "validator_id", "")))
            claim_to_validator_families.setdefault(str(claim_id), set()).add(str(getattr(validator, "family_id", "")))

    obligation_by_claim: dict[str, list[str]] = {}
    proof_obligations: list[ProofObligation] = []
    for obligation in getattr(package, "proof_obligations", []) or []:
        proof = obligation if isinstance(obligation, ProofObligation) else ProofObligation.model_validate(_as_plain(obligation))
        proof_obligations.append(proof)
        for claim_id in proof.claim_ids:
            obligation_by_claim.setdefault(str(claim_id), []).append(proof.obligation_id)

    residuals: dict[str, str] = {}
    claims: list[ValidationClaim] = []
    for claim in getattr(getattr(package, "claim_graph", None), "claims", []) or []:
        claim_id = str(getattr(claim, "claim_id", ""))
        residual_reason = str(getattr(claim, "unverifiable_reason", "") or "")
        proof_ids = sorted(obligation_by_claim.get(claim_id, []))
        validator_ids = sorted(item for item in claim_to_validator_ids.get(claim_id, []) if item)
        if not proof_ids and validator_ids:
            proof = ProofObligation(
                obligation_id=f"obligation.{claim_id}",
                claim_ids=[claim_id],
                description=f"Validate claim: {str(getattr(claim, 'text', '') or '')}",
                required_validator_families=sorted(
                    family_id
                    for family_id in claim_to_validator_families.get(claim_id, set())
                    if family_id
                ),
                validator_ids=validator_ids,
                minimum_authority=getattr(claim, "minimum_authority", AuthorityLevel.PRIVATE_ORACLE),
                failure_action="reject" if str(getattr(claim, "criticality", "")) == "hard" else "abstain",
            )
            proof_obligations.append(proof)
            proof_ids = [proof.obligation_id]
        if not proof_ids and not residual_reason:
            residual_reason = "missing_proof_obligation"
        if residual_reason:
            residuals[claim_id] = residual_reason
        claims.append(
            ValidationClaim(
                claim_id=claim_id,
                text=str(getattr(claim, "text", "") or ""),
                claim_type=str(getattr(claim, "claim_type", "outcome") or "outcome"),
                criticality=str(getattr(claim, "criticality", "major") or "major"),
                weight=float(getattr(claim, "weight", 1.0) or 0.0),
                authority_floor=getattr(claim, "minimum_authority", AuthorityLevel.PRIVATE_ORACLE),
                observability="unobservable" if residual_reason else "observable",
                dependencies=[str(item) for item in getattr(claim, "dependencies", []) or []],
                proof_obligation_ids=proof_ids,
                validator_ids=validator_ids,
                residual_reason=residual_reason,
                metadata=dict(getattr(claim, "metadata", {}) or {}),
            )
        )

    task_ids: list[str] = []
    fixture_digests: dict[str, str] = {}
    for task_set in getattr(package, "task_sets", []) or []:
        for task in getattr(task_set, "tasks", []) or []:
            task_id = str(getattr(task, "task_id", "") or "")
            if task_id:
                task_ids.append(task_id)
            digest = str(getattr(task, "sealed_payload_digest", "") or "")
            if task_id and digest:
                fixture_digests[task_id] = digest
    for ref in getattr(package, "fixture_bundle_refs", []) or []:
        ref_id = str(getattr(ref, "ref_id", "") or "")
        digest = str(getattr(ref, "digest", "") or "")
        if ref_id and digest:
            fixture_digests[ref_id] = digest

    validators = list(getattr(package, "validator_specs", []) or [])
    return ValidationPlan(
        plan_id=f"validation-plan.{stable_hash(getattr(package, 'package_id', ''), getattr(package, 'goal_id', ''), [claim.claim_id for claim in claims])[:16]}",
        goal_id=str(getattr(package, "goal_id", "") or ""),
        oracle_package_id=str(getattr(package, "package_id", "") or ""),
        oracle_package_hash=str(getattr(package, "package_hash", "") or ""),
        oracle_family_id=str(getattr(package, "oracle_family_id", "") or ""),
        runtime_spec_digest=str(getattr(package, "runtime_spec_digest", "") or ""),
        public_projection_hash=str(getattr(package, "public_view_hash", "") or ""),
        sealed_projection_hash=str(getattr(package, "sealed_view_hash", "") or ""),
        validator_bundle_hash=validator_bundle_hash(validators),
        fixture_digests=dict(sorted(fixture_digests.items())),
        claims=claims,
        proof_obligations=proof_obligations,
        validator_ids=sorted(str(getattr(validator, "validator_id", "") or "") for validator in validators),
        task_ids=sorted(set(task_ids)),
        residuals=residuals,
        promotion_policy={"source": "oracle_package", "scalar_scores_require_ledger": True},
        abstention_policy=_as_plain(getattr(package, "abstention_policy", {})) or {},
        leakage_policy=_as_plain(getattr(package, "leakage_policy", {})) or {},
    )


__all__ = [
    "AlphaBudget",
    "ArchitectureSignal",
    "ClaimPosterior",
    "ComparisonRecord",
    "EvidenceLedger",
    "EvidenceStatus",
    "ProofObligation",
    "ValidationAuthority",
    "ValidationClaim",
    "ValidationModel",
    "ValidationPlan",
    "ValidatorHealth",
    "ValidatorReport",
    "validation_plan_from_oracle_package",
    "validation_plan_hash",
    "validator_bundle_hash",
]
