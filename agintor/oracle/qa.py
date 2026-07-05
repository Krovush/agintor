from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..contracts import OraclePackage, OraclePackageQAReport
from ..contracts.oracle import oracle_package_hash, oracle_public_view_hash, oracle_sealed_view_hash
from ..utils import stable_hash
from .projections import public_oracle_projection


@dataclass(frozen=True)
class QACheck:
    name: str
    passed: bool
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def row(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "reason": self.reason, "details": dict(self.details)}


class OracleQARunner:
    """Deterministic QA gate for frozen oracle packages.

    This runner intentionally does not call LLMs. The compiler may be adaptive;
    package freezing and QA must be deterministic.
    """

    def run(self, package: OraclePackage) -> OraclePackageQAReport:
        checks: list[QACheck] = []
        checks.append(self._schema_check(package))
        checks.append(self._hash_check(package))
        checks.append(self._hard_claim_coverage_check(package))
        checks.append(self._validator_health_check(package))
        checks.append(self._leakage_projection_check(package))
        checks.append(self._vacuity_check(package))
        checks.append(self._comparability_check(package))
        passed = all(check.passed for check in checks)
        reasons = [check.reason for check in checks if not check.passed and check.reason]
        return OraclePackageQAReport(
            report_id=f"oracle-qa.{stable_hash(package.package_id, package.package_hash, [c.row() for c in checks])[:16]}",
            package_id=package.package_id,
            package_hash=package.package_hash,
            passed=passed,
            status="pass" if passed else "fail",
            checks=[check.row() for check in checks],
            reason_codes=reasons,
            public_view_hash=package.public_view_hash,
            sealed_view_hash=package.sealed_view_hash,
        )

    @staticmethod
    def _schema_check(package: OraclePackage) -> QACheck:
        try:
            OraclePackage.model_validate(package.model_dump(mode="json"))
            return QACheck("schema", True)
        except Exception as exc:  # pragma: no cover - defensive
            return QACheck("schema", False, "invalid_schema", {"error": str(exc)})

    @staticmethod
    def _hash_check(package: OraclePackage) -> QACheck:
        public_hash = oracle_public_view_hash(package)
        sealed_hash = oracle_sealed_view_hash(package)
        package_hash = oracle_package_hash(package, assume_projection_hashes=(public_hash, sealed_hash))
        ok = public_hash == package.public_view_hash and sealed_hash == package.sealed_view_hash and package_hash == package.package_hash
        return QACheck(
            "hashes",
            ok,
            "hash_mismatch" if not ok else "",
            {"public": public_hash, "sealed": sealed_hash, "package": package_hash},
        )

    @staticmethod
    def _hard_claim_coverage_check(package: OraclePackage) -> QACheck:
        hard_claims = {claim.claim_id for claim in package.claim_graph.claims if claim.criticality == "hard"}
        covered = {claim_id for validator in package.validator_specs for claim_id in validator.claim_ids}
        explicitly_unverifiable = {claim.claim_id for claim in package.claim_graph.claims if claim.unverifiable_reason}
        missing = sorted(hard_claims - covered - explicitly_unverifiable)
        return QACheck("hard_claim_coverage", not missing, "missing_critical_validators" if missing else "", {"missing": missing})

    @staticmethod
    def _validator_health_check(package: OraclePackage) -> QACheck:
        missing_health = sorted(
            validator.validator_id
            for validator in package.validator_specs
            if validator.visibility in {"private", "sealed"} and not validator.health_tests
        )
        return QACheck("validator_health", not missing_health, "missing_validator_health_tests" if missing_health else "", {"missing": missing_health})

    @staticmethod
    def _leakage_projection_check(package: OraclePackage) -> QACheck:
        try:
            public_payload = public_oracle_projection(package)
            forbidden = set(package.leakage_policy.sealed_fields_forbidden)
            _assert_no_sealed_material(public_payload, forbidden)
            return QACheck("leakage_projection", True)
        except Exception as exc:
            return QACheck("leakage_projection", False, "sealed_projection_leakage", {"error": str(exc)})

    @staticmethod
    def _vacuity_check(package: OraclePackage) -> QACheck:
        task_count = sum(len(task_set.tasks) for task_set in package.task_sets)
        validator_count = len(package.validator_specs)
        claim_count = len(package.claim_graph.claims)
        ok = task_count > 0 and validator_count > 0 and claim_count > 0
        return QACheck("non_vacuous", ok, "empty_oracle_package" if not ok else "", {"tasks": task_count, "validators": validator_count, "claims": claim_count})

    @staticmethod
    def _comparability_check(package: OraclePackage) -> QACheck:
        frozen = bool(package.frozen)
        has_contract = bool(package.evidence_contract and package.evidence_contract.contract_id)
        return QACheck("comparability_identity", frozen and has_contract, "missing_comparability_identity" if not (frozen and has_contract) else "")


def qa_oracle_package(package: OraclePackage) -> OraclePackageQAReport:
    return OracleQARunner().run(package)


def _assert_no_sealed_material(value: Any, forbidden_keys: set[str], *, path: str = "<root>") -> None:
    if isinstance(value, dict):
        for raw_key, item in value.items():
            key = str(raw_key)
            child_path = f"{path}.{key}"
            if key in forbidden_keys or key.startswith(("private_", "sealed_", "hidden_")):
                raise ValueError(f"sealed material key {key!r} visible at {child_path}")
            _assert_no_sealed_material(item, forbidden_keys, path=child_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_no_sealed_material(item, forbidden_keys, path=f"{path}[{index}]")


__all__ = ["OracleQARunner", "QACheck", "qa_oracle_package"]
