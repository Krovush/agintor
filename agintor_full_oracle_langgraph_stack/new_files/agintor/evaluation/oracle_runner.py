from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping, Sequence

from ..contracts import ClaimResult, OraclePackage, RunResult, ValidatorResult, ValidatorSpec
from ..oracle.validator_registry import ValidatorRegistry, default_validator_registry
from ..utils import stable_hash


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

    def evaluate_run(self, package: OraclePackage, run: RunResult | Mapping[str, Any], *, sealed_payload: Mapping[str, Any] | None = None) -> tuple[list[ValidatorResult], list[ClaimResult]]:
        payload = {**_run_payload(run), **dict(sealed_payload or {})}
        validator_results: list[ValidatorResult] = []
        for validator in package.validator_specs:
            validator_results.append(self._run_validator(validator, payload))
        return validator_results, self._claim_results(package, validator_results)

    def _run_validator(self, spec: ValidatorSpec, payload: dict[str, Any]) -> ValidatorResult:
        try:
            family = self.registry.get(spec.family_id)
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
    def _claim_results(package: OraclePackage, validator_results: Sequence[ValidatorResult]) -> list[ClaimResult]:
        by_claim: dict[str, list[ValidatorResult]] = defaultdict(list)
        for result in validator_results:
            for claim_id in result.claim_ids:
                by_claim[claim_id].append(result)
        claim_results: list[ClaimResult] = []
        for claim in package.claim_graph.claims:
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


__all__ = ["OracleEvaluationRunner"]
