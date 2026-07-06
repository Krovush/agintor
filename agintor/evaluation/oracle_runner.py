from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, Field

from ..contracts import (
    ClaimPosterior,
    ClaimResult,
    EvidenceLedger,
    OraclePackage,
    OracleTask,
    RunResult,
    ValidatorReport,
    ValidatorResult,
    ValidatorSpec,
)
from ..oracle.validator_registry import ValidatorRegistry, default_validator_registry
from ..utils import stable_hash


class SealedEvaluatorPayload(BaseModel):
    package: OraclePackage
    fixture_resolver_id: str = ""
    trace_events: list[dict[str, Any]] = Field(default_factory=list)
    side_effect_receipts: list[dict[str, Any]] = Field(default_factory=list)
    workspace_root: str = ""
    sealed_artifact_refs: dict[str, str] = Field(default_factory=dict)


def _run_payload(run: RunResult | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(run, RunResult):
        return {
            "artifact": run.artifact,
            "trace": run.trace_rows(),
            "task_id": run.task_id,
            "runtime_hash": run.runtime_hash,
            "run_id": run.run_id,
            "request_id": run.request_id,
            "attempt_id": run.attempt_id,
            "seed": run.seed,
            "verifier_score": run.verifier_score,
            "hard_invalid": run.hard_invalid,
            "invalid_reason": run.invalid_reason,
        }
    return dict(run)


class OracleEvaluationRunner:
    """Runs sealed/package validators and aggregates claim-level evidence.

    Validators emit observations. This runner produces claim results but still
    does not decide promotion; ProgressOracle consumes the resulting evidence.
    """

    def __init__(self, registry: ValidatorRegistry | None = None) -> None:
        self.registry = registry or default_validator_registry()

    def evaluate_run(
        self,
        package: OraclePackage,
        run: RunResult | Mapping[str, Any],
        *,
        oracle_task: OracleTask | None = None,
        sealed_payload: Mapping[str, Any] | None = None,
    ) -> tuple[list[ValidatorResult], list[ClaimResult]]:
        validator_results, claim_results, _ledger = self.evaluate_run_with_ledger(
            package,
            run,
            oracle_task=oracle_task,
            sealed_payload=sealed_payload,
        )
        return validator_results, claim_results

    def evaluate_run_with_ledger(
        self,
        package: OraclePackage,
        run: RunResult | Mapping[str, Any],
        *,
        oracle_task: OracleTask | None = None,
        sealed_payload: Mapping[str, Any] | None = None,
    ) -> tuple[list[ValidatorResult], list[ClaimResult], EvidenceLedger]:
        oracle_task = oracle_task or self._task_for_run(package, run)
        run_payload = _run_payload(run)
        if oracle_task is None:
            claim_results = self._claim_results(package, [], claim_ids=[])
            return [], claim_results, self._evidence_ledger(
                package,
                run_payload,
                validator_reports=[],
                claim_posteriors=self._claim_posteriors(package, claim_results, []),
            )
        payload = {
            **run_payload,
            **oracle_task.sealed_payload(),
            **dict(sealed_payload or {}),
        }
        validator_ids = {str(validator_id) for validator_id in oracle_task.validator_ids}
        validators = [
            validator
            for validator in package.validator_specs
            if str(validator.validator_id) in validator_ids
        ]
        validator_results = [self._run_validator(validator, payload) for validator in validators]
        claim_results = self._claim_results(package, validator_results, claim_ids=oracle_task.claim_ids)
        validator_reports = self._validator_reports(validator_results, validators)
        ledger = self._evidence_ledger(
            package,
            run_payload,
            validator_reports=validator_reports,
            claim_posteriors=self._claim_posteriors(package, claim_results, validator_reports),
        )
        return validator_results, claim_results, ledger

    @staticmethod
    def _task_for_run(package: OraclePackage, run: RunResult | Mapping[str, Any]) -> OracleTask | None:
        payload = _run_payload(run)
        run_task_id = str(payload.get("task_id", "") or "")
        for task_set in package.task_sets:
            for oracle_task in task_set.tasks:
                public_task = oracle_task.public_task()
                if run_task_id in {str(oracle_task.task_id), str(oracle_task.benchmark_task.task_id), str(public_task.task_id)}:
                    return oracle_task
        return None

    def _run_validator(self, spec: ValidatorSpec, payload: dict[str, Any]) -> ValidatorResult:
        try:
            family = self.registry.get(spec.family_id)
        except KeyError:
            return ValidatorResult(
                validator_id=spec.validator_id,
                family_id=spec.family_id,
                claim_ids=list(spec.claim_ids),
                status="abstain",
                authority_used="A0",
                observations={"reason": "unsupported_validator_family"},
            )
        try:
            return family.run_validator(spec, payload)
        except Exception as exc:
            return ValidatorResult(
                validator_id=spec.validator_id,
                family_id=spec.family_id,
                claim_ids=list(spec.claim_ids),
                status="error",
                authority_used="A0",
                health_status={"error": True},
                observations={"error": str(exc)},
            )

    @staticmethod
    def _claim_results(
        package: OraclePackage,
        validator_results: Sequence[ValidatorResult],
        *,
        claim_ids: Sequence[str] | None = None,
    ) -> list[ClaimResult]:
        allowed_claim_ids = {str(claim_id) for claim_id in claim_ids} if claim_ids is not None else None
        by_claim: dict[str, list[ValidatorResult]] = defaultdict(list)
        for result in validator_results:
            for claim_id in result.claim_ids:
                claim_id = str(claim_id)
                if allowed_claim_ids is None or claim_id in allowed_claim_ids:
                    by_claim[claim_id].append(result)
        claim_results: list[ClaimResult] = []
        for claim in package.claim_graph.claims:
            if allowed_claim_ids is not None and claim.claim_id not in allowed_claim_ids:
                continue
            results = by_claim.get(claim.claim_id, [])
            passed = [result for result in results if result.status == "pass"]
            failed = [result for result in results if result.status in {"fail", "error"}]
            authority_mass: dict[str, float] = defaultdict(float)
            for result in results:
                authority_mass[str(result.authority_used)] += 1.0
            if failed:
                satisfied: bool | None = False
            elif passed:
                satisfied = True
            else:
                satisfied = None
            coverage = 1.0 if results else 0.0
            residual = "" if results else "missing_validator_result"
            claim_results.append(
                ClaimResult(
                    claim_id=claim.claim_id,
                    satisfied=satisfied,
                    posterior_lower=1.0 if satisfied is True else 0.0 if satisfied is False else None,
                    posterior_upper=1.0 if satisfied is True else 0.0 if satisfied is False else None,
                    authority_mass=dict(authority_mass),
                    coverage=coverage,
                    residual_unverified=residual,
                    validator_result_ids=[result.validator_id for result in results],
                    evidence_digest=stable_hash(claim.claim_id, [result.evidence_digest for result in results]),
                )
            )
        return claim_results

    @staticmethod
    def _validator_reports(
        validator_results: Sequence[ValidatorResult],
        validators: Sequence[ValidatorSpec],
    ) -> list[ValidatorReport]:
        spec_by_id = {str(validator.validator_id): validator for validator in validators}
        reports: list[ValidatorReport] = []
        for result in validator_results:
            spec = spec_by_id.get(str(result.validator_id))
            coverage = 1.0 if result.status in {"pass", "fail"} else 0.0
            leakage_flags = []
            observations = dict(result.observations or {})
            if observations.get("leakage") or observations.get("leakage_flags"):
                raw_flags = observations.get("leakage_flags") or ["leakage"]
                leakage_flags = [str(flag) for flag in raw_flags]
            reports.append(
                ValidatorReport(
                    validator_id=result.validator_id,
                    family_id=result.family_id,
                    claim_ids=list(result.claim_ids),
                    status=result.status,
                    score=1.0 if result.status == "pass" else 0.0 if result.status == "fail" else None,
                    interval_lower=1.0 if result.status == "pass" else 0.0 if result.status == "fail" else None,
                    interval_upper=1.0 if result.status == "pass" else 0.0 if result.status == "fail" else None,
                    authority_used=result.authority_used,
                    authority_ceiling=getattr(spec, "authority_ceiling", "A0") if spec is not None else "A0",
                    coverage=coverage,
                    independence_group=getattr(spec, "independence_group", "default") if spec is not None else "default",
                    leakage_flags=leakage_flags,
                    observations=observations,
                    evidence_digest=result.evidence_digest,
                )
            )
        return reports

    @staticmethod
    def _claim_posteriors(
        package: OraclePackage,
        claim_results: Sequence[ClaimResult],
        validator_reports: Sequence[ValidatorReport],
    ) -> list[ClaimPosterior]:
        report_by_validator = {str(report.validator_id): report for report in validator_reports}
        posteriors: list[ClaimPosterior] = []
        for result in claim_results:
            authority_floor = OracleEvaluationRunner._claim_authority_floor(package, str(result.claim_id))
            report_ids = [
                report_by_validator[str(validator_id)].report_id
                for validator_id in result.validator_result_ids
                if str(validator_id) in report_by_validator
            ]
            reports = [
                report_by_validator[str(validator_id)]
                for validator_id in result.validator_result_ids
                if str(validator_id) in report_by_validator
            ]
            coverage = OracleEvaluationRunner._posterior_coverage(reports)
            authority_sufficient = OracleEvaluationRunner._supporting_authority_meets_floor(reports, authority_floor)
            quarantine_reason = OracleEvaluationRunner._posterior_quarantine_reason(reports)
            if quarantine_reason:
                state = "quarantined"
            elif result.satisfied is True and authority_sufficient:
                state = "satisfied"
            elif result.satisfied is True:
                state = "abstained"
            elif result.satisfied is False:
                state = "failed"
            elif result.residual_unverified:
                state = "unverifiable"
            elif reports and coverage <= 0.0:
                state = "abstained"
            else:
                state = "uncertain"
            residual_reason = result.residual_unverified
            posterior_lower = result.posterior_lower
            posterior_upper = result.posterior_upper
            if quarantine_reason:
                coverage = 0.0
                residual_reason = quarantine_reason
                posterior_lower = None
                posterior_upper = None
            elif result.satisfied is True and not authority_sufficient:
                coverage = 0.0
                residual_reason = residual_reason or "insufficient_authority"
                posterior_lower = None
                posterior_upper = None
            if reports and coverage <= 0.0 and not residual_reason:
                residual_reason = "unsupported_validator_result"
            posteriors.append(
                ClaimPosterior(
                    claim_id=result.claim_id,
                    state=state,
                    posterior_lower=posterior_lower,
                    posterior_upper=posterior_upper,
                    authority_mass=OracleEvaluationRunner._authority_mass_for_reports(reports),
                    coverage=coverage,
                    residual_mass=max(0.0, 1.0 - coverage),
                    residual_reason=residual_reason,
                    validator_report_ids=report_ids,
                )
            )
        return posteriors

    @staticmethod
    def _posterior_coverage(validator_reports: Sequence[ValidatorReport]) -> float:
        supported_statuses = {"pass", "fail", "score"}
        supported_coverage = [
            max(0.0, float(report.coverage))
            for report in validator_reports
            if str(report.status) in supported_statuses and float(report.coverage) > 0.0
        ]
        return min(1.0, sum(supported_coverage))

    @staticmethod
    def _posterior_quarantine_reason(validator_reports: Sequence[ValidatorReport]) -> str:
        if any(str(report.status) == "quarantine" for report in validator_reports):
            return "validator_quarantine"
        if any(report.leakage_flags for report in validator_reports):
            return "leakage_flag"
        return ""

    @staticmethod
    def _claim_authority_floor(package: OraclePackage, claim_id: str) -> str:
        floor_rank = 0
        plan = getattr(package, "validation_plan", None)
        if plan is not None:
            for claim in getattr(plan, "claims", []) or []:
                raw = dict(claim) if isinstance(claim, Mapping) else {}
                if str(getattr(claim, "claim_id", raw.get("claim_id", ""))) != str(claim_id):
                    continue
                floor_rank = max(
                    floor_rank,
                    OracleEvaluationRunner._authority_rank(str(getattr(claim, "authority_floor", raw.get("authority_floor", "A0")))),
                )
                break
        for claim in package.claim_graph.claims:
            if str(claim.claim_id) != str(claim_id):
                continue
            floor_rank = max(floor_rank, OracleEvaluationRunner._authority_rank(str(claim.minimum_authority)))
            break
        for axis in package.evidence_contract.quality_axes:
            raw = dict(axis) if isinstance(axis, Mapping) else {}
            if str(getattr(axis, "axis_id", raw.get("axis_id", ""))) != str(claim_id):
                continue
            floor_rank = max(
                floor_rank,
                OracleEvaluationRunner._authority_rank(str(getattr(axis, "minimum_authority", raw.get("minimum_authority", "A0")))),
            )
        return f"A{floor_rank}"

    @staticmethod
    def _supporting_authority_meets_floor(validator_reports: Sequence[ValidatorReport], authority_floor: str) -> bool:
        floor_rank = OracleEvaluationRunner._authority_rank(authority_floor)
        if floor_rank <= 0:
            return True
        return any(
            str(report.status) in {"pass", "score"}
            and float(report.coverage) > 0.0
            and OracleEvaluationRunner._effective_authority_rank(report) >= floor_rank
            for report in validator_reports
        )

    @staticmethod
    def _effective_authority_rank(report: ValidatorReport) -> int:
        return min(
            OracleEvaluationRunner._authority_rank(str(report.authority_used)),
            OracleEvaluationRunner._authority_rank(str(report.authority_ceiling)),
        )

    @staticmethod
    def _effective_authority(report: ValidatorReport) -> str:
        return f"A{OracleEvaluationRunner._effective_authority_rank(report)}"

    @staticmethod
    def _authority_mass_for_reports(validator_reports: Sequence[ValidatorReport]) -> dict[str, float]:
        authority_mass: dict[str, float] = defaultdict(float)
        for report in validator_reports:
            coverage = max(0.0, float(report.coverage))
            if coverage > 0.0:
                authority_mass[OracleEvaluationRunner._effective_authority(report)] += coverage
        return dict(authority_mass)

    @staticmethod
    def _authority_rank(authority: str) -> int:
        text = str(authority)
        return int(text[1:]) if len(text) == 2 and text.startswith("A") and text[1:].isdigit() else 0

    @staticmethod
    def _evidence_ledger(
        package: OraclePackage,
        run_payload: Mapping[str, Any],
        *,
        validator_reports: Sequence[ValidatorReport],
        claim_posteriors: Sequence[ClaimPosterior],
    ) -> EvidenceLedger:
        coverage: dict[str, float] = {}
        independence_partition: dict[str, list[str]] = defaultdict(list)
        leakage_flags: list[str] = []
        for report in validator_reports:
            independence_partition[str(report.independence_group)].append(report.report_id)
            leakage_flags.extend(str(flag) for flag in report.leakage_flags)
        for posterior in claim_posteriors:
            coverage[str(posterior.claim_id)] = float(posterior.coverage)
        return EvidenceLedger(
            oracle_package_hash=str(getattr(package, "package_hash", "") or ""),
            validation_plan_hash=str(getattr(package, "validation_plan_hash", "") or ""),
            public_projection_hash=str(getattr(package, "public_view_hash", "") or ""),
            sealed_projection_hash=str(getattr(package, "sealed_view_hash", "") or ""),
            runtime_hash=str(run_payload.get("runtime_hash", "") or ""),
            task_id=str(run_payload.get("task_id", "") or ""),
            run_id=str(run_payload.get("run_id", "") or run_payload.get("request_id", "") or ""),
            seed=int(run_payload["seed"]) if run_payload.get("seed") is not None else None,
            validator_reports=list(validator_reports),
            claim_posteriors=list(claim_posteriors),
            authority_mass=OracleEvaluationRunner._authority_mass_for_reports(validator_reports),
            coverage=coverage,
            independence_partition={key: sorted(value) for key, value in independence_partition.items()},
            leakage_attestation={"status": "flagged" if leakage_flags else "clean", "flags": sorted(set(leakage_flags))},
            process_violations=[],
            side_effect_violations=[],
            unverifiable_residual={
                posterior.claim_id: posterior.residual_reason
                for posterior in claim_posteriors
                if posterior.residual_reason
            },
            audit_status=OracleEvaluationRunner._audit_status(validator_reports, claim_posteriors, leakage_flags),
            scalar_score=float(run_payload["verifier_score"]) if run_payload.get("verifier_score") is not None else None,
            scalar_score_authority="M0",
            promotion_authoritative=False,
        )

    @staticmethod
    def _audit_status(
        validator_reports: Sequence[ValidatorReport],
        claim_posteriors: Sequence[ClaimPosterior],
        leakage_flags: Sequence[str],
    ) -> str:
        if leakage_flags or any(str(report.status) == "quarantine" for report in validator_reports):
            return "quarantine"
        if any(str(report.status) in {"fail", "error", "contradiction"} for report in validator_reports):
            return "fail"
        if not validator_reports:
            return "diagnostic"
        if not claim_posteriors:
            return "diagnostic"
        if any(
            posterior.residual_reason
            or str(posterior.state) in {"abstained", "unverifiable"}
            or float(posterior.residual_mass) > 0.0
            for posterior in claim_posteriors
        ):
            return "abstain"
        supported = [
            report
            for report in validator_reports
            if str(report.status) in {"pass", "score"} and float(report.coverage) > 0.0
        ]
        if len(supported) == len(validator_reports) and any(str(posterior.state) == "satisfied" for posterior in claim_posteriors):
            return "pass"
        if any(str(report.status) == "abstain" for report in validator_reports):
            return "abstain"
        return "diagnostic"


__all__ = ["OracleEvaluationRunner", "SealedEvaluatorPayload"]
