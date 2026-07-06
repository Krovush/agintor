from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..contracts import OraclePackage, OraclePackageQAReport
from ..contracts.oracle import oracle_package_hash, oracle_public_view_hash, oracle_sealed_projection, oracle_sealed_view_hash
from ..utils import stable_hash
from .projections import public_oracle_projection
from .validator_registry import ValidatorRegistry, default_validator_registry


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

    def __init__(self, registry: ValidatorRegistry | None = None) -> None:
        self.registry = registry or default_validator_registry()

    def run(self, package: OraclePackage) -> OraclePackageQAReport:
        checks: list[QACheck] = []
        checks.append(self._schema_check(package))
        checks.append(self._hash_check(package))
        checks.append(self._hard_claim_coverage_check(package))
        checks.append(self._task_validator_coverage_check(package))
        checks.append(self._sealed_input_check(package))
        checks.append(self._input_contract_check(package))
        checks.append(self._validator_health_check(package))
        checks.append(self._validator_control_check(package))
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
            OraclePackage.model_validate(oracle_sealed_projection(package))
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
    def _tasks_by_validator(package: OraclePackage) -> dict[str, list[Any]]:
        tasks: dict[str, list[Any]] = {}
        for task_set in package.task_sets:
            for task in task_set.tasks:
                for validator_id in task.validator_ids:
                    tasks.setdefault(str(validator_id), []).append(task)
        return tasks

    @staticmethod
    def _claim_criticality(package: OraclePackage) -> dict[str, str]:
        return {claim.claim_id: claim.criticality for claim in package.claim_graph.claims}

    @staticmethod
    def _authority_rank(authority: Any) -> int:
        text = str(authority)
        return int(text[1:]) if len(text) == 2 and text.startswith("A") and text[1:].isdigit() else 0

    @staticmethod
    def _claim_active_for_task(package: OraclePackage, claim_id: str, oracle_task: Any) -> bool:
        claim_by_id = {str(claim.claim_id): claim for claim in package.claim_graph.claims}
        validator_by_id = {str(validator.validator_id): validator for validator in package.validator_specs}
        claim = claim_by_id.get(str(claim_id))
        if claim is None:
            return False
        validators = [
            validator_by_id[str(validator_id)]
            for validator_id in oracle_task.validator_ids
            if str(validator_id) in validator_by_id
            and str(claim_id) in {str(item) for item in validator_by_id[str(validator_id)].claim_ids}
        ]
        if not validators:
            return False
        explicit_promotion = any(str(validator.failure_action) in {"reject", "quarantine"} for validator in validators)
        diagnostic = str(claim.criticality) == "diagnostic" or bool(claim.unverifiable_reason)
        if diagnostic:
            return explicit_promotion
        return any(str(validator.failure_action) != "diagnostic" for validator in validators)

    @staticmethod
    def _claim_should_have_task_validator(package: OraclePackage, claim_id: str) -> bool:
        claim_by_id = {str(claim.claim_id): claim for claim in package.claim_graph.claims}
        claim = claim_by_id.get(str(claim_id))
        if claim is None:
            return False
        validators = [
            validator
            for validator in package.validator_specs
            if str(claim_id) in {str(item) for item in validator.claim_ids}
        ]
        if not validators:
            return False
        explicit_promotion = any(str(validator.failure_action) in {"reject", "quarantine"} for validator in validators)
        diagnostic = str(claim.criticality) == "diagnostic" or bool(claim.unverifiable_reason)
        if diagnostic:
            return explicit_promotion
        return any(str(validator.failure_action) != "diagnostic" for validator in validators)

    @staticmethod
    def _task_validator_coverage_check(package: OraclePackage) -> QACheck:
        missing: list[dict[str, str]] = []
        unknown: list[dict[str, str]] = []
        validator_by_id = {str(validator.validator_id): validator for validator in package.validator_specs}
        for task_set in package.task_sets:
            for oracle_task in task_set.tasks:
                for validator_id in oracle_task.validator_ids:
                    if str(validator_id) not in validator_by_id:
                        unknown.append(
                            {
                                "task_id": str(oracle_task.task_id),
                                "validator_id": str(validator_id),
                            }
                        )
                known_validators = [
                    validator_by_id[str(validator_id)]
                    for validator_id in oracle_task.validator_ids
                    if str(validator_id) in validator_by_id
                ]
                for claim_id in {str(claim_id) for claim_id in oracle_task.claim_ids}:
                    if not OracleQARunner._claim_should_have_task_validator(package, claim_id):
                        continue
                    if any(claim_id in {str(item) for item in validator.claim_ids} for validator in known_validators):
                        continue
                    missing.append(
                        {
                            "task_id": str(oracle_task.task_id),
                            "claim_id": claim_id,
                        }
                    )
        ok = not missing and not unknown
        return QACheck(
            "task_validator_coverage",
            ok,
            "missing_task_local_validators" if not ok else "",
            {"missing": missing, "unknown_validator_ids": unknown},
        )

    @staticmethod
    def _covers_promotion_claim(package: OraclePackage, validator_ids: list[str]) -> bool:
        validators = {validator.validator_id: validator for validator in package.validator_specs}
        tasks_by_validator = OracleQARunner._tasks_by_validator(package)
        for validator_id in validator_ids:
            validator = validators.get(validator_id)
            if validator is None:
                continue
            if validator.failure_action == "diagnostic":
                continue
            validator_claim_ids = {str(claim_id) for claim_id in validator.claim_ids}
            for oracle_task in tasks_by_validator.get(str(validator_id), []):
                for claim_id in {str(claim_id) for claim_id in oracle_task.claim_ids} & validator_claim_ids:
                    if OracleQARunner._claim_active_for_task(package, claim_id, oracle_task):
                        return True
        return False

    @staticmethod
    def _claim_effective_authority_floor(package: OraclePackage, claim_id: str) -> str:
        floor_rank = 0
        for claim in package.claim_graph.claims:
            if str(claim.claim_id) == str(claim_id):
                floor_rank = max(floor_rank, OracleQARunner._authority_rank(claim.minimum_authority))
                break
        for axis in package.evidence_contract.quality_axes:
            raw = axis if isinstance(axis, dict) else {}
            axis_id = str(getattr(axis, "axis_id", raw.get("axis_id", "")) or "")
            if axis_id != str(claim_id):
                continue
            floor_rank = max(
                floor_rank,
                OracleQARunner._authority_rank(getattr(axis, "minimum_authority", raw.get("minimum_authority", "A0"))),
            )
        return f"A{floor_rank}"

    @staticmethod
    def _validator_active_authority_floor(package: OraclePackage, validator: Any) -> str:
        tasks_by_validator = OracleQARunner._tasks_by_validator(package)
        floor_rank = 0
        validator_claim_ids = {str(claim_id) for claim_id in validator.claim_ids}
        for oracle_task in tasks_by_validator.get(str(validator.validator_id), []):
            for claim_id in {str(claim_id) for claim_id in oracle_task.claim_ids} & validator_claim_ids:
                if not OracleQARunner._claim_active_for_task(package, claim_id, oracle_task):
                    continue
                floor_rank = max(
                    floor_rank,
                    OracleQARunner._authority_rank(OracleQARunner._claim_effective_authority_floor(package, claim_id)),
                )
        return f"A{floor_rank}"

    def _sealed_input_check(self, package: OraclePackage) -> QACheck:
        tasks_by_validator = self._tasks_by_validator(package)
        round_tripped = OraclePackage.model_validate(oracle_sealed_projection(package))
        round_trip_tasks = self._tasks_by_validator(round_tripped)
        missing: list[str] = []
        for validator in package.validator_specs:
            if validator.family_id != "exact_private_answer":
                continue
            tasks = round_trip_tasks.get(validator.validator_id, [])
            if not tasks or any(task.benchmark_task.private_expected is None for task in tasks):
                missing.append(validator.validator_id)
        return QACheck(
            "sealed_inputs",
            not missing,
            "missing_sealed_validator_inputs" if missing else "",
            {
                "missing_private_expected": missing,
                "validators_with_tasks": {key: len(value) for key, value in tasks_by_validator.items()},
            },
        )

    def _input_contract_check(self, package: OraclePackage) -> QACheck:
        tasks_by_validator = self._tasks_by_validator(package)
        blocked: dict[str, list[str]] = {}
        for validator in package.validator_specs:
            missing = self._missing_contract_inputs(validator, tasks_by_validator.get(validator.validator_id, []))
            if missing:
                blocked[validator.validator_id] = missing
        promotion_blocked = [validator_id for validator_id in blocked if self._covers_promotion_claim(package, [validator_id])]
        return QACheck(
            "validator_input_contracts",
            not promotion_blocked,
            "unsatisfied_validator_input_contracts" if promotion_blocked else "",
            {"blocked": blocked, "promotion_blocked": promotion_blocked},
        )

    @staticmethod
    def _missing_contract_inputs(validator: Any, tasks: list[Any]) -> list[str]:
        missing: list[str] = []
        inputs = dict(validator.inputs or {})
        contract = dict(dict(validator.metadata or {}).get("input_contract", {}) or {})
        for field in contract.get("requires", []) or []:
            field = str(field)
            if field == "artifact":
                continue
            if field == "private_expected":
                if not tasks or any(task.benchmark_task.private_expected is None for task in tasks):
                    missing.append(field)
                continue
            if field == "trace":
                continue
            if field == "final_state":
                continue
            if field == "patch_artifact":
                continue
            if field not in inputs or _missing_value(inputs.get(field)):
                missing.append(field)
        for field in contract.get("sealed_requires", []) or []:
            field = str(field)
            if field == "private_expected":
                if not tasks or any(task.benchmark_task.private_expected is None for task in tasks):
                    missing.append(field)
                continue
            if field not in inputs or _missing_value(inputs.get(field)):
                missing.append(field)
        requires_any = [str(field) for field in contract.get("requires_any", []) or []]
        if requires_any and not any(not _missing_value(inputs.get(field)) for field in requires_any):
            missing.append("any:" + "|".join(requires_any))
        if validator.family_id == "exact_private_answer":
            if not tasks or any(task.benchmark_task.private_expected is None for task in tasks):
                missing.append("private_expected")
        if validator.family_id == "trace_state" and not (inputs.get("required_events") or inputs.get("forbidden_events")):
            missing.append("trace_obligations")
        if validator.family_id == "stateful_service" and not inputs.get("expected_state"):
            missing.append("expected_state")
        if validator.family_id == "repo_patch":
            for field in ("repo_snapshot_digest", "public_test_command_digest", "hidden_tests_digest"):
                if not inputs.get(field):
                    missing.append(field)
        if validator.family_id == "schema_artifact" and _missing_value(inputs.get("schema")):
            missing.append("schema")
        return sorted(set(missing))

    def _validator_control_check(self, package: OraclePackage) -> QACheck:
        failures: dict[str, list[dict[str, Any]]] = {}
        authority_failures: dict[str, list[dict[str, Any]]] = {}
        unsupported: list[str] = []
        for validator in package.validator_specs:
            try:
                family = self.registry.get(validator.family_id)
            except KeyError:
                unsupported.append(validator.validator_id)
                continue
            cases = _control_cases(validator)
            if not cases:
                unsupported.append(validator.validator_id)
                continue
            authority_floor = self._validator_active_authority_floor(package, validator)
            for case in cases:
                result = family.run_validator(validator, dict(case["payload"]))
                expected = case["expected_status"]
                if result.status != expected:
                    failures.setdefault(validator.validator_id, []).append(
                        {
                            "case": case["name"],
                            "expected_status": expected,
                            "actual_status": result.status,
                            "observations": result.observations,
                        }
                    )
                    continue
                if expected == "pass" and self._authority_rank(result.authority_used) < self._authority_rank(authority_floor):
                    authority_failures.setdefault(validator.validator_id, []).append(
                        {
                            "case": case["name"],
                            "minimum_authority": authority_floor,
                            "actual_authority": result.authority_used,
                            "observations": result.observations,
                        }
                    )
        promotion_failures = [
            validator_id
            for validator_id in sorted(set(failures) | set(authority_failures) | set(unsupported))
            if self._covers_promotion_claim(package, [validator_id])
        ]
        return QACheck(
            "validator_controls",
            not promotion_failures,
            "validator_controls_failed" if promotion_failures else "",
            {"failures": failures, "authority_failures": authority_failures, "unsupported": unsupported, "promotion_failures": promotion_failures},
        )

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
            if key in forbidden_keys or key.startswith(("private_", "hidden_", "sealed_")):
                raise ValueError(f"sealed material key {key!r} visible at {child_path}")
            _assert_no_sealed_material(item, forbidden_keys, path=child_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_no_sealed_material(item, forbidden_keys, path=f"{path}[{index}]")


def _missing_value(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _different_control_state(expected: Any, label: str, *used: Any) -> Any:
    candidates = [
        {"__qa_control__": label},
        {"__qa_control__": label, "value": False},
        {"wrong": True},
        {"irrelevant": True},
        {"__qa_control_digest__": stable_hash(label, expected)},
    ]
    for candidate in candidates:
        if candidate != expected and all(candidate != item for item in used):
            return candidate
    return {"__qa_control__": label, "fallback_digest": stable_hash(expected, label, used)}


def _control_cases(validator: Any) -> list[dict[str, Any]]:
    inputs = dict(validator.inputs or {})
    if validator.family_id == "exact_private_answer":
        return [
            {"name": "known_good", "payload": {"artifact": "expected", "private_expected": "expected"}, "expected_status": "pass"},
            {"name": "known_bad", "payload": {"artifact": "wrong", "private_expected": "expected"}, "expected_status": "fail"},
            {"name": "empty_artifact", "payload": {"artifact": None, "private_expected": "expected"}, "expected_status": "fail"},
            {"name": "irrelevant_artifact", "payload": {"artifact": {"other": True}, "private_expected": "expected"}, "expected_status": "fail"},
            {"name": "leakage_canary", "payload": {"artifact": "__LEAKED__", "private_expected": "__SEALED__"}, "expected_status": "fail"},
        ]
    if validator.family_id == "trace_state":
        required = [str(event) for event in inputs.get("required_events", [])]
        forbidden = [str(event) for event in inputs.get("forbidden_events", [])]
        if not required and not forbidden:
            return []
        good_trace = [{"event": event} for event in required] or [{"event": "allowed_event"}]
        bad_trace = [{"event": event} for event in forbidden[:1]] if forbidden else []
        if required:
            bad_trace = []
        return [
            {"name": "known_good", "payload": {"trace": good_trace}, "expected_status": "pass"},
            {"name": "known_bad", "payload": {"trace": bad_trace}, "expected_status": "fail"},
            {"name": "empty_artifact", "payload": {"trace": []}, "expected_status": "fail"},
            {"name": "irrelevant_artifact", "payload": {"trace": [{"event": "irrelevant"}]}, "expected_status": "fail" if required else "pass"},
            {
                "name": "leakage_canary",
                "payload": {"trace": ([{"event": forbidden[0]}] if forbidden else [*good_trace, {"event": "__sealed_canary__"}])},
                "expected_status": "fail" if forbidden else "pass",
            },
        ]
    if validator.family_id == "stateful_service":
        expected = inputs.get("expected_state")
        if not expected:
            return []
        bad_state = _different_control_state(expected, "known_bad")
        irrelevant_state = _different_control_state(expected, "irrelevant", bad_state)
        return [
            {"name": "known_good", "payload": {"final_state": expected}, "expected_status": "pass"},
            {"name": "known_bad", "payload": {"final_state": bad_state}, "expected_status": "fail"},
            {"name": "empty_artifact", "payload": {"final_state": {}}, "expected_status": "fail"},
            {"name": "irrelevant_artifact", "payload": {"final_state": irrelevant_state}, "expected_status": "fail"},
            {"name": "leakage_canary", "payload": {"final_state": expected, "duplicate_side_effects": 1}, "expected_status": "fail"},
        ]
    if validator.family_id == "repo_patch":
        receipt = {
            "runner_digest": "runner",
            "repo_snapshot_digest": inputs.get("repo_snapshot_digest", "repo"),
            "public_test_command_digest": inputs.get("public_test_command_digest", "public"),
            "hidden_tests_digest": inputs.get("hidden_tests_digest", "hidden"),
            "applied": True,
            "public_tests_passed": True,
            "hidden_tests_passed": True,
            "tampered_tests": False,
        }
        return [
            {"name": "known_good", "payload": {"repo_patch_result": receipt}, "expected_status": "pass"},
            {"name": "known_bad", "payload": {"artifact": {"applied": True, "public_tests_passed": True, "hidden_tests_passed": True}}, "expected_status": "fail"},
            {"name": "empty_artifact", "payload": {"artifact": {}}, "expected_status": "fail"},
            {"name": "irrelevant_artifact", "payload": {"artifact": {"irrelevant": True}}, "expected_status": "fail"},
            {"name": "leakage_canary", "payload": {"repo_patch_result": {**receipt, "tampered_tests": True}}, "expected_status": "fail"},
        ]
    if validator.family_id == "schema_artifact":
        schema_value = inputs.get("schema")
        if not isinstance(schema_value, dict) or not schema_value:
            return []
        schema = dict(schema_value)
        good = _schema_control_artifact(schema, valid=True)
        bad = _schema_control_artifact(schema, valid=False)
        return [
            {"name": "known_good", "payload": {"artifact": good, "schema": schema}, "expected_status": "pass"},
            {"name": "known_bad", "payload": {"artifact": bad, "schema": schema}, "expected_status": "fail"},
            {"name": "empty_artifact", "payload": {"artifact": None, "schema": schema}, "expected_status": "fail"},
            {"name": "irrelevant_artifact", "payload": {"artifact": bad, "schema": schema}, "expected_status": "fail"},
            {"name": "leakage_canary", "payload": {"artifact": bad, "schema": schema}, "expected_status": "fail"},
        ]
    return []


def _schema_control_artifact(schema: dict[str, Any], *, valid: bool) -> Any:
    inferred_type = "object" if schema.get("required") or schema.get("properties") else "array" if schema.get("items") else ""
    schema_type = str(schema.get("type") or inferred_type)
    if schema_type == "object":
        if not valid:
            return "not-an-object"
        return {str(key): "ok" for key in schema.get("required", []) if isinstance(key, str)}
    if schema_type == "array":
        return ["ok"] if valid else "not-an-array"
    if schema_type == "string":
        return "ok" if valid else {}
    if schema_type == "number":
        return 1.0 if valid else "not-a-number"
    if schema_type == "boolean":
        return True if valid else "not-a-boolean"
    return {"ok": True} if valid else None


__all__ = ["OracleQARunner", "QACheck", "qa_oracle_package"]
