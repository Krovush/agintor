from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..contracts import OraclePackage
from .package_io import finalize_oracle_package
from .projections import public_oracle_projection, assert_no_private_oracle_fields


class OracleQAIssue(BaseModel):
    issue_id: str
    severity: Literal["error", "warning", "info"] = "error"
    message: str


class OracleQAReport(BaseModel):
    package_id: str
    package_hash: str = ""
    passed: bool = False
    issues: list[OracleQAIssue] = Field(default_factory=list)

    @property
    def errors(self) -> list[OracleQAIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]


def run_oracle_qa(package: OraclePackage) -> OracleQAReport:
    issues: list[OracleQAIssue] = []

    def add(issue_id: str, message: str, severity: Literal["error", "warning", "info"] = "error") -> None:
        issues.append(OracleQAIssue(issue_id=issue_id, severity=severity, message=message))

    frozen = finalize_oracle_package(package)
    if not package.frozen:
        add("package.not_frozen", "oracle package must be frozen before candidate evaluation")
    claim_ids = {claim.claim_id for claim in package.claim_graph.claims}
    validator_claims = {claim_id for validator in package.validator_specs for claim_id in validator.claim_ids}
    for claim in package.claim_graph.claims:
        if claim.criticality in {"hard", "major"} and claim.claim_id not in validator_claims and not claim.unverifiable_reason:
            add(
                f"claim.{claim.claim_id}.missing_validator",
                f"critical claim {claim.claim_id!r} has no validator and no explicit unverifiable reason",
            )
    for obligation in package.proof_obligations:
        missing = sorted(set(obligation.claim_ids) - claim_ids)
        if missing:
            add(f"obligation.{obligation.obligation_id}.missing_claims", f"proof obligation references missing claims: {missing}")
        if not obligation.validator_family_hints:
            add(f"obligation.{obligation.obligation_id}.no_family_hint", "proof obligation has no validator family hints", "warning")
    validator_ids = [validator.validator_id for validator in package.validator_specs]
    if len(validator_ids) != len(set(validator_ids)):
        add("validators.duplicate_ids", "validator ids must be unique")
    if not package.validator_specs:
        add("validators.empty", "oracle package requires at least one validator")
    if not package.task_sets:
        add("tasks.empty", "oracle package requires at least one task set")
    try:
        public_view = public_oracle_projection(frozen)
        assert_no_private_oracle_fields(public_view)
    except Exception as exc:
        add("projection.private_leakage", str(exc))
    if package.authority_policy.allow_model_judge_promotion_alone:
        add("authority.weak_judge_promotion", "model judges should not be final promotion authority alone", "warning")
    if package.evidence_contract.contract_id == "":
        add("evidence_contract.missing_id", "evidence contract id is required")
    return OracleQAReport(
        package_id=package.package_id,
        package_hash=frozen.package_hash,
        passed=not any(issue.severity == "error" for issue in issues),
        issues=issues,
    )


def assert_oracle_qa_passes(package: OraclePackage) -> OracleQAReport:
    report = run_oracle_qa(package)
    if not report.passed:
        rendered = "; ".join(f"{issue.issue_id}: {issue.message}" for issue in report.errors)
        raise ValueError(f"oracle QA failed: {rendered}")
    return report


__all__ = ["OracleQAIssue", "OracleQAReport", "assert_oracle_qa_passes", "run_oracle_qa"]
