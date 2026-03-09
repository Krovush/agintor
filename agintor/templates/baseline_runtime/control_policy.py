from __future__ import annotations

import math
from typing import Any, Sequence

from agintor.archive import ScopeScheduler


class ControlPolicy:
    MODEL_SPECS = {
        "small": {"solve": 0.60, "cost": 0.10, "latency": 0.10, "dollar": 0.10, "fail": 0.12},
        "medium": {"solve": 0.74, "cost": 0.20, "latency": 0.16, "dollar": 0.18, "fail": 0.08},
        "large": {"solve": 0.85, "cost": 0.35, "latency": 0.25, "dollar": 0.32, "fail": 0.04},
    }

    def assign_model(self, ctx, operation, frame) -> str:
        required = 0.60 + 0.10 * (operation.kind == "generated_expression") + 0.05 * bool(operation.dependencies)
        remaining = 1.0 - max(ctx.budget.normalized().values())
        best = "small"
        best_score = -1e9
        for model_name, spec in self.MODEL_SPECS.items():
            utility = spec["solve"] - 0.20 * spec["cost"] - 0.15 * spec["latency"] - 0.10 * spec["dollar"] - 0.20 * spec["fail"]
            if spec["solve"] >= required and utility > best_score and remaining > spec["cost"]:
                best = model_name
                best_score = utility
        return best

    def request_checks(self, ctx, artifact, exact_verifier_exists: bool, irreversible: bool, external_visible: bool) -> list[str]:
        checks = []
        evidence_size = len(str(artifact))
        local_voi = 0.35 * (evidence_size > 0) - 0.05 - 0.02
        subtree_voi = 0.25 * bool(ctx.state.unresolved_goals) - 0.08 - 0.03
        repo_voi = 0.10 * external_visible - 0.10 - 0.08
        bench_voi = 0.70 * exact_verifier_exists * (irreversible or external_visible) - 0.10 - 0.08
        if local_voi > 0:
            checks.append("local")
        if subtree_voi > 0 and not checks:
            checks.append("subtree")
        if repo_voi > 0 and not checks:
            checks.append("repo")
        if bench_voi > 0 or (exact_verifier_exists and external_visible):
            checks.append("benchmark")
        return checks

    def stop_policy(self, ctx, best_optimistic_utility: float, previous_best_utility: float, unresolved_count: int, verified_terminal: bool) -> bool:
        if verified_terminal:
            return True
        if ctx.budget.exhausted():
            return True
        return best_optimistic_utility < 0 and previous_best_utility < 0 and unresolved_count == 0 and verified_terminal

    def score_interface_scope(self, ctx, scope: Sequence[str], parent_eval, child_eval) -> float:
        if parent_eval is None or child_eval is None:
            return 0.0
        delta = child_eval.objective_scores.get("sbar:global", 0.0) - parent_eval.objective_scores.get("sbar:global", 0.0)
        return delta / max(1, len(scope))

    def update_scope_credit(self, ctx, scheduler: ScopeScheduler, objective: str, scope: Sequence[str], delta: float) -> None:
        scheduler.update_scope_credit(objective, scope, delta)
