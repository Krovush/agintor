from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..contracts import GoalSpec, OraclePackage, RuntimeSpec
from .compiler import OracleCompiler

CompilerStep = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass
class OracleCompilerGraph:
    """Small pluggable graph facade for the compiler workflow.

    This file intentionally does not require LangGraph at import time. When the
    dependency is present, callers can replace these steps with StateGraph nodes;
    the state contract remains the same.
    """

    compiler: OracleCompiler = field(default_factory=OracleCompiler)
    steps: list[tuple[str, CompilerStep]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.steps:
            self.steps = [
                ("goal_interpreter", self._identity),
                ("runtime_context_reader", self._identity),
                ("task_class_inferencer", self._identity),
                ("claim_decomposer", self._identity),
                ("validator_family_router", self._identity),
                ("benchmark_designer", self._identity),
                ("fixture_and_evaluator_designer", self._identity),
                ("authority_and_abstention_designer", self._identity),
                ("package_writer", self._package_writer),
                ("critic", self._identity),
                ("deterministic_qa_runner", self._identity),
                ("freeze_or_abstain", self._identity),
            ]

    @staticmethod
    def _identity(state: dict[str, Any]) -> dict[str, Any]:
        return state

    def _package_writer(self, state: dict[str, Any]) -> dict[str, Any]:
        goal_spec = state["goal_spec"]
        runtime_spec = state.get("runtime_spec")
        state["oracle_package"] = self.compiler.compile(goal_spec, runtime_spec)
        return state

    def invoke(self, *, goal_spec: GoalSpec, runtime_spec: RuntimeSpec | None = None) -> OraclePackage:
        state: dict[str, Any] = {"goal_spec": goal_spec, "runtime_spec": runtime_spec}
        for name, step in self.steps:
            state["current_step"] = name
            state = step(state)
        return state["oracle_package"]


__all__ = ["OracleCompilerGraph"]
