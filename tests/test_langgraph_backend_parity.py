from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from agintor.contracts import GoalSpec, baseline_langgraph_runtime_spec
from agintor.contracts.runtime_spec import GraphEdgeSpec, GraphNodeSpec, RuntimeSpec, ToolSpec as RuntimeToolSpec
from agintor.integrations.tradingagents.compiler import tradingagents_spec_from_goal
from agintor.runtime.langgraph.compiler import RuntimeSpecCompiler
from agintor.runtime.langgraph.executor import CompiledSpecRuntime, compile_runtime_spec
from agintor.search.spec_mutator import HeuristicSpecActionMutator, SpecMutationContext, load_runtime_spec_from_dir


def _langgraph_available() -> bool:
    try:
        return importlib.util.find_spec("langgraph.graph") is not None
    except ModuleNotFoundError:
        return False


LANGGRAPH_AVAILABLE = _langgraph_available()
LANGGRAPH_BACKEND = pytest.param(
    "langgraph",
    marks=pytest.mark.skipif(not LANGGRAPH_AVAILABLE, reason="langgraph extra is not installed"),
)


def _goal() -> GoalSpec:
    return GoalSpec(goal_id="goal.backend", raw_prompt="trade cautiously", normalized_goal="trade cautiously")


def _mutator_child_spec(tmp_path: Path) -> RuntimeSpec:
    runtime_dir = tmp_path / "parent"
    RuntimeSpecCompiler().compile_to_directory(
        baseline_langgraph_runtime_spec(runtime_id="runtime.parent"),
        runtime_dir,
        force=True,
    )
    candidate = HeuristicSpecActionMutator().mutate(
        SpecMutationContext(
            objective="backend parity",
            touched_scope=["top"],
            runtime_dir=runtime_dir,
            workspace=tmp_path / "work",
        )
    )
    return load_runtime_spec_from_dir(candidate.child_runtime_dir)


def _service_action_receipt_spec() -> RuntimeSpec:
    spec = baseline_langgraph_runtime_spec(runtime_id="runtime.receipts")
    nodes = [
        spec.graph.nodes[0],
        GraphNodeSpec(
            node_id="node.service",
            node_type="service_action",
            input_keys=["answer"],
            output_key="service_receipt",
            static_args={"method": "POST", "url": "https://example.invalid/hook"},
        ),
        spec.graph.nodes[1],
    ]
    graph = spec.graph.model_copy(
        update={
            "nodes": nodes,
            "edges": [
                GraphEdgeSpec(source="node.default", target="node.service"),
                GraphEdgeSpec(source="node.service", target="node.terminal"),
            ],
        },
        deep=True,
    )
    tools = [
        RuntimeToolSpec(
            tool_id="tool.service",
            name="Service action",
            family="service",
            runtime_visible=True,
            side_effect_kind="service_action",
        )
    ]
    return spec.model_copy(update={"graph": graph, "tools": tools}, deep=True)


def _spec_for_case(case_name: str, tmp_path: Path) -> RuntimeSpec:
    if case_name == "baseline":
        return baseline_langgraph_runtime_spec(runtime_id="runtime.baseline")
    if case_name == "mutator_child":
        return _mutator_child_spec(tmp_path)
    if case_name == "service_action_receipt":
        return _service_action_receipt_spec()
    raise AssertionError(f"unknown spec case {case_name!r}")


def _invoke(spec: RuntimeSpec, *, backend: str):
    runtime = compile_runtime_spec(spec, backend=backend)
    assert runtime.backend == backend
    return runtime.invoke(
        "hi",
        request_id="req.backend",
        task_id="task.backend",
        seed=7,
        runtime_hash="runtime.hash",
    )


def _terminal_state(state: Any) -> dict[str, Any]:
    return {
        "request_id": state.request_id,
        "task_id": state.task_id,
        "seed": state.seed,
        "prompt": state.prompt,
        "runtime_hash": state.runtime_hash,
        "runtime_spec_digest": state.runtime_spec_digest,
        "current_node_id": state.current_node_id,
        "status": state.status,
        "error": state.error,
        "budget": state.budget,
    }


def _trace_essentials(trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = ("event", "request_id", "node_id", "node_type", "output_key", "error")
    return [{key: row[key] for key in keys if key in row} for row in trace]


def _receipt_essentials(receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in receipt.items() if key != "created_at"} for receipt in receipts]


@pytest.mark.skipif(not LANGGRAPH_AVAILABLE, reason="langgraph extra is not installed")
def test_auto_backend_uses_real_langgraph_for_baseline_and_tradingagents() -> None:
    for spec in [
        baseline_langgraph_runtime_spec(runtime_id="r1"),
        tradingagents_spec_from_goal(_goal()),
    ]:
        runtime = CompiledSpecRuntime(spec)

        assert runtime.backend == "langgraph"
        assert runtime.invoke("hi").status == "completed"


@pytest.mark.skipif(LANGGRAPH_AVAILABLE, reason="requires langgraph extra to be absent")
def test_auto_backend_falls_back_to_sequential_without_langgraph() -> None:
    runtime = CompiledSpecRuntime(baseline_langgraph_runtime_spec(runtime_id="r1"))

    assert runtime.backend == "sequential"
    assert runtime.invoke("hi").status == "completed"


@pytest.mark.parametrize("backend", ["sequential", LANGGRAPH_BACKEND])
def test_forced_backend_runs_baseline(backend: str) -> None:
    state = _invoke(baseline_langgraph_runtime_spec(runtime_id=f"runtime.{backend}"), backend=backend)

    assert state.status == "completed"
    assert state.artifacts["answer"]["answer"] == "hi"


@pytest.mark.skipif(not LANGGRAPH_AVAILABLE, reason="langgraph extra is not installed")
@pytest.mark.parametrize("spec_case", ["baseline", "mutator_child", "service_action_receipt"])
def test_langgraph_and_sequential_backends_match_terminal_state_and_effects(
    spec_case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agintor.runtime.langgraph import operation_service

    monkeypatch.setattr(operation_service, "now_ts", lambda: 123.0)
    spec = _spec_for_case(spec_case, tmp_path)

    sequential = _invoke(spec, backend="sequential")
    langgraph = _invoke(spec, backend="langgraph")

    assert _terminal_state(sequential) == _terminal_state(langgraph)
    assert sequential.node_results == langgraph.node_results
    assert sequential.artifacts == langgraph.artifacts
    assert _trace_essentials(sequential.trace) == _trace_essentials(langgraph.trace)
    assert _receipt_essentials(sequential.side_effect_receipts) == _receipt_essentials(langgraph.side_effect_receipts)


def _unsupported_spec(case_name: str) -> RuntimeSpec:
    spec = baseline_langgraph_runtime_spec(runtime_id=f"runtime.unsupported.{case_name}")
    if case_name == "conditional_edge":
        graph = spec.graph.model_copy(
            update={"edges": [spec.graph.edges[0].model_copy(update={"condition": "route"}, deep=True)]},
            deep=True,
        )
        return spec.model_copy(update={"graph": graph}, deep=True)
    if case_name == "parallel_fanout":
        graph = spec.graph.model_copy(
            update={
                "nodes": [
                    *spec.graph.nodes,
                    GraphNodeSpec(node_id="node.extra", node_type="verify", input_keys=["answer"]),
                ],
                "edges": [
                    *spec.graph.edges,
                    GraphEdgeSpec(source="node.default", target="node.extra"),
                ],
            },
            deep=True,
        )
        return spec.model_copy(update={"graph": graph}, deep=True)
    if case_name == "interrupt_resume":
        nodes = [
            node.model_copy(update={"metadata": {"interrupt": True}}, deep=True)
            if node.node_id == "node.default"
            else node
            for node in spec.graph.nodes
        ]
        return spec.model_copy(update={"graph": spec.graph.model_copy(update={"nodes": nodes}, deep=True)}, deep=True)
    if case_name == "unsupported_node_type":
        nodes = [
            node.model_copy(update={"node_type": "router"}, deep=True)
            if node.node_id == "node.default"
            else node
            for node in spec.graph.nodes
        ]
        return spec.model_copy(update={"graph": spec.graph.model_copy(update={"nodes": nodes}, deep=True)}, deep=True)
    if case_name == "duplicate_output_key":
        nodes = [
            node.model_copy(update={"output_key": "answer"}, deep=True)
            if node.node_id == "node.terminal"
            else node
            for node in spec.graph.nodes
        ]
        return spec.model_copy(update={"graph": spec.graph.model_copy(update={"nodes": nodes}, deep=True)}, deep=True)
    if case_name == "multiple_terminal_nodes":
        graph = spec.graph.model_copy(update={"terminal_nodes": ["node.default", "node.terminal"]}, deep=True)
        return spec.model_copy(update={"graph": graph}, deep=True)
    if case_name == "terminal_outgoing_edge":
        graph = spec.graph.model_copy(
            update={"edges": [*spec.graph.edges, GraphEdgeSpec(source="node.terminal", target="node.default")]},
            deep=True,
        )
        return spec.model_copy(update={"graph": graph}, deep=True)
    raise AssertionError(f"unknown unsupported spec case {case_name!r}")


@pytest.mark.parametrize("backend", ["sequential", LANGGRAPH_BACKEND])
@pytest.mark.parametrize(
    "case_name",
    [
        "conditional_edge",
        "parallel_fanout",
        "interrupt_resume",
        "unsupported_node_type",
        "duplicate_output_key",
        "multiple_terminal_nodes",
        "terminal_outgoing_edge",
    ],
)
def test_pass1_subset_rejects_unsupported_graph_features(case_name: str, backend: str) -> None:
    with pytest.raises(ValueError, match="pass-1 LangGraph executor"):
        compile_runtime_spec(_unsupported_spec(case_name), backend=backend)


def test_runtime_spec_compiler_rejects_unsupported_pass1_subset(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="pass-1 LangGraph executor"):
        RuntimeSpecCompiler().compile_to_directory(_unsupported_spec("conditional_edge"), tmp_path / "runtime", force=True)
