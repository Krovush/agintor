from __future__ import annotations

class ControlPolicy:
    MODEL_ORDER = ["small", "medium", "large"]
    MODEL_SPECS = {
        "small": {"solve": 0.60, "cost": 0.10, "latency": 0.10, "dollar": 0.10, "fail": 0.12},
        "medium": {"solve": 0.74, "cost": 0.20, "latency": 0.16, "dollar": 0.18, "fail": 0.08},
        "large": {"solve": 0.85, "cost": 0.35, "latency": 0.25, "dollar": 0.32, "fail": 0.04},
    }

    def _model_order(self, ctx) -> list[str]:
        return list(ctx.profile.control.model_order or self.MODEL_ORDER)

    def _model_specs(self, ctx) -> dict[str, dict[str, float]]:
        return dict(ctx.profile.control.model_specs or self.MODEL_SPECS)

    def _model_rank(self, ctx, model_name: str) -> int:
        try:
            return self._model_order(ctx).index(model_name)
        except ValueError:
            return 0

    def _next_model(self, ctx, model_name: str) -> str:
        order = self._model_order(ctx)
        rank = min(len(order) - 1, self._model_rank(ctx, model_name) + 1)
        return order[rank]

    def _is_affordable(self, ctx, model_name: str) -> bool:
        spec = self._model_specs(ctx).get(model_name)
        if spec is None:
            return False
        remaining = 1.0 - max(ctx.budget.normalized().values())
        return remaining >= spec["cost"]

    def assign_model(self, ctx, operation, frame) -> str:
        required_cfg = ctx.profile.control.required_solve
        weights = ctx.profile.control.assign_model_weights
        required = (
            required_cfg["base"]
            + required_cfg["generated_bonus"] * (operation.kind == "generated_expression")
            + required_cfg["dependency_bonus"] * bool(operation.dependencies)
        )
        remaining = 1.0 - max(ctx.budget.normalized().values())
        qualifying: list[tuple[float, float, float, float, float, str]] = []
        affordable: list[tuple[float, float, float, float, float, str]] = []
        for model_name in self._model_order(ctx):
            spec = self._model_specs(ctx).get(model_name)
            if spec is None:
                continue
            utility = (
                spec["solve"]
                - weights["cost"] * spec["cost"]
                - weights["latency"] * spec["latency"]
                - weights["dollar"] * spec["dollar"]
                - weights["fail"] * spec["fail"]
            )
            if remaining < spec["cost"]:
                continue
            affordable.append((utility, spec["solve"], -spec["cost"], -spec["latency"], -self._model_rank(ctx, model_name), model_name))
            if spec["solve"] >= required:
                qualifying.append((utility, spec["solve"], -spec["cost"], -spec["latency"], -self._model_rank(ctx, model_name), model_name))
        selected = max(qualifying)[-1] if qualifying else max(affordable)[-1] if affordable else "small"
        subgoal_key = getattr(operation, "output_key", operation.op_id)
        negative_steps = ctx.state.subgoal_negative_steps.get(subgoal_key, 0)
        if negative_steps >= ctx.profile.control.negative_steps_before_escalation:
            prior_model = ctx.state.subgoal_last_model.get(subgoal_key, selected)
            escalated = max(selected, self._next_model(ctx, prior_model), key=lambda model_name: self._model_rank(ctx, model_name))
            if self._is_affordable(ctx, escalated):
                selected = escalated
        ctx.state.subgoal_last_model[subgoal_key] = selected
        return selected

    def request_checks(self, ctx, artifact, exact_verifier_exists: bool, irreversible: bool, external_visible: bool) -> list[str]:
        voi = ctx.profile.control.check_voi
        evidence_size = len(str(artifact))
        unresolved = len(ctx.state.unresolved_goals)
        local_voi = voi["local_positive"] * (evidence_size > 0) + voi["local_bias"]
        subtree_voi = voi["subtree_positive"] * bool(unresolved) + voi["subtree_bias"]
        repo_voi = voi["repo_positive"] * bool(external_visible or irreversible) + voi["repo_bias"]
        bench_voi = (
            voi["benchmark_positive"] * exact_verifier_exists * (irreversible or external_visible or unresolved == 0)
            + voi["benchmark_bias"]
        )
        checks: list[str] = []
        ladder = [
            ("local", local_voi, evidence_size > 0),
            ("subtree", subtree_voi, unresolved > 0 or external_visible),
            ("repo", repo_voi, external_visible or irreversible),
            ("benchmark", bench_voi, exact_verifier_exists and (external_visible or irreversible or unresolved == 0)),
        ]
        must_run_benchmark = exact_verifier_exists and (external_visible or irreversible)
        for checker, checker_voi, eligible in ladder:
            if not eligible or checker_voi <= 0:
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
        require_verified_terminal = bool(ctx.profile.control.stop_policy.require_verified_terminal)
        if verified_terminal and unresolved_count == 0:
            return True
        if ctx.budget.exhausted():
            return True
        if best_optimistic_utility < 0 and previous_best_utility < 0 and unresolved_count == 0:
            return verified_terminal or not require_verified_terminal
        return False
