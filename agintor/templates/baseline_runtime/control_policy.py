from __future__ import annotations

import math
from typing import Any, Sequence

from agintor.archive import ScopeScheduler


class ControlPolicy:
    MODEL_ORDER = ["small", "medium", "large"]
    MODEL_SPECS = {
        "small": {"solve": 0.60, "cost": 0.10, "latency": 0.10, "dollar": 0.10, "fail": 0.12},
        "medium": {"solve": 0.74, "cost": 0.20, "latency": 0.16, "dollar": 0.18, "fail": 0.08},
        "large": {"solve": 0.85, "cost": 0.35, "latency": 0.25, "dollar": 0.32, "fail": 0.04},
    }

    def _model_rank(self, model_name: str) -> int:
        try:
            return self.MODEL_ORDER.index(model_name)
        except ValueError:
            return 0

    def _next_model(self, model_name: str) -> str:
        rank = min(len(self.MODEL_ORDER) - 1, self._model_rank(model_name) + 1)
        return self.MODEL_ORDER[rank]

    def _is_affordable(self, ctx, model_name: str) -> bool:
        spec = self.MODEL_SPECS.get(model_name)
        if spec is None:
            return False
        remaining = 1.0 - max(ctx.budget.normalized().values())
        return remaining >= spec["cost"]

    def assign_model(self, ctx, operation, frame) -> str:
        required = 0.60 + 0.10 * (operation.kind == "generated_expression") + 0.05 * bool(operation.dependencies)
        remaining = 1.0 - max(ctx.budget.normalized().values())
        qualifying: list[tuple[float, float, float, str]] = []
        affordable: list[tuple[float, float, float, str]] = []
        for model_name, spec in self.MODEL_SPECS.items():
            utility = spec["solve"] - 0.20 * spec["cost"] - 0.15 * spec["latency"] - 0.10 * spec["dollar"] - 0.20 * spec["fail"]
            if remaining < spec["cost"]:
                continue
            affordable.append((spec["cost"], spec["latency"], -utility, model_name))
            if spec["solve"] >= required:
                qualifying.append((spec["cost"], spec["latency"], -utility, model_name))
        selected = min(qualifying)[-1] if qualifying else min(affordable)[-1] if affordable else "small"
        subgoal_key = getattr(operation, "output_key", operation.op_id)
        negative_steps = ctx.state.subgoal_negative_steps.get(subgoal_key, 0)
        if negative_steps >= 2:
            prior_model = ctx.state.subgoal_last_model.get(subgoal_key, selected)
            escalated = max(selected, self._next_model(prior_model), key=self._model_rank)
            if self._is_affordable(ctx, escalated):
                selected = escalated
        ctx.state.subgoal_last_model[subgoal_key] = selected
        return selected

    def request_checks(self, ctx, artifact, exact_verifier_exists: bool, irreversible: bool, external_visible: bool) -> list[str]:
        evidence_size = len(str(artifact))
        unresolved = len(ctx.state.unresolved_goals)
        local_voi = 0.30 * (evidence_size > 0) - 0.04
        subtree_voi = 0.22 * bool(unresolved) - 0.06
        repo_voi = 0.18 * bool(external_visible or irreversible) - 0.08
        bench_voi = 0.65 * exact_verifier_exists * (irreversible or external_visible or unresolved == 0) - 0.08
        checks: list[str] = []
        ladder = [
            ("local", local_voi, evidence_size > 0),
            ("subtree", subtree_voi, unresolved > 0 or external_visible),
            ("repo", repo_voi, external_visible or irreversible),
            ("benchmark", bench_voi, exact_verifier_exists and (external_visible or irreversible or unresolved == 0)),
        ]
        must_run_benchmark = exact_verifier_exists and (external_visible or irreversible)
        for checker, voi, eligible in ladder:
            if not eligible or voi <= 0:
                continue
            checks.append(checker)
            if checker == "benchmark":
                break
            if not (external_visible or irreversible or unresolved > 0 or must_run_benchmark):
                break
        if must_run_benchmark and "benchmark" not in checks:
            checks.append("benchmark")
        return checks

    def stop_policy(self, ctx, best_optimistic_utility: float, previous_best_utility: float, unresolved_count: int, verified_terminal: bool) -> bool:
        if verified_terminal and unresolved_count == 0:
            return True
        if ctx.budget.exhausted():
            return True
        if best_optimistic_utility < 0 and previous_best_utility < 0 and unresolved_count == 0:
            return verified_terminal
        return False

    def score_interface_scope(self, ctx, scope: Sequence[str], parent_eval, child_eval) -> float:
        if parent_eval is None or child_eval is None:
            return 0.0
        delta = child_eval.objective_scores.get("sbar:global", 0.0) - parent_eval.objective_scores.get("sbar:global", 0.0)
        return delta / max(1, len(scope))

    def update_scope_credit(self, ctx, scheduler: ScopeScheduler, objective: str, scope: Sequence[str], delta: float) -> None:
        scheduler.update_scope_credit(objective, scope, delta)
