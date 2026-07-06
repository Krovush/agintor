from __future__ import annotations

from typing import Any, Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..utils import now_ts, stable_hash
from .benchmarks import BenchmarkTask, sealed_benchmark_task_payload, runtime_visible_benchmark_task
from .evidence import AuthorityLevel, DomainEvidenceContract, EvidenceRef


class OracleModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


AuthorityLiteral = Literal["A0", "A1", "A2", "A3", "A4", "A5"]
ClaimCriticality = Literal["hard", "major", "minor", "diagnostic"]
ClaimType = Literal["outcome", "state", "process", "safety", "factual", "semantic", "architecture", "cost"]
ValidatorVisibility = Literal["public", "private", "sealed"]
ValidatorStatus = Literal["pass", "fail", "error", "abstain"]


class ValidationIntent(OracleModel):
    task_classes: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    user_weights: dict[str, float] = Field(default_factory=dict)
    hard_failures: list[str] = Field(default_factory=list)
    acceptable_tradeoffs: list[str] = Field(default_factory=list)
    authority_floor: AuthorityLiteral | str = "A4"
    unverifiable_residual_policy: Literal["abstain", "human_audit", "diagnostic_only"] = "abstain"
    notes: list[str] = Field(default_factory=list)


class ClaimSpec(OracleModel):
    claim_id: str
    text: str
    claim_type: ClaimType
    criticality: ClaimCriticality = "major"
    weight: float = 1.0
    minimum_authority: AuthorityLiteral | str = "A4"
    dependencies: list[str] = Field(default_factory=list)
    unverifiable_reason: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ClaimGraph(OracleModel):
    graph_id: str = "claims"
    claims: list[ClaimSpec] = Field(default_factory=list)
    edges: list[dict[str, str]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_claim_graph(self) -> "ClaimGraph":
        ids = [claim.claim_id for claim in self.claims]
        if len(ids) != len(set(ids)):
            raise ValueError("claim_id values must be unique")
        id_set = set(ids)
        for claim in self.claims:
            missing = sorted(set(claim.dependencies) - id_set)
            if missing:
                raise ValueError(f"claim {claim.claim_id!r} has missing dependencies {missing}")
        return self


class ProofObligation(OracleModel):
    obligation_id: str
    claim_ids: list[str]
    description: str = ""
    required_validator_families: list[str] = Field(default_factory=list)
    minimum_authority: AuthorityLiteral | str = "A4"
    failure_action: Literal["reject", "abstain", "quarantine", "diagnostic"] = "abstain"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ValidatorSpec(OracleModel):
    validator_id: str
    family_id: str
    claim_ids: list[str] = Field(default_factory=list)
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs_schema: dict[str, Any] = Field(default_factory=dict)
    authority_ceiling: AuthorityLiteral | str = "A4"
    visibility: ValidatorVisibility = "sealed"
    independence_group: str = "default"
    leakage_risk: Literal["low", "medium", "high"] = "medium"
    health_tests: list[str] = Field(default_factory=list)
    failure_action: Literal["reject", "abstain", "quarantine", "diagnostic"] = "abstain"
    metadata: dict[str, Any] = Field(default_factory=dict)


class OracleTask(OracleModel):
    task_id: str
    benchmark_task: BenchmarkTask
    claim_ids: list[str] = Field(default_factory=list)
    validator_ids: list[str] = Field(default_factory=list)
    partition: Literal["train", "val", "test", "heldout", "confirmatory", "proxy"] = "train"
    public_tags: list[str] = Field(default_factory=list)
    sealed_refs: list[str] = Field(default_factory=list)
    sealed_payload_digest: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    def public_task(self) -> BenchmarkTask:
        return oracle_runtime_visible_benchmark_task(self)

    def sealed_payload(self) -> dict[str, Any]:
        return sealed_benchmark_task_payload(self.benchmark_task)


class OracleTaskSet(OracleModel):
    task_set_id: str
    partition: Literal["train", "val", "test", "heldout", "confirmatory", "proxy"] = "train"
    tasks: list[OracleTask] = Field(default_factory=list)
    sampling_policy: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class FixtureBundleRef(OracleModel):
    ref_id: str
    uri: str = ""
    digest: str = ""
    visibility: Literal["public", "private", "sealed"] = "sealed"
    description: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ScoringProjection(OracleModel):
    projection_id: str = "default"
    axis_weights: dict[str, float] = Field(default_factory=dict)
    claim_weights: dict[str, float] = Field(default_factory=dict)
    hard_claim_ids: list[str] = Field(default_factory=list)
    scalar_score_rule: Literal["weighted_claim_mean", "min_hard_then_weighted_mean", "diagnostic_only"] = "min_hard_then_weighted_mean"
    metadata: dict[str, Any] = Field(default_factory=dict)


class AuthorityPolicy(OracleModel):
    authority_floor: AuthorityLiteral | str = "A4"
    critical_claim_floor: AuthorityLiteral | str = "A4"
    allow_model_judge_promotion: bool = False
    authority_caps_by_family: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class LeakagePolicy(OracleModel):
    status_required: bool = True
    sealed_fields_forbidden: list[str] = Field(
        default_factory=lambda: [
            "private_expected",
            "private_answer_ref",
            "private_answer_mechanism",
            "expected_digest",
            "hidden_tests",
            "private_rubric",
            "promotion_threshold",
        ]
    )
    runtime_visible_validator_families: list[str] = Field(default_factory=list)
    tamper_evidence_action: Literal["reject", "quarantine"] = "quarantine"
    metadata: dict[str, Any] = Field(default_factory=dict)


class AbstentionPolicy(OracleModel):
    missing_validator_action: Literal["abstain", "reject", "diagnostic"] = "abstain"
    unsupported_claim_action: Literal["abstain", "human_audit", "diagnostic"] = "abstain"
    insufficient_authority_action: Literal["abstain", "human_audit", "diagnostic"] = "abstain"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ValidatorResult(OracleModel):
    validator_id: str
    family_id: str = ""
    claim_ids: list[str] = Field(default_factory=list)
    status: ValidatorStatus
    authority_used: AuthorityLiteral | str = "A0"
    health_status: dict[str, Any] = Field(default_factory=dict)
    observations: dict[str, Any] = Field(default_factory=dict)
    evidence_digest: str = ""
    created_at: float = Field(default_factory=now_ts)

    @model_validator(mode="after")
    def fill_digest(self) -> "ValidatorResult":
        if not self.evidence_digest:
            self.evidence_digest = stable_hash(
                self.validator_id,
                self.family_id,
                self.claim_ids,
                self.status,
                self.authority_used,
                self.health_status,
                self.observations,
            )
        return self


class ClaimResult(OracleModel):
    claim_id: str
    satisfied: bool | None = None
    posterior_lower: float | None = None
    posterior_upper: float | None = None
    authority_mass: dict[str, float] = Field(default_factory=dict)
    coverage: float = 0.0
    residual_unverified: str = ""
    validator_result_ids: list[str] = Field(default_factory=list)
    evidence_digest: str = ""

    @model_validator(mode="after")
    def fill_digest(self) -> "ClaimResult":
        if not self.evidence_digest:
            self.evidence_digest = stable_hash(
                self.claim_id,
                self.satisfied,
                self.posterior_lower,
                self.posterior_upper,
                self.authority_mass,
                self.coverage,
                self.residual_unverified,
                self.validator_result_ids,
            )
        return self


class OraclePackage(OracleModel):
    package_id: str
    oracle_family_id: str = "adaptive_general"
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
    scoring_projection: ScoringProjection = Field(default_factory=ScoringProjection)
    authority_policy: AuthorityPolicy = Field(default_factory=AuthorityPolicy)
    leakage_policy: LeakagePolicy = Field(default_factory=LeakagePolicy)
    abstention_policy: AbstentionPolicy = Field(default_factory=AbstentionPolicy)
    qa_report_ref: str = ""
    public_view_hash: str = ""
    sealed_view_hash: str = ""
    sealed_payload_digest: str = ""
    frozen: bool = True
    created_at: float = Field(default_factory=now_ts)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_oracle_package(self) -> "OraclePackage":
        claim_ids = {claim.claim_id for claim in self.claim_graph.claims}
        validator_claims = {claim_id for validator in self.validator_specs for claim_id in validator.claim_ids}
        for validator in self.validator_specs:
            missing = sorted(set(validator.claim_ids) - claim_ids)
            if missing:
                raise ValueError(f"validator {validator.validator_id!r} references unknown claims {missing}")
        for obligation in self.proof_obligations:
            missing = sorted(set(obligation.claim_ids) - claim_ids)
            if missing:
                raise ValueError(f"obligation {obligation.obligation_id!r} references unknown claims {missing}")
        critical_uncovered = sorted(
            claim.claim_id
            for claim in self.claim_graph.claims
            if claim.criticality == "hard" and claim.claim_id not in validator_claims and not claim.unverifiable_reason
        )
        if critical_uncovered:
            raise ValueError(f"hard claims require validators or explicit abstention: {critical_uncovered}")
        for task_set in self.task_sets:
            for task in task_set.tasks:
                computed_task_digest = oracle_task_sealed_payload_digest(task)
                if task.sealed_payload_digest and task.sealed_payload_digest != computed_task_digest:
                    raise ValueError(f"task {task.task_id!r} sealed_payload_digest does not match sealed task payload")
                if not task.sealed_payload_digest:
                    task.sealed_payload_digest = computed_task_digest
        computed_public = oracle_public_view_hash(self)
        computed_sealed = oracle_sealed_view_hash(self)
        if self.public_view_hash and self.public_view_hash != computed_public:
            raise ValueError("public_view_hash does not match package projection")
        if self.sealed_view_hash and self.sealed_view_hash != computed_sealed:
            raise ValueError("sealed_view_hash does not match package projection")
        computed_package = oracle_package_hash(self, assume_projection_hashes=(computed_public, computed_sealed))
        if self.package_hash and self.package_hash != computed_package:
            raise ValueError("package_hash does not match package payload")
        if not self.public_view_hash:
            self.public_view_hash = computed_public
        if not self.sealed_view_hash:
            self.sealed_view_hash = computed_sealed
        computed_sealed_payload = oracle_sealed_payload_digest(self)
        if self.sealed_payload_digest and self.sealed_payload_digest != computed_sealed_payload:
            raise ValueError("sealed_payload_digest does not match nested sealed task payloads")
        if not self.sealed_payload_digest:
            self.sealed_payload_digest = computed_sealed_payload
        if not self.package_hash:
            self.package_hash = computed_package
        return self

    def assert_frozen(self) -> None:
        if not self.frozen:
            raise ValueError("OraclePackage is not frozen")


class OraclePackageDraft(OracleModel):
    draft_id: str
    goal_id: str
    package: OraclePackage
    compiler_trace_ref: str = ""
    created_at: float = Field(default_factory=now_ts)


class OraclePackageQAReport(OracleModel):
    report_id: str
    package_id: str
    package_hash: str
    passed: bool
    status: Literal["pass", "fail", "abstain"] = "pass"
    checks: list[dict[str, Any]] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    public_view_hash: str = ""
    sealed_view_hash: str = ""
    created_at: float = Field(default_factory=now_ts)


_HASH_EXCLUDE_KEYS = {"package_hash", "public_view_hash", "sealed_view_hash", "qa_report_ref", "created_at", "completed_at"}


def _strip_private_mapping(value: Any, forbidden_keys: set[str]) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, Mapping):
        return {
            str(key): _strip_private_mapping(item, forbidden_keys)
            for key, item in sorted(value.items())
            if str(key) not in forbidden_keys and not str(key).startswith("private_")
        }
    if isinstance(value, list):
        return [_strip_private_mapping(item, forbidden_keys) for item in value]
    return value


def _hash_payload(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, Mapping):
        return {
            str(key): _hash_payload(item)
            for key, item in sorted(value.items())
            if str(key) not in _HASH_EXCLUDE_KEYS
        }
    if isinstance(value, list):
        return [_hash_payload(item) for item in value]
    return value


def oracle_task_sealed_payload_digest(task: OracleTask) -> str:
    return stable_hash("agintor.oracle.task.sealed", sealed_benchmark_task_payload(task.benchmark_task))


def oracle_sealed_payload_digest(package: OraclePackage | dict[str, Any]) -> str:
    pkg = package if isinstance(package, OraclePackage) else OraclePackage.model_validate(package)
    task_digests = [
        {
            "task_set_id": task_set.task_set_id,
            "task_id": task.task_id,
            "sealed_payload_digest": oracle_task_sealed_payload_digest(task),
        }
        for task_set in pkg.task_sets
        for task in task_set.tasks
    ]
    return stable_hash("agintor.oracle.sealed.payloads", task_digests)


def oracle_runtime_visible_benchmark_task(task: OracleTask) -> BenchmarkTask:
    public_task = runtime_visible_benchmark_task(task.benchmark_task)
    expected = {"oracle_claim_ids": list(task.claim_ids)}
    return public_task.model_copy(
        update={
            "expected": expected,
            "private_expected": None,
            "verifier_type": "oracle_package",
            "verification_required": True,
        },
        deep=True,
    )


def oracle_public_projection(package: OraclePackage | dict[str, Any]) -> dict[str, Any]:
    pkg = package if isinstance(package, OraclePackage) else OraclePackage.model_validate(package)
    forbidden = set(pkg.leakage_policy.sealed_fields_forbidden)
    evidence_contract_payload = pkg.evidence_contract.model_dump(mode="json", exclude_none=True)
    answer_mechanism = dict(evidence_contract_payload.get("answer_mechanism", {}) or {})
    answer_mechanism.pop("sealed_validators", None)
    if answer_mechanism:
        evidence_contract_payload["answer_mechanism"] = answer_mechanism
    else:
        evidence_contract_payload.pop("answer_mechanism", None)
    public_validator_specs = [
        validator.model_dump(mode="json", exclude_none=True)
        for validator in pkg.validator_specs
        if validator.visibility == "public"
    ]
    public_task_sets: list[dict[str, Any]] = []
    for task_set in pkg.task_sets:
        public_task_sets.append(
            {
                "task_set_id": task_set.task_set_id,
                "partition": task_set.partition,
                "tasks": [
                    {
                        "task_id": task.task_id,
                        "benchmark_task": oracle_runtime_visible_benchmark_task(task).model_dump(mode="json", exclude_none=True),
                        "claim_ids": list(task.claim_ids),
                        "validator_ids": [
                            validator_id
                            for validator_id in task.validator_ids
                            if any(v.validator_id == validator_id and v.visibility == "public" for v in pkg.validator_specs)
                        ],
                        "public_tags": list(task.public_tags),
                        "metadata": _strip_private_mapping(task.metadata, forbidden),
                    }
                    for task in task_set.tasks
                ],
                "sampling_policy": _strip_private_mapping(task_set.sampling_policy, forbidden),
                "metadata": _strip_private_mapping(task_set.metadata, forbidden),
            }
        )
    return {
        "package_id": pkg.package_id,
        "oracle_family_id": pkg.oracle_family_id,
        "goal_id": pkg.goal_id,
        "runtime_spec_digest": pkg.runtime_spec_digest,
        "validation_intent": pkg.validation_intent.model_dump(mode="json", exclude_none=True),
        "claim_graph": pkg.claim_graph.model_dump(mode="json", exclude_none=True),
        "proof_obligations": [item.model_dump(mode="json", exclude_none=True) for item in pkg.proof_obligations],
        "validator_specs": public_validator_specs,
        "task_sets": public_task_sets,
        "fixture_bundle_refs": [
            ref.model_dump(mode="json", exclude_none=True)
            for ref in pkg.fixture_bundle_refs
            if ref.visibility == "public"
        ],
        "evidence_contract": evidence_contract_payload,
        "scoring_projection": pkg.scoring_projection.model_dump(mode="json", exclude_none=True),
        "authority_policy": pkg.authority_policy.model_dump(mode="json", exclude_none=True),
        "leakage_policy": {
            key: value
            for key, value in pkg.leakage_policy.model_dump(mode="json", exclude_none=True).items()
            if key not in {"sealed_fields_forbidden", "runtime_visible_validator_families"}
        },
        "abstention_policy": pkg.abstention_policy.model_dump(mode="json", exclude_none=True),
        "frozen": pkg.frozen,
    }


def oracle_sealed_projection(package: OraclePackage | dict[str, Any]) -> dict[str, Any]:
    pkg = package if isinstance(package, OraclePackage) else OraclePackage.model_validate(package)
    payload = pkg.model_dump(
        mode="json",
        exclude_none=True,
        exclude={"package_hash", "public_view_hash", "sealed_view_hash", "task_sets"},
    )
    payload["sealed_payload_digest"] = oracle_sealed_payload_digest(pkg)
    payload["task_sets"] = []
    for task_set in pkg.task_sets:
        task_set_payload = task_set.model_dump(mode="json", exclude_none=True, exclude={"tasks"})
        task_set_payload["tasks"] = []
        for task in task_set.tasks:
            task_payload = task.model_dump(mode="json", exclude_none=True, exclude={"benchmark_task"})
            task_payload["benchmark_task"] = sealed_benchmark_task_payload(task.benchmark_task)
            task_payload["sealed_payload_digest"] = oracle_task_sealed_payload_digest(task)
            task_set_payload["tasks"].append(task_payload)
        payload["task_sets"].append(task_set_payload)
    return payload


def oracle_public_view_hash(package: OraclePackage | dict[str, Any]) -> str:
    return stable_hash("agintor.oracle.public", _hash_payload(oracle_public_projection(package)))


def oracle_sealed_view_hash(package: OraclePackage | dict[str, Any]) -> str:
    return stable_hash("agintor.oracle.sealed", _hash_payload(oracle_sealed_projection(package)))


def oracle_package_hash(package: OraclePackage | dict[str, Any], *, assume_projection_hashes: tuple[str, str] | None = None) -> str:
    pkg = package if isinstance(package, OraclePackage) else OraclePackage.model_validate(package)
    public_hash, sealed_hash = assume_projection_hashes or (oracle_public_view_hash(pkg), oracle_sealed_view_hash(pkg))
    payload = _hash_payload(oracle_sealed_projection(pkg))
    return stable_hash("agintor.oracle.package", payload, public_hash, sealed_hash)


def freeze_oracle_package(package: OraclePackage | dict[str, Any]) -> OraclePackage:
    pkg = package if isinstance(package, OraclePackage) else OraclePackage.model_validate(package)
    payload = oracle_sealed_projection(pkg)
    payload["frozen"] = True
    payload["public_view_hash"] = ""
    payload["sealed_view_hash"] = ""
    payload["package_hash"] = ""
    return OraclePackage.model_validate(payload)


def oracle_tasks_by_partition(package: OraclePackage, partition: str) -> list[BenchmarkTask]:
    tasks: list[BenchmarkTask] = []
    for task_set in package.task_sets:
        if str(task_set.partition) == str(partition):
            tasks.extend(task.benchmark_task for task in task_set.tasks)
    return tasks


def oracle_runtime_visible_tasks_by_partition(package: OraclePackage, partition: str) -> list[BenchmarkTask]:
    tasks: list[BenchmarkTask] = []
    for task_set in package.task_sets:
        if str(task_set.partition) == str(partition):
            tasks.extend(oracle_runtime_visible_benchmark_task(task) for task in task_set.tasks)
    return tasks


__all__ = [
    "AbstentionPolicy",
    "AuthorityPolicy",
    "ClaimGraph",
    "ClaimResult",
    "ClaimSpec",
    "FixtureBundleRef",
    "LeakagePolicy",
    "OraclePackage",
    "OraclePackageDraft",
    "OraclePackageQAReport",
    "OracleTask",
    "OracleTaskSet",
    "ProofObligation",
    "ScoringProjection",
    "ValidationIntent",
    "ValidatorResult",
    "ValidatorSpec",
    "freeze_oracle_package",
    "oracle_package_hash",
    "oracle_public_projection",
    "oracle_public_view_hash",
    "oracle_runtime_visible_tasks_by_partition",
    "oracle_runtime_visible_benchmark_task",
    "oracle_sealed_projection",
    "oracle_sealed_payload_digest",
    "oracle_sealed_view_hash",
    "oracle_task_sealed_payload_digest",
    "oracle_tasks_by_partition",
]
