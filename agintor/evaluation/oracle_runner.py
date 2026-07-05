from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, Field

from ..contracts import ClaimResult, OraclePackage, OracleTask, RunResult, ValidatorResult, ValidatorSpec
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
        oracle_task = oracle_task or self._task_for_run(package, run)
        if oracle_task is None:
            return [], self._claim_results(package, [], claim_ids=[])
        payload = {
            **_run_payload(run),
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
        return validator_results, self._claim_results(package, validator_results, claim_ids=oracle_task.claim_ids)

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


__all__ = ["OracleEvaluationRunner", "SealedEvaluatorPayload"]
