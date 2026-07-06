from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..utils import now_ts


class EvidenceModel(BaseModel):
    model_config = ConfigDict(use_enum_values=True)


class DomainKind(str, Enum):
    GENERATED_TOOL_WORKFLOW = "generated_tool_workflow"
    STRUCTURED_MEMORY_RETRIEVAL = "structured_memory_retrieval"
    STATEFUL_SERVICE = "stateful_service"
    REPO_PATCH = "repo_patch"
    PREFERENCE = "preference"


class AuthorityLevel(str, Enum):
    NONE = "A0"
    HEURISTIC = "A1"
    PREFERENCE = "A2"
    TRACE_OR_ARTIFACT = "A3"
    PRIVATE_ORACLE = "A4"
    HUMAN_AUDITED = "A5"
    FORMAL_OR_CERTIFIED_PROOF = "A6"
    PROMOTION_NONE = "M0"
    PROMOTION_EXPLORATION = "M1"
    PROMOTION_WEAK_PREFERENCE = "M2"
    PROMOTION_GROUNDED_SUBSKILL = "M3"
    PROMOTION_LOCAL_CAPABILITY = "M4"
    PROMOTION_SEALED_CAPABILITY = "M5"
    PROMOTION_CERTIFIED_INVARIANT = "M6"


class PromotionDecisionType(str, Enum):
    CAPABILITY = "capability"
    EFFICIENCY = "efficiency"
    PREFERENCE = "preference"
    SUBSKILL = "subskill"
    REJECT = "reject"
    ABSTAIN = "abstain"
    QUARANTINE = "quarantine"
    NO_PROGRESS = "no_progress"


class OptimizerUpdate(str, Enum):
    CAPABILITY_ARCHIVE = "capability_archive"
    CAPABILITY_SCHEDULER = "capability_scheduler"
    CAPABILITY_PREDICTORS = "capability_predictors"
    CAPABILITY_PRIORS = "capability_priors"
    EFFICIENCY_ARCHIVE = "efficiency_archive"
    EFFICIENCY_PREDICTORS = "efficiency_predictors"
    SUBSKILL_ARCHIVE = "subskill_archive"
    SUBSKILL_SCHEDULER = "subskill_scheduler"
    SUBSKILL_PREDICTORS = "subskill_predictors"
    PREFERENCE_ARCHIVE = "preference_archive"
    PREFERENCE_MODEL = "preference_model"
    DIAGNOSTIC_LOG = "diagnostic_log"
    DIAGNOSTIC_PREDICTORS = "diagnostic_predictors"
    INSTRUMENT_IMPROVEMENT_QUEUE = "instrument_improvement_queue"
    HARD_FAILURE_STATS = "hard_failure_stats"


CAPABILITY_UPDATES = {
    OptimizerUpdate.CAPABILITY_ARCHIVE.value,
    OptimizerUpdate.CAPABILITY_SCHEDULER.value,
    OptimizerUpdate.CAPABILITY_PREDICTORS.value,
    OptimizerUpdate.CAPABILITY_PRIORS.value,
}

PROMOTING_UPDATES = {
    OptimizerUpdate.CAPABILITY_ARCHIVE.value,
    OptimizerUpdate.CAPABILITY_SCHEDULER.value,
    OptimizerUpdate.CAPABILITY_PREDICTORS.value,
    OptimizerUpdate.CAPABILITY_PRIORS.value,
    OptimizerUpdate.EFFICIENCY_ARCHIVE.value,
    OptimizerUpdate.EFFICIENCY_PREDICTORS.value,
    OptimizerUpdate.SUBSKILL_ARCHIVE.value,
    OptimizerUpdate.SUBSKILL_SCHEDULER.value,
    OptimizerUpdate.SUBSKILL_PREDICTORS.value,
    OptimizerUpdate.PREFERENCE_ARCHIVE.value,
    OptimizerUpdate.PREFERENCE_MODEL.value,
}


def _value(value: Any) -> str:
    return value.value if isinstance(value, Enum) else str(value)


def decision_attr(decision: Any, name: str, default: Any = None) -> Any:
    if decision is None:
        return default
    if isinstance(decision, Mapping):
        return decision.get(name, default)
    return getattr(decision, name, default)


def decision_value(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(getattr(value, "value", value))


def decision_field_value(decision: Any, name: str, default: str = "") -> str:
    return decision_value(decision_attr(decision, name, default), default)


def decision_type_value(decision: Any) -> str | None:
    value = decision_attr(decision, "decision_type")
    return None if value is None else decision_value(value)


class EvidenceRef(EvidenceModel):
    ref_id: str
    uri: str = ""
    digest: str = ""
    visibility: Literal["public", "private", "sealed", "aggregate"] = "public"
    description: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvidenceScope(EvidenceModel):
    domain: str = ""
    domain_name: str = ""
    slice_tags: list[str] = Field(default_factory=list)
    axis_ids: list[str] = Field(default_factory=list)
    claim: str = ""
    allowed_claim_language: list[str] | str = Field(default_factory=list)
    forbidden_claim_language: list[str] = Field(default_factory=list)


class QualityAxisSpec(EvidenceModel):
    axis_id: str
    description: str = ""
    weight: float = 1.0
    promotion_kind: Literal["capability", "preference", "subskill"] = "capability"
    comparator_type: Literal[
        "exact_outcome",
        "field_vector",
        "hidden_challenge",
        "metamorphic",
        "pairwise_preference",
        "defect_search",
    ] = "exact_outcome"
    minimum_authority: AuthorityLevel | str = AuthorityLevel.PRIVATE_ORACLE
    epsilon: float = 0.0
    protected_regression_tolerance: float = 0.0
    promotion_eligible: bool = True
    tie_margin: float = 0.02
    metadata: dict[str, Any] = Field(default_factory=dict)


class EfficiencyAxisSpec(EvidenceModel):
    axis_id: str
    promotion_kind: Literal["efficiency"] = "efficiency"
    comparator_type: str = "exact_outcome"
    metric: Literal["cost", "latency", "tokens", "tool_calls", "retries", "faults"] = "cost"
    minimum_authority: AuthorityLevel | str = AuthorityLevel.PRIVATE_ORACLE
    epsilon: float = 0.0
    lower_is_better: bool = True
    weight: float = 1.0
    equivalence_tolerance: float = 0.0


class DomainEvidenceContract(EvidenceModel):
    contract_id: str
    domain_kind: DomainKind | str
    version: str
    scope: EvidenceScope | dict[str, Any]
    challenge_distribution: dict[str, Any] = Field(default_factory=dict)
    answer_mechanism: dict[str, Any] = Field(default_factory=dict)
    quality_axes: list[QualityAxisSpec | dict[str, Any]] = Field(default_factory=list)
    efficiency_axes: list[EfficiencyAxisSpec | dict[str, Any]] = Field(default_factory=list)
    health_floors: dict[str, Any] = Field(default_factory=dict)
    statistical_rule: dict[str, Any] = Field(default_factory=dict)
    leakage_policy: dict[str, Any] = Field(default_factory=dict)
    feedback_policy: dict[str, Any] = Field(default_factory=dict)
    artifact_refs: dict[str, Any] | list[EvidenceRef] = Field(default_factory=dict)
    frozen: bool = True
    created_at: float = Field(default_factory=now_ts)

    @model_validator(mode="after")
    def validate_axes(self) -> "DomainEvidenceContract":
        if not self.quality_axes and not self.efficiency_axes:
            raise ValueError("domain evidence contracts require at least one quality or efficiency axis")
        allowed_quality_promotion_kinds = {"capability", "preference", "subskill"}
        for axis in self.quality_axes:
            raw = dict(axis) if isinstance(axis, Mapping) else {}
            promotion_kind = str(getattr(axis, "promotion_kind", raw.get("promotion_kind", "capability")))
            if promotion_kind not in allowed_quality_promotion_kinds:
                raise ValueError(f"unsupported quality promotion_kind {promotion_kind!r}")
        axis_ids = [
            str(axis.axis_id if isinstance(axis, (QualityAxisSpec, EfficiencyAxisSpec)) else axis.get("axis_id", ""))
            for axis in [*self.quality_axes, *self.efficiency_axes]
        ]
        axis_ids = [axis_id for axis_id in axis_ids if axis_id]
        if len(axis_ids) != len(set(axis_ids)):
            raise ValueError("domain evidence contract axis ids must be unique")
        return self


class ChallengeGeneratorVersion(EvidenceModel):
    generator_id: str
    domain_kind: DomainKind | str
    version: str
    template_state: Literal["design", "train", "validation", "confirmatory", "retired"] = "train"
    difficulty_parameters: dict[str, Any] = Field(default_factory=dict)
    slice_coverage: dict[str, float] = Field(default_factory=dict)
    private_answer_available: bool = False
    health_report_ref: EvidenceRef | None = None
    realism_report_ref: EvidenceRef | None = None
    retirement_reason: str = ""


class ChallengeInstance(EvidenceModel):
    challenge_id: str
    contract_id: str
    generator_id: str
    partition: Literal["explore", "train", "validation", "confirmatory", "heldout", "val", "test", "proxy"]
    domain_kind: DomainKind | str
    slice_tags: list[str] = Field(default_factory=list)
    difficulty_vector: dict[str, float] = Field(default_factory=dict)
    public_prompt: str = ""
    public_fixture_refs: list[EvidenceRef] = Field(default_factory=list)
    private_answer_ref: EvidenceRef | None = None
    metamorphic_relation_refs: list[EvidenceRef] = Field(default_factory=list)
    validator_refs: list[EvidenceRef] = Field(default_factory=list)
    contamination_flags: list[str] = Field(default_factory=list)
    template_lineage: list[str] = Field(default_factory=list)


class OutcomeAxisScore(EvidenceModel):
    axis_id: str
    score: float
    authority: AuthorityLevel | str = AuthorityLevel.NONE
    evidence_ref: str = ""
    evidence_digest: str = ""


class DefectReport(EvidenceModel):
    artifact_id: str = ""
    defect_type: str
    severity: float = 0.0
    claim_ref: str = ""
    verifier_status: Literal["verified", "unverified", "false_alarm"] = "unverified"


class OutcomeVector(EvidenceModel):
    task_id: str
    run_id: str = ""
    runtime_hash: str
    hard_gates: dict[str, bool] = Field(default_factory=dict)
    axes: list[OutcomeAxisScore] = Field(default_factory=list)
    defects: list[DefectReport] = Field(default_factory=list)


class EvidenceRecord(EvidenceModel):
    record_id: str
    contract_id: str
    challenge_id: str
    candidate_runtime_hash: str
    oracle_package_hash: str = ""
    runtime_spec_digest: str = ""
    oracle_public_view_hash: str = ""
    oracle_sealed_view_hash: str = ""
    validation_plan_hash: str = ""
    validator_results: list[dict[str, Any]] = Field(default_factory=list)
    claim_results: list[dict[str, Any]] = Field(default_factory=list)
    validator_reports: list[dict[str, Any]] = Field(default_factory=list)
    evidence_ledger: dict[str, Any] = Field(default_factory=dict)
    parent_runtime_hash: str = ""
    run_ref: str = ""
    attempt_ref: str = ""
    checkpoint_refs: list[str] = Field(default_factory=list)
    trace_refs: list[str] = Field(default_factory=list)
    artifact_ref: str = ""
    axis_scores: list[OutcomeAxisScore] = Field(default_factory=list)
    efficiency_scores: dict[str, float] = Field(default_factory=dict)
    verifier_evidence: list[dict[str, Any]] = Field(default_factory=list)
    defect_evidence: list[DefectReport] = Field(default_factory=list)
    metamorphic_evidence: list[EvidenceRef] = Field(default_factory=list)
    authority_level: AuthorityLevel | str = AuthorityLevel.NONE
    invalid_reason: str = ""
    evidence_digest: str = ""
    created_at: float = Field(default_factory=now_ts)


class AxisDelta(EvidenceModel):
    axis_id: str
    promotion_kind: Literal["capability", "efficiency", "preference", "subskill"] = "capability"
    estimate: float = 0.0
    lower: float = 0.0
    upper: float = 0.0
    evidence_count: int = 0
    authority_level: AuthorityLevel | str = AuthorityLevel.NONE
    source: str = "exact_verifier"
    saturated: bool = False
    reason_codes: list[str] = Field(default_factory=list)

    @property
    def delta_estimate(self) -> float:
        return self.estimate

    @property
    def delta_lower(self) -> float:
        return self.lower

    @property
    def delta_upper(self) -> float:
        return self.upper


class EfficiencyDelta(EvidenceModel):
    axis_id: str
    estimate: float = 0.0
    lower: float = 0.0
    upper: float = 0.0
    promotion_kind: Literal["efficiency"] = "efficiency"
    authority_level: AuthorityLevel | str = AuthorityLevel.NONE


class PairedComparison(EvidenceModel):
    comparison_id: str
    parent_runtime_hash: str
    child_runtime_hash: str
    contract_id: str = ""
    oracle_package_hash: str = ""
    parent_oracle_package_hash: str = ""
    child_oracle_package_hash: str = ""
    parent_runtime_spec_digest: str = ""
    child_runtime_spec_digest: str = ""
    challenge_ids: list[str] = Field(default_factory=list)
    axis_deltas: dict[str, AxisDelta | dict[str, Any]] = Field(default_factory=dict)
    axis_task_ids: dict[str, list[str]] = Field(default_factory=dict)
    protected_axis_bounds: dict[str, float] = Field(default_factory=dict)
    efficiency_deltas: dict[str, EfficiencyDelta | dict[str, Any]] = Field(default_factory=dict)
    confidence_intervals: dict[str, Any] = Field(default_factory=dict)
    alpha_spent: float = 0.0
    health_floor_status: dict[str, Any] = Field(default_factory=dict)
    leakage_status: str | dict[str, Any] = "unknown"
    decision_ref: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    evidence_digest: str = ""

    @model_validator(mode="after")
    def normalize_deltas(self) -> "PairedComparison":
        self.axis_deltas = {
            axis_id: value if isinstance(value, AxisDelta) else AxisDelta(axis_id=axis_id, **dict(value))
            for axis_id, value in self.axis_deltas.items()
        }
        self.efficiency_deltas = {
            axis_id: value if isinstance(value, EfficiencyDelta) else EfficiencyDelta(axis_id=axis_id, **dict(value))
            for axis_id, value in self.efficiency_deltas.items()
        }
        return self


class CapabilitySignal(EvidenceModel):
    quality_delta_estimate: float = 0.0
    quality_delta_lower: float = 0.0
    quality_delta_upper: float = 0.0
    axis_ids: list[str] = Field(default_factory=list)


class EfficiencySignal(EvidenceModel):
    quality_equivalent: bool = False
    quality_delta_lower: float = 0.0
    cost_delta_lower: float = 0.0
    latency_delta_lower: float = 0.0
    token_delta_lower: float = 0.0
    axis_ids: list[str] = Field(default_factory=list)


class PreferenceSignal(EvidenceModel):
    preference_delta_estimate: float = 0.0
    preference_delta_lower: float = 0.0
    axis_ids: list[str] = Field(default_factory=list)
    human_or_user_grounded: bool = False


class SubskillSignal(EvidenceModel):
    subskill_id: str = ""
    quality_delta_estimate: float = 0.0
    quality_delta_lower: float = 0.0
    full_task_passed: bool = False
    axis_ids: list[str] = Field(default_factory=list)


class ProgressSignal(EvidenceModel):
    signal_id: str = ""
    parent_runtime_hash: str
    child_runtime_hash: str
    contract_id: str = ""
    oracle_package_hash: str = ""
    parent_oracle_package_hash: str = ""
    child_oracle_package_hash: str = ""
    parent_runtime_spec_digest: str = ""
    child_runtime_spec_digest: str = ""
    decision_type: PromotionDecisionType | str
    capability_signal: CapabilitySignal | None = None
    efficiency_signal: EfficiencySignal | None = None
    preference_signal: PreferenceSignal | None = None
    subskill_signal: SubskillSignal | None = None
    quality_delta_estimate: float = 0.0
    quality_delta_lower: float = 0.0
    quality_delta_upper: float = 0.0
    efficiency_delta_estimate: float = 0.0
    efficiency_delta_lower: float = 0.0
    efficiency_delta_upper: float = 0.0
    improved_axes: list[str] = Field(default_factory=list)
    regressed_axes: list[str] = Field(default_factory=list)
    tied_axes: list[str] = Field(default_factory=list)
    comparison_count: int = 0
    n_eff: float = 0.0
    authority_summary: dict[str, float] = Field(default_factory=dict)
    evidence_digest: str = ""
    reason_codes: list[str] = Field(default_factory=list)
    pairwise_comparisons: list[PairedComparison] = Field(default_factory=list)
    no_capability_signal_reason: str = ""

    @property
    def decision(self) -> str:
        return _value(self.decision_type)

    @model_validator(mode="after")
    def validate_signal(self) -> "ProgressSignal":
        decision = _value(self.decision_type)
        if decision == PromotionDecisionType.CAPABILITY.value:
            if self.capability_signal is None:
                raise ValueError("capability progress requires a capability signal")
            if self.capability_signal.quality_delta_lower <= 0.0:
                raise ValueError("capability progress requires positive quality lower bound")
        if decision == PromotionDecisionType.EFFICIENCY.value:
            if self.efficiency_signal is None:
                raise ValueError("efficiency progress requires an efficiency signal")
            if not self.efficiency_signal.quality_equivalent:
                raise ValueError("efficiency progress requires quality equivalence")
        return self


class PromotionDecision(EvidenceModel):
    decision_id: str
    decision_type: PromotionDecisionType | str
    contract_id: str = ""
    scope: EvidenceScope | dict[str, Any] = Field(default_factory=dict)
    winning_runtime_hash: str = ""
    parent_runtime_hash: str = ""
    child_runtime_hash: str = ""
    oracle_package_hash: str = ""
    parent_oracle_package_hash: str = ""
    child_oracle_package_hash: str = ""
    parent_runtime_spec_digest: str = ""
    child_runtime_spec_digest: str = ""
    comparison_ref: str = ""
    progress_signal_ref: str = ""
    progress_signal: ProgressSignal | None = None
    allowed_optimizer_updates: list[OptimizerUpdate | str] = Field(default_factory=list)
    forbidden_optimizer_updates: list[OptimizerUpdate | str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    alpha_spent: float = 0.0
    evidence_refs: list[str] = Field(default_factory=list)
    quality_delta_lower: float | None = None
    quality_delta_estimate: float | None = None
    efficiency_delta_lower: float | None = None
    efficiency_delta_estimate: float | None = None
    evidence_digest: str = ""
    created_at: float = Field(default_factory=now_ts)

    @model_validator(mode="after")
    def validate_optimizer_updates(self) -> "PromotionDecision":
        decision = _value(self.decision_type)
        allowed = {_value(update) for update in self.allowed_optimizer_updates}
        forbidden = {_value(update) for update in self.forbidden_optimizer_updates}
        if self.progress_signal is not None and _value(self.progress_signal.decision_type) != decision:
            raise ValueError("promotion decision and progress signal decision types must match")
        if decision == PromotionDecisionType.CAPABILITY.value:
            if OptimizerUpdate.CAPABILITY_ARCHIVE.value not in allowed:
                raise ValueError("capability decisions must allow capability archive insertion")
            return self
        if allowed & CAPABILITY_UPDATES:
            raise ValueError("only capability decisions may allow capability optimizer updates")
        if decision in {
            PromotionDecisionType.REJECT.value,
            PromotionDecisionType.ABSTAIN.value,
            PromotionDecisionType.QUARANTINE.value,
            PromotionDecisionType.NO_PROGRESS.value,
        }:
            illegal = allowed & PROMOTING_UPDATES
            if illegal:
                raise ValueError(f"{decision} decisions may not allow promoting optimizer updates: {sorted(illegal)}")
        if decision != PromotionDecisionType.CAPABILITY.value and not forbidden:
            self.forbidden_optimizer_updates = sorted(CAPABILITY_UPDATES)
        return self
