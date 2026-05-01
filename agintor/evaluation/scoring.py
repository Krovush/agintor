from __future__ import annotations

import math
from collections import defaultdict
from typing import Dict, Iterable, List, Mapping, Sequence

from ..contracts import RunResult, SuiteEvaluation, TaskScore
from ..utils import EPS, lower_cvar, mean, median, safe_log1p_ratio, std_error, variance


DEFAULT_LAMBDAS = {
    "cost": 0.08,
    "latency": 0.05,
    "fault": 0.12,
}

DEFAULT_ROBUSTNESS = {
    "eta_sigma": 0.35,
    "kappa_b": 0.25,
    "kappa_u": 0.30,
    "alpha": 1.0 / 3.0,
}


class ScoreCalculator:
    def __init__(
        self,
        baseline_costs: Mapping[str, float] | None = None,
        baseline_latencies: Mapping[str, float] | None = None,
        family_weights: Mapping[str, float] | None = None,
        lambdas: Mapping[str, float] | None = None,
        robustness: Mapping[str, float] | None = None,
        prior_variances: Mapping[str, float] | None = None,
    ) -> None:
        self.baseline_costs = dict(baseline_costs or {})
        self.baseline_latencies = dict(baseline_latencies or {})
        self.family_weights = dict(family_weights or {"top": 0.25, "mem": 0.25, "tool": 0.25, "e2e": 0.25})
        self.lambdas = dict(DEFAULT_LAMBDAS)
        self.lambdas.update(lambdas or {})
        self.robustness = dict(DEFAULT_ROBUSTNESS)
        self.robustness.update(robustness or {})
        self.prior_variances = defaultdict(lambda: 0.05)
        self.prior_variances.update(prior_variances or {})

    def reference_cost(self, task_id: str) -> float:
        return max(1.0, float(self.baseline_costs.get(task_id, 1.0)))

    def reference_latency(self, task_id: str) -> float:
        return max(1.0, float(self.baseline_latencies.get(task_id, 1.0)))

    def utility(self, run: RunResult) -> float:
        ref_c = self.reference_cost(run.task_id)
        ref_l = self.reference_latency(run.task_id)
        utility = (
            run.verifier_score
            - self.lambdas["cost"] * safe_log1p_ratio(run.cost, ref_c)
            - self.lambdas["latency"] * safe_log1p_ratio(run.latency, ref_l)
            - self.lambdas["fault"] * float(run.faults)
        )
        return float(utility)

    def task_score(self, family: str, runs: Sequence[RunResult]) -> TaskScore:
        utilities = [self.utility(run) for run in runs]
        for run, utility in zip(runs, utilities):
            run.utility = utility
        s = mean(utilities)
        sample_var = variance(utilities)
        eta_sigma = self.robustness["eta_sigma"]
        prior = float(self.prior_variances[family])
        sigma2_hat = (1.0 - eta_sigma) * sample_var + eta_sigma * prior
        sigma_hat = math.sqrt(max(EPS, sigma2_hat))
        r_count = max(1, len(utilities))
        rho = s - self.robustness["kappa_b"] * sigma_hat - self.robustness["kappa_u"] * sigma_hat / math.sqrt(r_count)
        cvar = lower_cvar(utilities, self.robustness["alpha"])
        return TaskScore(
            s=float(s),
            rho=float(rho),
            cvar=float(cvar),
            utilities=list(utilities),
            verifier_scores=[run.verifier_score for run in runs],
            costs=[run.cost for run in runs],
            latencies=[run.latency for run in runs],
            faults=[run.faults for run in runs],
        )

    def suite_score(self, runtime_hash: str, task_family_map: Mapping[str, str], runs: Sequence[RunResult]) -> SuiteEvaluation:
        grouped: Dict[str, List[RunResult]] = defaultdict(list)
        for run in runs:
            grouped[run.task_id].append(run)
        task_scores = {task_id: self.task_score(task_family_map[task_id], task_runs) for task_id, task_runs in grouped.items()}
        family_scores: Dict[str, Dict[str, float]] = defaultdict(dict)
        family_to_scores_s: Dict[str, List[float]] = defaultdict(list)
        family_to_scores_rho: Dict[str, List[float]] = defaultdict(list)
        objective_scores: Dict[str, float] = {}
        for task_id, score in task_scores.items():
            family = task_family_map[task_id]
            family_to_scores_s[family].append(score.s)
            family_to_scores_rho[family].append(score.rho)
            objective_scores[f"s:{task_id}"] = score.s
        for family in sorted(family_to_scores_s):
            family_scores[family]["s"] = mean(family_to_scores_s[family])
            family_scores[family]["rho"] = mean(family_to_scores_rho[family])
            objective_scores[f"sbar:{family}"] = family_scores[family]["s"]
            objective_scores[f"rhobar:{family}"] = family_scores[family]["rho"]
        global_s = 0.0
        global_rho = 0.0
        for family, weight in self.family_weights.items():
            global_s += weight * family_scores.get(family, {}).get("s", 0.0)
            global_rho += weight * family_scores.get(family, {}).get("rho", 0.0)
        objective_scores["sbar:global"] = global_s
        objective_scores["rhobar:global"] = global_rho
        invalid = any(run.hard_invalid for run in runs)
        return SuiteEvaluation(
            runtime_hash=runtime_hash,
            objective_scores=objective_scores,
            task_scores=task_scores,
            family_scores=dict(family_scores),
            run_results=list(runs),
            invalid=invalid,
        )



def estimate_reference_scales(runs: Sequence[RunResult]) -> tuple[Dict[str, float], Dict[str, float]]:
    by_task_cost: Dict[str, List[float]] = defaultdict(list)
    by_task_latency: Dict[str, List[float]] = defaultdict(list)
    for run in runs:
        by_task_cost[run.task_id].append(run.cost)
        by_task_latency[run.task_id].append(run.latency)
    return (
        {task_id: max(1.0, median(values)) for task_id, values in by_task_cost.items()},
        {task_id: max(1.0, median(values)) for task_id, values in by_task_latency.items()},
    )



def mean_improvement(child_scores: Sequence[float], parent_scores: Sequence[float]) -> tuple[float, float, float]:
    if len(child_scores) != len(parent_scores):
        raise ValueError("score lengths mismatch")
    deltas = [c - p for c, p in zip(child_scores, parent_scores)]
    avg = mean(deltas)
    se = std_error(deltas)
    lcb = avg - 1.0 * se
    return avg, se, lcb
