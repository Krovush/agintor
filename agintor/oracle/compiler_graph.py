from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..contracts import GoalSpec, OraclePackage, RuntimeSpec
from .compiler import OracleCompiler
from .qa import qa_oracle_package


@dataclass
class OracleCompilerState:
    goal: GoalSpec
    runtime_spec: RuntimeSpec | None = None
    prior_ledgers: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    proposals: dict[str, Any] = field(default_factory=dict)
    package: OraclePackage | None = None
    qa_passed: bool = False
    reason_codes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CompilerProposal:
    family_ids: list[str] = field(default_factory=list)
    claim_pack_ids: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class LinearCompilerGraph:
    """Small deterministic fallback used when langgraph is not installed."""

    def __init__(self, steps: list[tuple[str, Callable[[OracleCompilerState], OracleCompilerState]]]) -> None:
        self.steps = steps

    def invoke(self, state: OracleCompilerState | dict[str, Any]) -> OracleCompilerState:
        current = state if isinstance(state, OracleCompilerState) else OracleCompilerState(**state)
        for name, step in self.steps:
            current.notes.append(f"compiler_step:{name}")
            current = step(current)
        return current


def build_oracle_compiler_graph(compiler: OracleCompiler | None = None):
    compiler = compiler or OracleCompiler()

    def goal_interpreter(state: OracleCompilerState) -> OracleCompilerState:
        state.proposals["goal_summary"] = compiler._goal_text(state.goal)
        return state

    def runtime_context_reader(state: OracleCompilerState) -> OracleCompilerState:
        state.proposals["runtime_spec_digest"] = getattr(state.runtime_spec, "spec_digest", "") or ""
        return state

    def package_writer(state: OracleCompilerState) -> OracleCompilerState:
        state.package = compiler.compile(state.goal, state.runtime_spec, prior_ledgers=state.prior_ledgers)
        return state

    def deterministic_qa_runner(state: OracleCompilerState) -> OracleCompilerState:
        if state.package is None:
            state.qa_passed = False
            state.reason_codes.append("missing_package")
            return state
        report = qa_oracle_package(state.package)
        state.qa_passed = report.passed
        state.reason_codes.extend(report.reason_codes)
        return state

    steps = [
        ("goal_interpreter", goal_interpreter),
        ("runtime_context_reader", runtime_context_reader),
        ("task_class_inferencer", lambda state: state),
        ("claim_decomposer", lambda state: state),
        ("validator_family_router", lambda state: state),
        ("benchmark_designer", lambda state: state),
        ("fixture_and_evaluator_designer", lambda state: state),
        ("authority_and_abstention_designer", lambda state: state),
        ("package_writer", package_writer),
        ("critic", lambda state: state),
        ("deterministic_qa_runner", deterministic_qa_runner),
        ("freeze_or_abstain", lambda state: state),
    ]
    try:
        from langgraph.graph import END, START, StateGraph  # type: ignore
    except Exception:
        return LinearCompilerGraph(steps)

    graph = StateGraph(dict)

    def wrap(fn):
        def inner(raw: dict[str, Any]) -> dict[str, Any]:
            state = raw.get("state")
            if not isinstance(state, OracleCompilerState):
                state = OracleCompilerState(**raw)
            return {"state": fn(state)}
        return inner

    previous = START
    for name, fn in steps:
        graph.add_node(name, wrap(fn))
        graph.add_edge(previous, name)
        previous = name
    graph.add_edge(previous, END)
    return graph.compile()


def run(goal: GoalSpec, runtime_spec: RuntimeSpec | None, registry: Any, provider: Any | None = None) -> CompilerProposal:
    if provider is None:
        return CompilerProposal()
    hook = getattr(provider, "propose_oracle_compiler", None)
    if callable(hook):
        proposal = hook(goal=goal, runtime_spec=runtime_spec, registry=registry)
        if isinstance(proposal, CompilerProposal):
            return proposal
        if isinstance(proposal, dict):
            return CompilerProposal(
                family_ids=[str(item) for item in proposal.get("family_ids", [])],
                claim_pack_ids=[str(item) for item in proposal.get("claim_pack_ids", [])],
                notes=[str(item) for item in proposal.get("notes", [])],
            )
    return CompilerProposal(notes=["provider_has_no_oracle_compiler_hook"])


__all__ = ["CompilerProposal", "LinearCompilerGraph", "OracleCompilerState", "build_oracle_compiler_graph", "run"]
