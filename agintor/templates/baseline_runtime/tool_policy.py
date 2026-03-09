from __future__ import annotations

import math
from typing import Any, Iterable, Sequence

from agintor.schemas import ToolSpec
from agintor.tool_runtime import RegisteredTool, validate_expression_tool
from agintor.utils import lexical_overlap, stable_hash


class ToolPolicy:
    ETA_P = 0.80
    ETA_R = 3
    K_C = 3
    T_SLICE = 60
    BUILD_WEIGHT = 0.20
    EXEC_WEIGHT = 0.10
    SAFETY_WEIGHT = 0.05
    FUTURE_WEIGHT = 0.10

    def rank_categories(self, ctx, operation, category_summaries: dict[str, str]) -> list[str]:
        scored = []
        for category_key, summary in category_summaries.items():
            descendants = len(ctx.shell.tool_registry.tools_in_category(category_key))
            histpass = 0.0
            cachehit = 1.0 if category_key.startswith("math/") or category_key.startswith("data/") else 0.5
            coldstart = 0.05 if category_key.startswith("math/") else 0.15
            permrisk = 0.0
            iface = lexical_overlap(category_key, operation.description)
            sim = lexical_overlap(summary + " " + category_key, ctx.task.prompt)
            score = 0.35 * sim + 0.20 * iface + 0.10 * histpass + 0.10 * cachehit - 0.08 * math.log1p(descendants) - 0.10 * coldstart - 0.07 * permrisk
            scored.append((score, category_key))
        return [category for _, category in sorted(scored, key=lambda item: (-item[0], item[1]))]

    def rank_tools(self, ctx, operation, candidate_tools: Sequence[RegisteredTool]) -> list[str]:
        scored = []
        for tool in candidate_tools:
            sim = lexical_overlap(tool.spec.description + " " + tool.spec.name, operation.description + " " + ctx.task.prompt)
            sigmatch = lexical_overlap(tool.spec.signature, operation.description)
            cachehit = 1.0 if tool.sandbox_hash else 0.0
            coldstart = 0.05 if tool.spec.runtime == "python" else 0.20
            permrisk = 0.0
            depdepth = len(tool.spec.deps)
            score = 0.30 * sim + 0.20 * sigmatch + 0.15 * tool.pass_rate + 0.10 * cachehit - 0.10 * coldstart - 0.07 * permrisk - 0.08 * depdepth
            scored.append((score, tool.spec.name))
        return [name for _, name in sorted(scored, key=lambda item: (-item[0], item[1]))]

    def should_create_tool(self, ctx, operation, ranked_reusable_tool_names: Sequence[str]) -> bool:
        best_reuse_gain = 0.0 if not ranked_reusable_tool_names else 0.55
        current_gain = 0.85 if operation.kind == "generated_expression" else 0.20
        future_gain = 0.60 if operation.kind == "generated_expression" else 0.10
        build_cost = 0.35 if operation.kind == "generated_expression" else 0.10
        exec_cost = 0.15
        safety_cost = 0.05
        return current_gain + self.FUTURE_WEIGHT * future_gain - best_reuse_gain > self.BUILD_WEIGHT * build_cost + self.EXEC_WEIGHT * exec_cost + self.SAFETY_WEIGHT * safety_cost

    def propose_tool_spec(self, ctx, operation) -> tuple[ToolSpec, str, Any]:
        args = sorted(operation.args)
        signature = f"({', '.join(args)}) -> value"
        name = f"generated/local/{stable_hash(operation.expression, signature)[:10]}"
        tests = []
        if operation.args:
            expected = eval(operation.expression, {"sum": sum, "min": min, "max": max, "abs": abs, "round": round, "math": math}, dict(operation.args))
            tests.append({"input": dict(operation.args), "expected": expected})
        source, executor = validate_expression_tool(operation.expression, tests, ctx.shell.safety_guard)
        spec = ToolSpec(
            name=name,
            category_path=["generated", "local"],
            signature=signature,
            description=operation.description,
            runtime="python",
            deps=[],
            permissions=[],
            tests=tests,
            backgroundable=False,
            state_schema={"type": "object"},
            source_digest=stable_hash(source),
            build_cmd="python -m py_compile tool.py",
            run_cmd="python tool.py",
            timeout_s=10,
            determinism_class="stable",
        )
        return spec, source, executor

    def validate_tool(self, ctx, spec: ToolSpec, source: str) -> bool:
        ctx.shell.safety_guard.validate_permissions(spec.permissions)
        ctx.shell.safety_guard.validate_generated_source(source)
        compile(source, f"<{spec.name}>", "exec")
        return True

    def promote_tool(self, ctx, tool: RegisteredTool) -> bool:
        return tool.pass_rate >= self.ETA_P and len(tool.distinct_tasks) >= self.ETA_R and tool.spec.determinism_class == "stable"

    def dispatch_tool(self, ctx, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        estimated_latency = 0.05 * max(1, len(args))
        async_flag = estimated_latency > self.T_SLICE or bool(ctx.shell.tool_registry.get(tool_name).spec.backgroundable)
        return {"async": async_flag}
