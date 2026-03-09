from __future__ import annotations

import json
import inspect
import math
import sys
from typing import Any, Iterable, Sequence

from agintor.schemas import ToolSpec
from agintor.tool_runtime import RegisteredTool, validate_expression_tool, validate_tool_candidate
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

    def _fallback_expression(self, operation, tool_args: dict[str, Any] | None = None) -> str:
        description = operation.description.lower()
        available_args = dict(tool_args or operation.args)
        arg_names = sorted(available_args)
        if not arg_names:
            return "0"
        if "numbers" in available_args and "modulus" in available_args and any(token in description for token in ("square", "squared", "mod")):
            return "sum(x*x for x in numbers) % modulus"
        if "numbers" in available_args and any(token in description for token in ("sum", "total", "add")):
            return "sum(numbers)"
        if any(token in description for token in ("range", "difference")) and len(arg_names) >= 2:
            joined = ", ".join(arg_names)
            return f"max({joined}) - min({joined})"
        if len(arg_names) == 1:
            return arg_names[0]
        if any(token in description for token in ("add", "sum", "total", "plus")):
            return " + ".join(arg_names)
        return arg_names[0]

    def _parse_tool_spec_payload(self, text: str) -> dict[str, Any]:
        stripped = text.strip()
        candidates: list[str] = []
        if stripped:
            candidates.append(stripped)
            start = stripped.find("{")
            end = stripped.rfind("}")
            if start != -1 and end > start:
                fragment = stripped[start : end + 1]
                if fragment != stripped:
                    candidates.append(fragment)
        for candidate in candidates:
            try:
                payload = json.loads(candidate)
            except Exception:
                continue
            if isinstance(payload, dict):
                return payload
        return {}

    def _build_expression_tool(self, candidates: Sequence[str], tool_args: dict[str, Any], safety_guard) -> tuple[str, str, Any, list[dict[str, Any]]]:
        last_error: Exception | None = None
        seen: set[str] = set()
        for candidate in candidates:
            expression = str(candidate or "").strip()
            if not expression or expression in seen:
                continue
            seen.add(expression)
            try:
                source, executor = validate_expression_tool(expression, [], safety_guard)
                expected = executor(**tool_args)
                tests = [{"input": dict(tool_args), "expected": expected}]
                source, executor = validate_expression_tool(expression, tests, safety_guard)
                return expression, source, executor, tests
            except Exception as exc:
                last_error = exc
        raise ValueError(f"unable to synthesize valid tool expression: {last_error}")

    def rank_categories(self, ctx, operation, category_summaries: dict[str, dict[str, Any]]) -> list[str]:
        scored = []
        for category_key, summary_meta in category_summaries.items():
            summary = str(summary_meta.get("summary", ""))
            descendants = int(summary_meta.get("descendants", len(ctx.shell.tool_registry.tools_in_category(category_key))))
            histpass = float(summary_meta.get("historical_pass_rate", 0.0))
            cachehit = float(summary_meta.get("cache_hit", 0.0))
            coldstart = float(summary_meta.get("coldstart", 0.15))
            permrisk = float(summary_meta.get("permission_risk", 0.0))
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
            cachehit = tool.cache_hit
            coldstart = 0.05 if tool.spec.runtime == "python" else 0.20
            permrisk = 1.0 if tool.spec.permissions else 0.0
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

    def propose_tool_spec(self, ctx, operation, resolved_args: dict[str, Any] | None = None) -> tuple[ToolSpec, str, Any]:
        tool_args = dict(resolved_args or operation.args)
        fallback_expression = self._fallback_expression(operation, tool_args)
        candidate_expressions = [operation.expression or fallback_expression]
        if not operation.expression:
            default_expression = self._fallback_expression(operation, tool_args)
            response = ctx.provider.generate(
                type(
                    "Req",
                    (),
                    {
                        "instructions": "Return only JSON with keys expression and description for a deterministic Python tool.",
                        "prompt": json.dumps({"description": operation.description, "args": tool_args}, sort_keys=True),
                        "model_class": "medium",
                        "seed": ctx.seed,
                        "metadata": {"mode": "tool_spec", "payload": {"expression": default_expression, "description": operation.description, "args": tool_args}},
                    },
                )
            )
            ctx.consume_model_response(response, purpose="tool_spec")
            payload = self._parse_tool_spec_payload(response.text)
            provider_expression = payload.get("expression")
            if provider_expression not in (None, ""):
                candidate_expressions.insert(0, str(provider_expression))
        expression, source, executor, tests = self._build_expression_tool(
            candidate_expressions,
            tool_args,
            ctx.shell.safety_guard,
        )
        args = list(inspect.signature(executor).parameters)
        signature = f"({', '.join(args)}) -> value"
        name = f"generated/local/{stable_hash(expression, signature)[:10]}"
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
            build_cmd=f'"{sys.executable}" -m py_compile tool.py',
            run_cmd=f'"{sys.executable}" tool.py',
            timeout_s=10,
            determinism_class="stable",
        )
        return spec, source, executor

    def validate_tool(self, ctx, spec: ToolSpec, source: str) -> bool:
        result = validate_tool_candidate(spec, source, ctx.shell.safety_guard, ctx.shell.sandbox_manager)
        return bool(result.get("valid", result.get("deterministic", True)))

    def promote_tool(self, ctx, tool: RegisteredTool) -> bool:
        return (
            tool.pass_rate >= self.ETA_P
            and len(tool.distinct_tasks) >= self.ETA_R
            and tool.spec.determinism_class == "stable"
            and tool.safety_validated
        )

    def dispatch_tool(self, ctx, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        estimated_latency = 0.05 * max(1, len(args))
        async_flag = estimated_latency > self.T_SLICE or bool(ctx.shell.tool_registry.get(tool_name).spec.backgroundable)
        return {"async": async_flag}
