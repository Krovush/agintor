from __future__ import annotations

from typing import Any, Sequence

from agintor_runtime.schemas import Checkpoint, ChildSpec, SummaryRecord
from agintor_runtime.utils import jaccard, lexical_overlap


class TopologyPolicy:
    THETA_CREATE = 0.58
    K_MAX = 3
    SPAWN_PENALTY = 0.06
    COORD_PENALTY = 0.05
    DEP_PENALTY = 0.04
    SIZE_PENALTY = 0.03
    CONFLICT_PENALTY = 0.03
    COLDSTART_PENALTY = 0.02

    def score_agent(self, ctx, agent, child_spec: ChildSpec) -> float:
        task_repr = ctx.task.prompt.lower()
        description = agent.description.lower()
        tool_overlap = jaccard(agent.default_tool_scope, child_spec.tool_scope)
        cap_overlap = jaccard(agent.capability_set, child_spec.required_capabilities)
        historical_success = agent.success_stats.get(ctx.task.family, agent.success_stats.get("global", 0.0))
        reusefit = lexical_overlap(
            " ".join(child_spec.required_capabilities),
            " ".join(agent.capability_set + agent.symbol_set),
        )
        ctx_overhead = 0.02 * len(agent.default_tool_scope)
        staleness = 0.01 * agent.staleness_clock
        permission_gap = 0.10 if set(child_spec.required_permissions) - {"local"} else 0.0
        return (
            0.35 * lexical_overlap(description, task_repr)
            + 0.15 * tool_overlap
            + 0.20 * cap_overlap
            + 0.15 * historical_success
            + 0.15 * reusefit
            - ctx_overhead
            - staleness
            - permission_gap
        )

    def select_mode(self, ctx, frame, operations: Sequence[Any]) -> str:
        config = ctx.profile.topology
        op_count = len(operations)
        dependency_count = sum(len(op.dependencies) for op in operations)
        generated_count = sum(1 for op in operations if op.kind == "generated_expression")
        exact_verifier_hint = 1.0 if ctx.task.verification_required else 0.0
        context_saturation = min(1.0, len(ctx.shell.short_term.nodes) / 20.0)
        candidate_utilities = {}
        for mode in ("single", "vertical", "horizontal"):
            if mode == "single":
                solve = 0.55 + 0.10 * (op_count == 1) + 0.05 * (generated_count == 0)
                cost = 0.12 + 0.08 * op_count
                latency = 0.10 + 0.05 * op_count
                coordination = 0.02
            elif mode == "vertical":
                solve = 0.62 + 0.06 * min(3, op_count) + 0.04 * dependency_count + 0.05 * exact_verifier_hint
                cost = 0.18 + 0.05 * op_count
                latency = 0.16 + 0.04 * op_count
                coordination = 0.03 * op_count + 0.03 * context_saturation
            else:
                solve = 0.45 + 0.10 * min(config.k_max, op_count)
                cost = 0.25 + 0.08 * min(config.k_max, op_count)
                latency = 0.20 + 0.03 * op_count
                coordination = 0.06 * op_count + 0.06 * generated_count
            candidate_utilities[mode] = solve - 0.25 * cost - 0.18 * latency - 0.18 * coordination
        if op_count <= 1:
            return "single"
        return max(candidate_utilities, key=candidate_utilities.get)

    def propose_children(self, ctx, frame, operations: Sequence[Any]) -> list[ChildSpec]:
        config = ctx.profile.topology
        children = []
        for index, op in enumerate(operations):
            delta = (
                0.52
                + 0.10 * (op.kind in {"generated_expression", "memory_lookup"})
                - config.spawn_penalty
                - config.coord_penalty * min(2, index)
                - config.dep_penalty * len(op.dependencies)
            )
            if delta <= 0:
                continue
            child_id = f"child_{index}_{op.op_id}"
            instruction = f"Solve subgoal {op.op_id}: {op.description}"
            tool_scope = [op.tool_hint] if op.tool_hint else []
            summary = {"op_id": op.op_id, "output_key": op.output_key}
            required_capabilities = [
                "memory" if op.kind == "memory_lookup" else "tooling" if op.kind == "generated_expression" else "arithmetic"
            ]
            children.append(
                ChildSpec(
                    child_id=child_id,
                    role="child",
                    instruction=instruction,
                    tool_scope=tool_scope,
                    model_class="medium" if op.kind == "generated_expression" else "small",
                    required_capabilities=required_capabilities,
                    required_permissions=["local"],
                    dependency_ids=list(op.dependencies),
                    comm_mode="summary_only",
                    resume_policy="checkpoint",
                    init_summary=summary,
                )
            )
        return children

    def select_workers(self, ctx, frame, operations: Sequence[Any]) -> list[dict[str, Any]]:
        config = ctx.profile.topology
        op_ids = [op.op_id for op in operations]
        candidates = [
            {
                "worker_id": "w0",
                "instruction": "Sequential canonical plan",
                "op_ids": op_ids,
                "predicted_solve": 0.62,
                "tool_scope": ctx.state.visible_tool_names,
                "agent_id": "root",
            },
            {
                "worker_id": "w1",
                "instruction": "Reverse order plan",
                "op_ids": list(reversed(op_ids)),
                "predicted_solve": 0.58,
                "tool_scope": ctx.state.visible_tool_names,
                "agent_id": "root",
            },
            {
                "worker_id": "w2",
                "instruction": "Dependency-first plan",
                "op_ids": sorted(
                    op_ids,
                    key=lambda op_id: 0 if any(op.op_id == op_id and op.dependencies for op in operations) else 1,
                ),
                "predicted_solve": 0.55,
                "tool_scope": ctx.state.visible_tool_names,
                "agent_id": "root",
            },
        ]
        selected = []
        selected_ids = set()
        while len(selected) < min(config.k_max, len(candidates)):
            best = None
            best_score = -1e9
            for worker in candidates:
                if worker["worker_id"] in selected_ids:
                    continue
                solve_term = 1.0 - (1.0 - worker["predicted_solve"])
                diversity_penalty = 0.0
                for existing in selected:
                    diversity_penalty += jaccard(worker["op_ids"], existing["op_ids"])
                score = solve_term - 0.12 * diversity_penalty - 0.06 * (len(selected) + 1)
                if score > best_score:
                    best_score = score
                    best = worker
            if best is None or best_score < 0.35:
                break
            selected.append(best)
            selected_ids.add(best["worker_id"])
        return selected or [candidates[0]]

    def assign_scope(self, ctx, child_spec: ChildSpec, candidate_tool_names: Sequence[str]) -> list[str]:
        config = ctx.profile.topology
        if not candidate_tool_names:
            return child_spec.tool_scope
        scored = []
        for name in candidate_tool_names:
            coverage = (
                1.0
                if name in child_spec.tool_scope or any(token in name for token in child_spec.required_capabilities)
                else lexical_overlap(name, child_spec.instruction)
            )
            conflict = 1.0 if name in scored else 0.0
            coldstart = 0.2 if name.startswith("generated/") else 0.05
            score = coverage - config.size_penalty - config.conflict_penalty * conflict - config.coldstart_penalty * coldstart
            scored.append((score, name))
        ordered = [name for _, name in sorted(scored, key=lambda item: (-item[0], item[1]))]
        return ordered[:12]

    def merge_ensemble(self, ctx, worker_outputs: Sequence[dict[str, Any]]) -> Any:
        ordered = sorted(
            worker_outputs,
            key=lambda item: (
                0 if item.get("verifier_support", 0.0) >= 1.0 else 1,
                -item.get("verifier_support", 0.0),
                item.get("unresolved_critical", 0),
                -item.get("predicted_solve", 0.0),
                item.get("merge_priority", 0),
                item.get("branch_id", item.get("worker_id", "")),
            ),
        )
        if not ordered:
            return {}
        if all(isinstance(item.get("artifact"), dict) for item in ordered):
            merged: dict[str, Any] = {}
            for item in ordered:
                for key, value in item.get("artifact", {}).items():
                    merged.setdefault(key, value)
            if merged:
                return merged
        return ordered[0]["artifact"]

    def make_checkpoint(self, ctx, frame, artifacts, unresolved, open_handles) -> Checkpoint:
        summary = SummaryRecord(
            objective=frame.objective,
            evidence=[f"completed_ops={frame.operation_ids}"],
            artifacts=list(artifacts.keys()),
            unresolved=list(unresolved),
            open_handles=list(open_handles),
            next_actions=["resume" if unresolved else "stop"],
            symbols=ctx.task.symbolic_seeds,
            verifier_state={"verified": len(unresolved) == 0},
            provenance={"agent_id": frame.agent.agent_id, "role": frame.role},
        )
        return Checkpoint(
            summary=summary,
            artifact_refs=list(artifacts.keys()),
            open_handles=list(open_handles),
            unresolved_goals=list(unresolved),
            budget_state=ctx.budget.normalized(),
            verifier_state={"verified": len(unresolved) == 0},
            resume_constraints={"tool_scope": frame.tool_scope, "model_class": frame.model_class},
        )
