from __future__ import annotations

from typing import Any

from ..contracts import GoalSpec, RuntimeSpec, baseline_langgraph_runtime_spec
from ..integrations.tradingagents.compiler import tradingagents_spec_from_goal
from ..utils import stable_hash

SPEC_BACKED_RUNTIME_KINDS = frozenset({"langgraph_spec", "tradingagents_langgraph"})
SUPPORTED_RUNTIME_KINDS = frozenset({"policy_modules", *SPEC_BACKED_RUNTIME_KINDS})


def normalize_runtime_kind(runtime_kind: str | None) -> str:
    kind = str(runtime_kind or "policy_modules").strip() or "policy_modules"
    if kind not in SUPPORTED_RUNTIME_KINDS:
        raise ValueError(f"unknown runtime kind {kind!r}")
    return kind


def is_spec_backed_runtime_kind(runtime_kind: str | None) -> bool:
    return normalize_runtime_kind(runtime_kind) in SPEC_BACKED_RUNTIME_KINDS


def runtime_spec_for_kind(
    runtime_kind: str | None,
    *,
    goal_spec: GoalSpec | None = None,
    runtime_id: str | None = None,
) -> RuntimeSpec | None:
    kind = normalize_runtime_kind(runtime_kind)
    if kind == "policy_modules":
        return None
    if kind == "langgraph_spec":
        if runtime_id is None:
            suffix = stable_hash(goal_spec.goal_id, goal_spec.normalized_goal)[:12] if goal_spec is not None else "preview"
            runtime_id = f"runtime.{suffix}"
        return baseline_langgraph_runtime_spec(runtime_id=runtime_id)
    if kind == "tradingagents_langgraph":
        if goal_spec is None:
            raise ValueError("tradingagents_langgraph runtime kind requires a GoalSpec")
        return tradingagents_spec_from_goal(goal_spec)
    raise ValueError(f"unknown runtime kind {kind!r}")


def runtime_spec_for_plan(runtime_plan: Any, goal_spec: GoalSpec) -> RuntimeSpec | None:
    plan_id = str(getattr(runtime_plan, "plan_id", "") or "runtime")
    return runtime_spec_for_kind(
        str(getattr(runtime_plan, "runtime_kind", "policy_modules") or "policy_modules"),
        goal_spec=goal_spec,
        runtime_id=f"runtime.{stable_hash(plan_id)[:12]}",
    )


__all__ = [
    "SPEC_BACKED_RUNTIME_KINDS",
    "SUPPORTED_RUNTIME_KINDS",
    "is_spec_backed_runtime_kind",
    "normalize_runtime_kind",
    "runtime_spec_for_kind",
    "runtime_spec_for_plan",
]
