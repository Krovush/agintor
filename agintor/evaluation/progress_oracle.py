from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..contracts import (
    AxisDelta,
    CapabilitySignal,
    DomainEvidenceContract,
    EfficiencyDelta,
    EfficiencySignal,
    PairedComparison,
    ProgressSignal,
    PromotionDecision,
    PromotionDecisionType,
    RunResult,
    SubskillSignal,
    SuiteEvaluation,
)
from ..utils import mean, stable_hash, std_error


@dataclass(frozen=True)
class ProgressOracleConfig:
    capability_epsilon: float = 0.03
    efficiency_epsilon: float = 0.02
    equivalence_tolerance: float = 0.0
    protected_regression_tolerance: float = 0.0
    confidence_z: float = 1.96
    saturation_threshold: float = 0.999
    min_quality_comparisons: int = 2


@dataclass(frozen=True)
class PairedEffect:
    estimate: float
    lower: float
    upper: float
    n_eff: float


def paired_mean_effect(
    child_scores: Sequence[float],
    parent_scores: Sequence[float],
    *,
    confidence_z: float = 1.96,
    singleton_margin: float = 1.0,
) -> PairedEffect:
    if len(child_scores) != len(parent_scores):
        raise ValueError("paired score lengths mismatch")
    deltas = [float(child) - float(parent) for child, parent in zip(child_scores, parent_scores)]
    if not deltas:
        return PairedEffect(estimate=0.0, lower=0.0, upper=0.0, n_eff=0.0)
    estimate = mean(deltas)
    margin = singleton_margin if len(deltas) <= 1 else confidence_z * std_error(deltas)
    return PairedEffect(
        estimate=float(estimate),
        lower=float(max(-1.0, estimate - margin)),
        upper=float(min(1.0, estimate + margin)),
        n_eff=float(len(deltas)),
    )


def _contract_distribution(contract: DomainEvidenceContract) -> Mapping[str, Any]:
    return dict(contract.challenge_distribution or {})


def _minimum_pairs(contract: DomainEvidenceContract | None) -> int:
    if contract is None:
        return 0
    distribution = _contract_distribution(contract)
    statistical_rule = dict(contract.statistical_rule or {})
    return int(
        distribution.get("minimum_frontier_tasks")
        or distribution.get("minimum_pairs")
        or statistical_rule.get("minimum_pairs")
        or statistical_rule.get("min_pairs")
        or 0
    )


def _minimum_frontier_tasks(contract: DomainEvidenceContract | None) -> int:
    if contract is None:
        return 0
    distribution = _contract_distribution(contract)
    return int(distribution.get("minimum_frontier_tasks") or 0)


def _quality_axis_id(axis: Any) -> str:
    raw = dict(axis) if isinstance(axis, Mapping) else {}
    return str(getattr(axis, "axis_id", raw.get("axis_id", "")))


def _quality_axis_metadata(axis: Any) -> dict[str, Any]:
    raw = dict(axis) if isinstance(axis, Mapping) else {}
    metadata = getattr(axis, "metadata", raw.get("metadata", {}))
    return dict(metadata or {})


def _quality_axis_promotion_kind(axis: Any, default: str = "capability") -> str:
    raw = dict(axis) if isinstance(axis, Mapping) else {}
    return str(getattr(axis, "promotion_kind", raw.get("promotion_kind", default)))


def _quality_axis_comparator_type(axis: Any) -> str:
    raw = dict(axis) if isinstance(axis, Mapping) else {}
    return str(getattr(axis, "comparator_type", raw.get("comparator_type", "exact_outcome")))


def _unsupported_quality_comparators(contract: DomainEvidenceContract | None) -> list[str]:
    if contract is None:
        return []
    unsupported = {"pairwise_preference", "metamorphic", "defect_search"}
    return sorted(
        {
            _quality_axis_comparator_type(axis)
            for axis in contract.quality_axes
            if _quality_axis_comparator_type(axis) in unsupported
        }
    )


def _quality_axis_promotion_eligible(contract: DomainEvidenceContract | None, axis_id: str, default: bool = True) -> bool:
    if contract is None:
        return default
    for axis in contract.quality_axes:
        if _quality_axis_id(axis) == axis_id:
            raw = dict(axis) if isinstance(axis, Mapping) else {}
            return bool(getattr(axis, "promotion_eligible", raw.get("promotion_eligible", default)))
    return default


def _quality_axis_weight(contract: DomainEvidenceContract | None, axis_id: str, default: float = 1.0) -> float:
    if contract is None:
        return default
    for axis in contract.quality_axes:
        if _quality_axis_id(axis) == axis_id:
            raw = dict(axis) if isinstance(axis, Mapping) else {}
            return max(0.0, float(getattr(axis, "weight", raw.get("weight", default))))
    return default


def _axis_matches_task(axis: Any, task_id: str, metadata: Mapping[str, Any], *, allow_wildcard: bool) -> bool:
    if _quality_axis_id(axis) == task_id:
        return True
    axis_metadata = _quality_axis_metadata(axis)
    if not axis_metadata:
        return allow_wildcard
    task_ids = {str(item) for item in axis_metadata.get("task_ids", [])}
    if task_ids and str(task_id) not in task_ids:
        return False
    domain_kind = axis_metadata.get("domain_kind")
    if domain_kind and str(metadata.get("domain_kind", "")) != str(domain_kind):
        return False
    required_tags = {str(tag) for tag in axis_metadata.get("slice_tags", [])}
    if required_tags:
        task_tags = {str(tag) for tag in metadata.get("slice_tags", [])}
        if not required_tags.issubset(task_tags):
            return False
    return True


def _axis_epsilon(contract: DomainEvidenceContract | None, axis_id: str, default: float) -> float:
    if contract is None:
        return default
    for axis in contract.quality_axes:
        raw = dict(axis) if isinstance(axis, Mapping) else {}
        if _quality_axis_id(axis) == axis_id:
            return float(getattr(axis, "epsilon", raw.get("epsilon", default)))
    return default


def _axis_regression_tolerance(contract: DomainEvidenceContract | None, axis_id: str, default: float) -> float:
    if contract is None:
        return default
    for axis in contract.quality_axes:
        raw = dict(axis) if isinstance(axis, Mapping) else {}
        if _quality_axis_id(axis) == axis_id:
            return float(getattr(axis, "protected_regression_tolerance", raw.get("protected_regression_tolerance", default)))
    return default


def _efficiency_axis_id(axis: Any) -> str:
    raw = dict(axis) if isinstance(axis, Mapping) else {}
    return str(getattr(axis, "axis_id", raw.get("axis_id", "")))


def _efficiency_axis_metric(axis: Any) -> str:
    raw = dict(axis) if isinstance(axis, Mapping) else {}
    return str(getattr(axis, "metric", raw.get("metric", "cost")))


def _efficiency_axis_comparator_type(axis: Any) -> str:
    raw = dict(axis) if isinstance(axis, Mapping) else {}
    return str(getattr(axis, "comparator_type", raw.get("comparator_type", "exact_outcome")))


def _efficiency_axis_lower_is_better(axis: Any) -> bool:
    raw = dict(axis) if isinstance(axis, Mapping) else {}
    return bool(getattr(axis, "lower_is_better", raw.get("lower_is_better", True)))


def _efficiency_axis_epsilon(contract: DomainEvidenceContract | None, axis_id: str, default: float) -> float:
    if contract is None:
        return default
    for axis in contract.efficiency_axes:
        raw = dict(axis) if isinstance(axis, Mapping) else {}
        if _efficiency_axis_id(axis) == axis_id:
            return float(getattr(axis, "epsilon", raw.get("epsilon", default)))
    return default


def _health_issue(contract: DomainEvidenceContract, comparison: PairedComparison) -> str:
    required = {str(key) for key in dict(contract.health_floors or {})}
    status = dict(comparison.health_floor_status or {})
    failing = {"fail", "failed", "quarantined", "quarantine"}
    passing = {"pass", "passed", "clean", "ok", "true"}
    if any(str(value).lower() in failing for value in status.values()):
        return "health_floor_failed"
    if required:
        missing = [
            key
            for key in required
            if key not in status or str(status.get(key, "")).lower() not in passing
        ]
        if missing:
            return "missing_health_floor_evidence"
    return ""


def _leakage_issue(contract: DomainEvidenceContract, comparison: PairedComparison) -> str:
    status = comparison.leakage_status
    if isinstance(status, Mapping):
        if bool(status.get("detected", False)):
            return "leakage_detected"
        value = status.get("status", "unknown")
    else:
        value = status
    normalized = str(value).lower()
    if normalized in {"leaked", "detected", "fail", "failed", "dirty"}:
        return "leakage_detected"
    leakage_policy = dict(contract.leakage_policy or {})
    policy_requires_status = bool(leakage_policy) and leakage_policy.get("status_required", True) is not False
    requires_leakage_evidence = policy_requires_status or "leakage" in dict(contract.health_floors or {})
    if requires_leakage_evidence and normalized not in {"clean", "pass", "passed", "ok"}:
        return "missing_leakage_evidence"
    return ""


def _is_implicit_suite_contract(contract: DomainEvidenceContract | None) -> bool:
    if contract is None:
        return False
    return str(contract.version) == "implicit" or str(contract.contract_id) == "implicit_suite_progress_contract"


def _is_frontier_source(source: Any) -> bool:
    return "frontier" in str(source)


def _oracle_hash_mismatch(comparison: PairedComparison) -> bool:
    common_hash = str(comparison.oracle_package_hash or "").strip()
    parent_hash = str(comparison.parent_oracle_package_hash or "").strip() or common_hash
    child_hash = str(comparison.child_oracle_package_hash or "").strip() or common_hash
    return bool(parent_hash or child_hash) and parent_hash != child_hash


def _evaluation_identity(evaluation: SuiteEvaluation) -> Mapping[str, Any]:
    identity = dict(getattr(evaluation, "evaluation_identity", {}) or {})
    if identity:
        return identity
    return dict(evaluation.task_metadata.get("__agintor_evaluation_identity__", {}) or {})


class ProgressOracle:
    def __init__(self, config: ProgressOracleConfig | None = None) -> None:
        self.config = config or ProgressOracleConfig()

    def compare(self, parent: SuiteEvaluation, child: SuiteEvaluation) -> ProgressSignal:
        decision = self.decide_evaluations(parent, child)
        if decision.progress_signal is not None:
            return decision.progress_signal
        comparison = self.compare_evaluations(parent, child)
        return self._signal_from_decision(decision, comparison)

    def decide_evaluations(
        self,
        parent: SuiteEvaluation,
        child: SuiteEvaluation,
        *,
        contract: DomainEvidenceContract | None = None,
        leakage_status: str | Mapping[str, Any] | None = None,
        health_floor_status: Mapping[str, Any] | None = None,
    ) -> PromotionDecision:
        comparison = self.compare_evaluations(
            parent,
            child,
            contract=contract,
            leakage_status=leakage_status,
            health_floor_status=health_floor_status,
        )
        if contract is None:
            contract = self._implicit_contract(comparison)
            comparison = self.compare_evaluations(
                parent,
                child,
                contract=contract,
                leakage_status=comparison.leakage_status,
                health_floor_status=comparison.health_floor_status,
            )
        if child.invalid:
            return self._decision(
                comparison,
                "reject",
                contract=contract,
                reason_codes=["invalid_child_evaluation"],
            )
        return self.decide(
            contract=contract,
            comparison=comparison,
            static_suite_saturated=all(delta.saturated for delta in comparison.axis_deltas.values()),
            frontier_evidence_available=any(_is_frontier_source(delta.source) for delta in comparison.axis_deltas.values()),
        )

    def reject_evaluations(
        self,
        parent: SuiteEvaluation,
        child: SuiteEvaluation,
        *,
        contract: DomainEvidenceContract | None = None,
        leakage_status: str | Mapping[str, Any] | None = None,
        health_floor_status: Mapping[str, Any] | None = None,
        reason_codes: Sequence[str],
    ) -> PromotionDecision:
        comparison = self.compare_evaluations(
            parent,
            child,
            contract=contract,
            leakage_status=leakage_status,
            health_floor_status=health_floor_status,
        )
        if contract is None:
            contract = self._implicit_contract(comparison)
            comparison = self.compare_evaluations(
                parent,
                child,
                contract=contract,
                leakage_status=comparison.leakage_status,
                health_floor_status=comparison.health_floor_status,
            )
        return self._decision(comparison, "reject", contract=contract, reason_codes=reason_codes)

    def compare_evaluations(
        self,
        parent: SuiteEvaluation,
        child: SuiteEvaluation,
        *,
        contract: DomainEvidenceContract | None = None,
        leakage_status: str | Mapping[str, Any] | None = None,
        health_floor_status: Mapping[str, Any] | None = None,
    ) -> PairedComparison:
        axis_deltas, challenge_ids, axis_task_ids = self._quality_axis_deltas(parent, child, contract=contract)
        efficiency_deltas = self._efficiency_deltas(parent, child, contract=contract)
        if health_floor_status is None:
            health_floor_status = {"verifier": "pass", "leakage": "pass"} if contract is None else {}
        if leakage_status is None:
            leakage_status = "clean" if contract is None else "unknown"
        parent_identity = _evaluation_identity(parent)
        child_identity = _evaluation_identity(child)
        parent_oracle_hash = str(parent_identity.get("oracle_package_hash", "") or "")
        child_oracle_hash = str(child_identity.get("oracle_package_hash", "") or "")
        common_oracle_hash = parent_oracle_hash if parent_oracle_hash == child_oracle_hash else ""
        evidence_digest = stable_hash(
            parent.runtime_hash,
            child.runtime_hash,
            list(challenge_ids),
            [axis.model_dump(mode="json") for axis in axis_deltas.values()],
            efficiency_deltas,
            parent_identity,
            child_identity,
        )
        return PairedComparison(
            comparison_id=stable_hash("paired-comparison", parent.runtime_hash, child.runtime_hash, challenge_ids, evidence_digest)[:24],
            parent_runtime_hash=parent.runtime_hash,
            child_runtime_hash=child.runtime_hash,
            contract_id=contract.contract_id if contract is not None else "implicit_suite_progress_contract",
            oracle_package_hash=common_oracle_hash,
            parent_oracle_package_hash=parent_oracle_hash,
            child_oracle_package_hash=child_oracle_hash,
            parent_runtime_spec_digest=str(parent_identity.get("runtime_spec_digest", "") or ""),
            child_runtime_spec_digest=str(child_identity.get("runtime_spec_digest", "") or ""),
            challenge_ids=challenge_ids,
            axis_deltas={axis.axis_id: axis for axis in axis_deltas.values()},
            protected_axis_bounds={axis.axis_id: axis.lower for axis in axis_deltas.values()},
            axis_task_ids=axis_task_ids,
            efficiency_deltas=efficiency_deltas,
            confidence_intervals={
                axis.axis_id: {"lower": axis.lower, "upper": axis.upper}
                for axis in axis_deltas.values()
            },
            health_floor_status=dict(health_floor_status),
            leakage_status=leakage_status,
            evidence_digest=evidence_digest,
        )

    def decide(
        self,
        *,
        contract: DomainEvidenceContract | None,
        comparison: PairedComparison,
        parent_static_exact_pass_rate: float | None = None,
        child_static_exact_pass_rate: float | None = None,
        static_suite_saturated: bool = False,
        frontier_evidence_available: bool = True,
    ) -> PromotionDecision:
        reason_codes: list[str] = []
        if _oracle_hash_mismatch(comparison):
            return self._decision(
                comparison,
                "quarantine",
                contract=contract,
                reason_codes=["oracle_package_hash_mismatch"],
            )
        if contract is None:
            return self._decision(comparison, "abstain", reason_codes=["missing_domain_evidence_contract"])
        unsupported_comparators = _unsupported_quality_comparators(contract)
        if unsupported_comparators:
            return self._decision(
                comparison,
                "abstain",
                contract=contract,
                reason_codes=[
                    "unsupported_quality_comparator",
                    *[f"unsupported_comparator:{name}" for name in unsupported_comparators],
                ],
            )
        health_issue = _health_issue(contract, comparison)
        if health_issue:
            decision_type = "quarantine" if health_issue == "health_floor_failed" else "abstain"
            return self._decision(comparison, decision_type, contract=contract, reason_codes=[health_issue])
        leakage_issue = _leakage_issue(contract, comparison)
        if leakage_issue:
            decision_type = "quarantine" if leakage_issue == "leakage_detected" else "abstain"
            return self._decision(comparison, decision_type, contract=contract, reason_codes=[leakage_issue])

        axis_deltas = list(comparison.axis_deltas.values())
        if not axis_deltas:
            return self._decision(comparison, "abstain", contract=contract, reason_codes=["no_paired_quality_evidence"])

        distinct_challenges = len(set(comparison.challenge_ids))
        minimum_frontier_tasks = _minimum_frontier_tasks(contract)
        frontier_challenges = {
            str(task_id)
            for axis in axis_deltas
            if _is_frontier_source(axis.source)
            for task_id in comparison.axis_task_ids.get(axis.axis_id, [])
        }
        has_frontier_axis = any(_is_frontier_source(axis.source) for axis in axis_deltas)
        if minimum_frontier_tasks and not frontier_challenges and has_frontier_axis:
            frontier_challenges = {str(challenge_id) for challenge_id in comparison.challenge_ids}
        n_pairs = (
            (len(frontier_challenges) if has_frontier_axis else distinct_challenges)
            if minimum_frontier_tasks
            else max(
                distinct_challenges,
                sum(int(axis.evidence_count or 0) for axis in axis_deltas),
            )
        )
        required_pairs = _minimum_pairs(contract)
        if required_pairs and n_pairs < required_pairs:
            return self._decision(comparison, "abstain", contract=contract, reason_codes=["insufficient_evidence"])

        quality = self._aggregate_quality(axis_deltas, contract=contract)
        regressed_axes = []
        for axis in axis_deltas:
            regression_tolerance = _axis_regression_tolerance(contract, axis.axis_id, self.config.protected_regression_tolerance)
            protected_bound = float(comparison.protected_axis_bounds.get(axis.axis_id, axis.lower))
            if min(axis.lower, protected_bound) < -regression_tolerance:
                regressed_axes.append(axis.axis_id)
        if regressed_axes:
            return self._decision(
                comparison,
                "reject",
                contract=contract,
                regressed_axes=regressed_axes,
                reason_codes=["protected_axis_regression"],
            )

        capability_axes = [
            axis
            for axis in axis_deltas
            if str(axis.promotion_kind) == "capability"
            and _quality_axis_promotion_eligible(contract, axis.axis_id)
        ]
        improved_axes = [
            axis.axis_id
            for axis in capability_axes
            if axis.estimate > _axis_epsilon(contract, axis.axis_id, self.config.capability_epsilon)
        ]
        capability_lcb_not_cleared = False
        if improved_axes:
            capability_quality = self._aggregate_quality(capability_axes, contract=contract)
            if capability_quality.lower <= self.config.capability_epsilon:
                capability_lcb_not_cleared = True
            else:
                return self._decision(
                    comparison,
                    "capability",
                    contract=contract,
                    improved_axes=improved_axes,
                    reason_codes=["suite_quality_win"] if _is_implicit_suite_contract(contract) else ["frontier_quality_win"],
                    quality_override=capability_quality,
                )

        quality_equivalent = quality.lower >= -self.config.equivalence_tolerance
        efficiency_delta = self._best_efficiency_delta(comparison)
        efficiency_epsilon = _efficiency_axis_epsilon(contract, efficiency_delta.axis_id, self.config.efficiency_epsilon)
        if comparison.efficiency_deltas and quality_equivalent and efficiency_delta.lower > efficiency_epsilon:
            reasons = ["quality_equivalent"]
            if static_suite_saturated:
                reasons.append("static_suite_saturated")
            reasons.append("efficiency_lcb_cleared")
            return self._decision(comparison, "efficiency", contract=contract, reason_codes=reasons)
        if capability_lcb_not_cleared:
            return self._decision(comparison, "no_progress", contract=contract, reason_codes=["capability_lcb_not_cleared"])

        subskill_axes = [
            axis
            for axis in axis_deltas
            if str(axis.promotion_kind) == "subskill"
            and _quality_axis_promotion_eligible(contract, axis.axis_id)
        ]
        improved_subskill_axes = [
            axis.axis_id
            for axis in subskill_axes
            if axis.estimate > _axis_epsilon(contract, axis.axis_id, self.config.capability_epsilon)
        ]
        if improved_subskill_axes:
            subskill_quality = self._aggregate_quality(subskill_axes, contract=contract)
            if subskill_quality.lower > self.config.capability_epsilon:
                return self._decision(
                    comparison,
                    "subskill",
                    contract=contract,
                    improved_axes=improved_subskill_axes,
                    reason_codes=["suite_quality_win"],
                    quality_override=subskill_quality,
                )

        saturated = static_suite_saturated or all(axis.saturated for axis in axis_deltas)
        if saturated and not frontier_evidence_available:
            return self._decision(
                comparison,
                "abstain",
                contract=contract,
                no_capability_signal_reason="all quality axes saturated; expand frontier challenges",
                reason_codes=["static_suite_saturated", "expand_frontier_challenges", "no_capability_signal"],
            )
        if saturated:
            return self._decision(
                comparison,
                "no_progress",
                contract=contract,
                no_capability_signal_reason="all quality axes saturated",
                reason_codes=["quality_saturated", "no_capability_signal"],
            )
        return self._decision(comparison, "no_progress", contract=contract, reason_codes=["quality_lcb_not_cleared"])

    def _quality_axis_deltas(
        self,
        parent: SuiteEvaluation,
        child: SuiteEvaluation,
        *,
        contract: DomainEvidenceContract | None = None,
    ) -> tuple[dict[str, AxisDelta], list[str], dict[str, list[str]]]:
        parent_runs = self._runs_by_task(parent.run_results)
        child_runs = self._runs_by_task(child.run_results)
        axis_scores: dict[str, dict[str, Any]] = {}
        challenge_ids: set[str] = set()
        for task_id in sorted(set(parent_runs) & set(child_runs)):
            metadata = self._task_metadata(parent, child, task_id)
            axis_id, promotion_kind = self._axis_for_task(contract, task_id, metadata)
            parent_scores = [self._quality_score(run) for run in parent_runs[task_id]]
            child_scores = [self._quality_score(run) for run in child_runs[task_id]]
            pair_count = min(len(parent_scores), len(child_scores))
            if pair_count <= 0:
                continue
            source = self._axis_source(metadata)
            bucket = axis_scores.setdefault(
                axis_id,
                {
                    "parent_scores": [],
                    "child_scores": [],
                    "sources": set(),
                    "promotion_kind": promotion_kind,
                    "task_ids": set(),
                },
            )
            bucket["parent_scores"].extend(parent_scores[:pair_count])
            bucket["child_scores"].extend(child_scores[:pair_count])
            bucket["sources"].add(source)
            bucket["task_ids"].add(task_id)
            challenge_ids.add(task_id)

        axis_deltas: dict[str, AxisDelta] = {}
        axis_task_ids: dict[str, list[str]] = {}
        for axis_id, bucket in sorted(axis_scores.items()):
            parent_scores = bucket["parent_scores"]
            child_scores = bucket["child_scores"]
            parent_quality = mean(parent_scores)
            child_quality = mean(child_scores)
            saturated = parent_quality >= self.config.saturation_threshold and child_quality >= self.config.saturation_threshold
            sources = set(bucket["sources"])
            source = "frontier" if "frontier" in sources else "static_exact"
            if saturated:
                effect = PairedEffect(estimate=0.0, lower=0.0, upper=0.0, n_eff=float(min(len(parent_scores), len(child_scores))))
            else:
                effect = paired_mean_effect(child_scores, parent_scores, confidence_z=self.config.confidence_z, singleton_margin=1.0)
            axis_delta = AxisDelta(
                axis_id=axis_id,
                promotion_kind=bucket["promotion_kind"],
                estimate=effect.estimate,
                lower=effect.lower,
                upper=effect.upper,
                evidence_count=min(len(parent_scores), len(child_scores)),
                authority_level="A4",
                source=source,
                saturated=saturated,
                reason_codes=["saturated_exact_verifier"] if saturated else [],
            )
            task_ids = sorted(bucket["task_ids"])
            axis_task_ids[axis_id] = task_ids
            axis_deltas[axis_id] = axis_delta
        return axis_deltas, sorted(challenge_ids), axis_task_ids

    def _axis_for_task(
        self,
        contract: DomainEvidenceContract | None,
        task_id: str,
        metadata: Mapping[str, Any],
    ) -> tuple[str, str]:
        if contract is None:
            return task_id, "capability"
        allow_wildcard = len(contract.quality_axes) == 1
        for axis in contract.quality_axes:
            if _axis_matches_task(axis, task_id, metadata, allow_wildcard=allow_wildcard):
                axis_id = _quality_axis_id(axis)
                if axis_id:
                    return axis_id, _quality_axis_promotion_kind(axis)
        return task_id, "subskill"

    @staticmethod
    def _task_metadata(parent: SuiteEvaluation, child: SuiteEvaluation, task_id: str) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        metadata.update(dict(parent.task_metadata.get(task_id, {}) or {}))
        metadata.update(dict(child.task_metadata.get(task_id, {}) or {}))
        return metadata

    @staticmethod
    def _axis_source(metadata: Mapping[str, Any]) -> str:
        slice_tags = {str(tag) for tag in metadata.get("slice_tags", [])}
        if str(metadata.get("domain_kind", "")) == "generated_tool_workflow" or "frontier" in slice_tags:
            return "frontier"
        return "static_exact"

    def _efficiency_effect(self, parent: SuiteEvaluation, child: SuiteEvaluation) -> PairedEffect:
        return self._efficiency_metric_effect(parent, child, metric="runtime_efficiency", lower_is_better=True)

    def _efficiency_metric_effect(
        self,
        parent: SuiteEvaluation,
        child: SuiteEvaluation,
        *,
        metric: str,
        lower_is_better: bool,
    ) -> PairedEffect:
        parent_runs = self._runs_by_task(parent.run_results)
        child_runs = self._runs_by_task(child.run_results)
        parent_scores: list[float] = []
        child_scores: list[float] = []
        for task_id in sorted(set(parent_runs) & set(child_runs)):
            if metric == "runtime_efficiency":
                parent_burden = mean([self._resource_burden(run) for run in parent_runs[task_id]])
                child_burden = mean([self._resource_burden(run) for run in child_runs[task_id]])
            else:
                parent_burden = mean([self._run_efficiency_metric(run, metric) for run in parent_runs[task_id]])
                child_burden = mean([self._run_efficiency_metric(run, metric) for run in child_runs[task_id]])
            baseline = max(1.0, parent_burden)
            parent_scores.append(0.0)
            delta = (parent_burden - child_burden) / baseline
            child_scores.append(delta if lower_is_better else -delta)
        return paired_mean_effect(child_scores, parent_scores, confidence_z=self.config.confidence_z, singleton_margin=0.0)

    def _efficiency_deltas(
        self,
        parent: SuiteEvaluation,
        child: SuiteEvaluation,
        *,
        contract: DomainEvidenceContract | None,
    ) -> dict[str, dict[str, Any]]:
        if contract is None:
            efficiency = self._efficiency_effect(parent, child)
            return {
                "runtime_efficiency": {
                    "estimate": efficiency.estimate,
                    "lower": efficiency.lower,
                    "upper": efficiency.upper,
                    "authority_level": "A3",
                }
            }
        supported_metrics = {"cost", "latency", "tokens", "faults"}
        deltas: dict[str, dict[str, Any]] = {}
        for axis in contract.efficiency_axes:
            axis_id = _efficiency_axis_id(axis)
            metric = _efficiency_axis_metric(axis)
            if not axis_id or metric not in supported_metrics:
                continue
            if _efficiency_axis_comparator_type(axis) != "exact_outcome":
                continue
            effect = self._efficiency_metric_effect(
                parent,
                child,
                metric=metric,
                lower_is_better=_efficiency_axis_lower_is_better(axis),
            )
            deltas[axis_id] = {
                "estimate": effect.estimate,
                "lower": effect.lower,
                "upper": effect.upper,
                "authority_level": "A3",
            }
        return deltas

    @staticmethod
    def _run_efficiency_metric(run: RunResult, metric: str) -> float:
        if metric == "cost":
            return max(0.0, float(run.cost))
        if metric == "latency":
            return max(0.0, float(run.latency))
        if metric == "tokens":
            return max(0.0, float(run.tokens_used or (run.input_tokens + run.output_tokens) or 0.0))
        if metric == "faults":
            return max(0.0, float(run.faults))
        return 0.0

    def _decision(
        self,
        comparison: PairedComparison,
        decision_type: str,
        *,
        contract: DomainEvidenceContract | None = None,
        improved_axes: Sequence[str] = (),
        regressed_axes: Sequence[str] = (),
        no_capability_signal_reason: str = "",
        reason_codes: Sequence[str],
        quality_override: PairedEffect | None = None,
    ) -> PromotionDecision:
        axis_deltas = list(comparison.axis_deltas.values())
        quality = quality_override or self._aggregate_quality(axis_deltas, contract=contract)
        efficiency = self._best_efficiency_delta(comparison)
        tied_axes = [
            axis.axis_id
            for axis in axis_deltas
            if axis.axis_id not in set(improved_axes) and axis.axis_id not in set(regressed_axes)
        ]
        progress_signal = ProgressSignal(
            signal_id=stable_hash("progress-signal", comparison.comparison_id, decision_type)[:24],
            parent_runtime_hash=comparison.parent_runtime_hash,
            child_runtime_hash=comparison.child_runtime_hash,
            contract_id=contract.contract_id if contract is not None else comparison.contract_id,
            oracle_package_hash=comparison.oracle_package_hash,
            parent_oracle_package_hash=comparison.parent_oracle_package_hash,
            child_oracle_package_hash=comparison.child_oracle_package_hash,
            parent_runtime_spec_digest=comparison.parent_runtime_spec_digest,
            child_runtime_spec_digest=comparison.child_runtime_spec_digest,
            decision_type=decision_type,
            capability_signal=CapabilitySignal(
                quality_delta_estimate=quality.estimate,
                quality_delta_lower=quality.lower,
                quality_delta_upper=quality.upper,
                axis_ids=list(improved_axes),
            )
            if decision_type == PromotionDecisionType.CAPABILITY.value
            else None,
            efficiency_signal=EfficiencySignal(
                quality_equivalent=quality.lower >= -self.config.equivalence_tolerance,
                quality_delta_lower=quality.lower,
                cost_delta_lower=efficiency.lower,
                latency_delta_lower=efficiency.lower,
                token_delta_lower=efficiency.lower,
                axis_ids=[efficiency.axis_id] if efficiency.axis_id else [],
            )
            if decision_type == PromotionDecisionType.EFFICIENCY.value
            else None,
            subskill_signal=SubskillSignal(
                subskill_id=contract.contract_id if contract is not None else comparison.contract_id,
                quality_delta_estimate=quality.estimate,
                quality_delta_lower=quality.lower,
                full_task_passed=quality.lower > self.config.capability_epsilon,
                axis_ids=list(improved_axes),
            )
            if decision_type == PromotionDecisionType.SUBSKILL.value
            else None,
            quality_delta_estimate=quality.estimate,
            quality_delta_lower=quality.lower,
            quality_delta_upper=quality.upper,
            efficiency_delta_estimate=efficiency.estimate,
            efficiency_delta_lower=efficiency.lower,
            efficiency_delta_upper=efficiency.upper,
            improved_axes=list(improved_axes),
            regressed_axes=list(regressed_axes),
            tied_axes=tied_axes,
            comparison_count=len(axis_deltas),
            n_eff=quality.n_eff,
            authority_summary={"offline_deterministic": 1.0, "llm_judge": 0.0},
            evidence_digest=comparison.evidence_digest,
            reason_codes=list(reason_codes),
            pairwise_comparisons=[comparison],
            no_capability_signal_reason=no_capability_signal_reason,
        )
        allowed, forbidden = self._optimizer_updates(decision_type)
        winning_runtime_hash = comparison.child_runtime_hash if decision_type in {"capability", "efficiency", "preference", "subskill"} else ""
        return PromotionDecision(
            decision_id=stable_hash("promotion-decision", comparison.comparison_id, decision_type, reason_codes)[:24],
            decision_type=decision_type,
            contract_id=contract.contract_id if contract is not None else comparison.contract_id,
            scope=contract.scope if contract is not None else {},
            winning_runtime_hash=winning_runtime_hash,
            parent_runtime_hash=comparison.parent_runtime_hash,
            child_runtime_hash=comparison.child_runtime_hash,
            oracle_package_hash=comparison.oracle_package_hash,
            parent_oracle_package_hash=comparison.parent_oracle_package_hash,
            child_oracle_package_hash=comparison.child_oracle_package_hash,
            parent_runtime_spec_digest=comparison.parent_runtime_spec_digest,
            child_runtime_spec_digest=comparison.child_runtime_spec_digest,
            comparison_ref=comparison.comparison_id,
            progress_signal_ref=progress_signal.signal_id,
            progress_signal=progress_signal,
            allowed_optimizer_updates=allowed,
            forbidden_optimizer_updates=forbidden,
            reason_codes=list(reason_codes),
            alpha_spent=comparison.alpha_spent,
            evidence_refs=list(comparison.evidence_refs),
            quality_delta_lower=quality.lower,
            quality_delta_estimate=quality.estimate,
            efficiency_delta_lower=efficiency.lower,
            efficiency_delta_estimate=efficiency.estimate,
            evidence_digest=comparison.evidence_digest,
        )

    def _signal_from_decision(self, decision: PromotionDecision, comparison: PairedComparison) -> ProgressSignal:
        if decision.progress_signal is not None:
            return decision.progress_signal
        return ProgressSignal(
            signal_id=stable_hash("progress-signal", decision.decision_id)[:24],
            parent_runtime_hash=comparison.parent_runtime_hash,
            child_runtime_hash=comparison.child_runtime_hash,
            contract_id=decision.contract_id,
            oracle_package_hash=comparison.oracle_package_hash,
            parent_oracle_package_hash=comparison.parent_oracle_package_hash,
            child_oracle_package_hash=comparison.child_oracle_package_hash,
            parent_runtime_spec_digest=comparison.parent_runtime_spec_digest,
            child_runtime_spec_digest=comparison.child_runtime_spec_digest,
            decision_type=decision.decision_type,
            quality_delta_estimate=decision.quality_delta_estimate or 0.0,
            quality_delta_lower=decision.quality_delta_lower or 0.0,
            efficiency_delta_estimate=decision.efficiency_delta_estimate or 0.0,
            efficiency_delta_lower=decision.efficiency_delta_lower or 0.0,
            reason_codes=list(decision.reason_codes),
            pairwise_comparisons=[comparison],
        )

    def _aggregate_quality(
        self,
        axis_deltas: Sequence[AxisDelta],
        *,
        contract: DomainEvidenceContract | None = None,
    ) -> PairedEffect:
        if not axis_deltas:
            return PairedEffect(estimate=0.0, lower=0.0, upper=0.0, n_eff=0.0)
        weighted_axes = [
            (
                axis,
                max(0.0, float(axis.evidence_count or 0)) * _quality_axis_weight(contract, axis.axis_id, 1.0),
            )
            for axis in axis_deltas
        ]
        total_weight = sum(weight for _, weight in weighted_axes)
        if total_weight <= 0.0:
            total_weight = float(len(axis_deltas))
            weighted_axes = [(axis, 1.0) for axis in axis_deltas]
        estimate = sum(axis.estimate * weight for axis, weight in weighted_axes) / total_weight
        lower = sum(axis.lower * weight for axis, weight in weighted_axes) / total_weight
        upper = sum(axis.upper * weight for axis, weight in weighted_axes) / total_weight
        return PairedEffect(
            estimate=estimate,
            lower=max(-1.0, lower),
            upper=min(1.0, upper),
            n_eff=float(sum(max(0, axis.evidence_count) for axis in axis_deltas)),
        )

    def _best_efficiency_delta(self, comparison: PairedComparison) -> EfficiencyDelta:
        if not comparison.efficiency_deltas:
            return EfficiencyDelta(axis_id="", estimate=0.0, lower=0.0, upper=0.0)
        return max(comparison.efficiency_deltas.values(), key=lambda delta: delta.lower)

    def _implicit_contract(self, comparison: PairedComparison) -> DomainEvidenceContract:
        min_pairs = 1 if len(comparison.axis_deltas) <= 1 else self.config.min_quality_comparisons
        frontier_available = any(_is_frontier_source(delta.source) for delta in comparison.axis_deltas.values())
        return DomainEvidenceContract(
            contract_id=comparison.contract_id or "implicit_suite_progress_contract",
            domain_kind="benchmark_suite",
            version="implicit",
            scope={"domain": "benchmark_suite", "allowed_claim_language": ["current suite only"]},
            challenge_distribution={
                ("minimum_frontier_tasks" if frontier_available else "minimum_pairs"): min_pairs,
                "frontier_available": frontier_available,
            },
            answer_mechanism={"type": "runtime_verifier", "authority": "A4"},
            quality_axes=[
                {
                    "axis_id": axis.axis_id,
                    "promotion_kind": "subskill" if frontier_available else "capability",
                    "epsilon": self.config.capability_epsilon,
                    "protected_regression_tolerance": self.config.protected_regression_tolerance,
                }
                for axis in comparison.axis_deltas.values()
            ],
            efficiency_axes=[{"axis_id": "runtime_efficiency", "promotion_kind": "efficiency", "epsilon": self.config.efficiency_epsilon}],
            health_floors={"verifier": "pass", "leakage": "pass"},
            leakage_policy={
                "status_required": True,
                "attestation": "suite_scores_from_runtime_visible_tasks_and_private_rescore_when_available",
            },
            statistical_rule={"type": "fixed_confirmatory", "minimum_pairs": min_pairs, "alpha": 0.05},
        )

    @staticmethod
    def _optimizer_updates(decision_type: str) -> tuple[list[str], list[str]]:
        if decision_type == "capability":
            return (
                ["capability_archive", "capability_scheduler", "capability_predictors", "capability_priors"],
                ["efficiency_archive"],
            )
        if decision_type == "efficiency":
            return (
                ["efficiency_archive", "efficiency_predictors"],
                ["capability_archive", "capability_scheduler", "capability_predictors", "capability_priors"],
            )
        if decision_type == "subskill":
            return (
                ["subskill_archive", "subskill_scheduler", "subskill_predictors"],
                ["capability_archive", "capability_predictors", "capability_priors", "efficiency_archive"],
            )
        if decision_type == "preference":
            return (
                ["preference_archive", "preference_model"],
                ["capability_archive", "capability_scheduler", "capability_predictors", "capability_priors"],
            )
        return (
            ["diagnostic_log", "diagnostic_predictors"] if decision_type in {"abstain", "no_progress"} else ["hard_failure_stats", "diagnostic_predictors"],
            ["capability_archive", "capability_scheduler", "capability_predictors", "capability_priors", "efficiency_archive", "efficiency_predictors"],
        )

    @staticmethod
    def _quality_score(run: RunResult) -> float:
        if run.hard_invalid:
            return 0.0
        return float(run.verifier_score)

    @staticmethod
    def _resource_burden(run: RunResult) -> float:
        token_count = run.tokens_used or (run.input_tokens + run.output_tokens)
        return (
            max(0.0, float(run.cost))
            + max(0.0, float(run.latency)) / 1000.0
            + max(0.0, float(token_count)) / 1000.0
            + max(0.0, float(run.faults))
        )

    @staticmethod
    def _runs_by_task(runs: Sequence[RunResult]) -> dict[str, list[RunResult]]:
        grouped: dict[str, list[RunResult]] = defaultdict(list)
        for run in runs:
            grouped[run.task_id].append(run)
        return grouped
