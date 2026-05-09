from __future__ import annotations

import json

import pytest

from agintor.evaluation.benchmarks import build_demo_suite, load_suite
from agintor.evaluation.challenge_generators import (
    TOOL_WORKFLOW_GENERATOR_ID,
    ToolWorkflowDifficulty,
    generate_tool_workflow_challenges,
)
from agintor.contracts import (
    BenchmarkTask,
    CapabilityExchange,
    RunResult,
    RuntimeSolveResponse,
    SolveResult,
    sealed_benchmark_task_payload,
    runtime_visible_benchmark_task,
)
from agintor.contracts.verifiers import rescore_private_solve_response
from agintor.runtime.api import runtime_solve_request_for_task


def test_tool_workflow_generation_is_deterministic_and_keeps_public_payload_sealed() -> None:
    difficulty = ToolWorkflowDifficulty(expression_depth=4, dependency_width=2, distractor_count=2, numeric_edge_cases=True)
    first = generate_tool_workflow_challenges(partition="train", count=3, seed=42, difficulty=difficulty)
    second = generate_tool_workflow_challenges(partition="train", count=3, seed=42, difficulty=difficulty)

    assert [task.model_dump() for task in first] == [task.model_dump() for task in second]
    for task in first:
        assert task.family == "tool"
        assert task.task_type == "tool_expression"
        assert task.verifier_type == "number_exact"
        assert task.expected is None
        assert task.private_expected is not None
        assert "private_expected" not in task.model_dump()
        assert task.metadata["domain_kind"] == "generated_tool_workflow"
        assert task.metadata["generator_id"] == TOOL_WORKFLOW_GENERATOR_ID
        assert task.metadata["private_answer_ref"].startswith(f"private://{TOOL_WORKFLOW_GENERATOR_ID}/")

        public_blob = json.dumps({"prompt": task.prompt, "metadata": task.metadata["public_payload"]}, sort_keys=True)
        assert "expected" not in public_blob.lower()
        assert "private_answer_ref" not in public_blob


def test_tool_workflow_runtime_projection_strips_private_answer_metadata() -> None:
    task = generate_tool_workflow_challenges(
        partition="train",
        count=1,
        seed=3,
        difficulty=ToolWorkflowDifficulty(expression_depth=3, dependency_width=2, distractor_count=1),
    )[0]

    visible = runtime_visible_benchmark_task(task)
    request = runtime_solve_request_for_task(runtime_backend="local", seed=0, task=task)
    request_payload = request.model_dump(mode="json")

    assert visible.expected is None
    assert visible.private_expected is None
    assert visible.verifier_type == "none"
    assert "private_answer_ref" not in visible.metadata
    assert "private_answer_mechanism" not in visible.metadata
    assert "private_expected" not in request_payload["task"]
    assert request_payload["task"]["verifier_type"] == "none"
    assert "private_answer_ref" not in json.dumps(request_payload, sort_keys=True)


def test_runtime_projection_strips_unknown_private_metadata_prefix() -> None:
    task = generate_tool_workflow_challenges(
        partition="train",
        count=1,
        seed=4,
        difficulty=ToolWorkflowDifficulty(expression_depth=2, dependency_width=1, distractor_count=0),
    )[0].model_copy(
        update={
            "private_expected": None,
            "metadata": {
                "public_payload": {"ok": True},
                "private_custom_hint": "do-not-send",
            },
        }
    )

    visible = runtime_visible_benchmark_task(task)

    assert visible.metadata == {"public_payload": {"ok": True}}


def test_sealed_benchmark_task_payload_preserves_private_answer_for_frozen_suites() -> None:
    task = generate_tool_workflow_challenges(
        partition="train",
        count=1,
        seed=5,
        difficulty=ToolWorkflowDifficulty(expression_depth=2, dependency_width=1, distractor_count=0),
    )[0]

    payload = sealed_benchmark_task_payload(task)

    assert "private_expected" not in task.model_dump()
    assert payload["private_expected"] == task.private_expected


def test_tool_frontier_suite_extends_demo_without_replacing_demo_tasks() -> None:
    demo = build_demo_suite()
    frontier = load_suite("tool-frontier")

    demo_train_ids = [task.task_id for task in demo.train]
    frontier_train_ids = [task.task_id for task in frontier.train]

    assert set(demo_train_ids).issubset(set(frontier_train_ids))
    generated = [task for task in frontier.train if task.task_id not in set(demo_train_ids)]
    assert generated
    assert frontier.train[0].task_id == generated[0].task_id
    assert all(task.metadata["domain_kind"] == "generated_tool_workflow" for task in generated)
    assert frontier.evidence_contract is not None
    assert frontier.evidence_contract.contract_id == "tool-frontier-generated-workflow-v1"
    assert load_suite("demo").train == demo.train


def test_tool_frontier_suite_has_one_canonical_loader_name() -> None:
    assert load_suite("tool-frontier").name == "tool-frontier"
    with pytest.raises(ValueError, match="tool-frontier"):
        load_suite("tool_frontier")
    with pytest.raises(ValueError, match="tool-frontier"):
        load_suite("generated_tool_workflow_v1")


def test_suite_json_loader_preserves_evidence_contract(tmp_path) -> None:
    frontier = load_suite("tool-frontier")
    path = tmp_path / "suite.json"
    payload = {
        "name": frontier.name,
        "train": [sealed_benchmark_task_payload(task) for task in frontier.train],
        "val": [sealed_benchmark_task_payload(task) for task in frontier.val],
        "test": [sealed_benchmark_task_payload(task) for task in frontier.test],
        "proxy": [sealed_benchmark_task_payload(task) for task in frontier.proxy],
        "evidence_contract": frontier.evidence_contract.model_dump(mode="json"),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = load_suite(str(path))

    assert loaded.evidence_contract is not None
    assert loaded.evidence_contract.contract_id == frontier.evidence_contract.contract_id
    loaded_generated = [task for task in loaded.train if task.metadata.get("domain_kind") == "generated_tool_workflow"]
    assert loaded_generated
    assert all(task.private_expected is not None for task in loaded_generated)


def test_trace_based_private_solve_rescore_uses_recorded_trace() -> None:
    task = BenchmarkTask(
        task_id="tool.sealed.trace",
        family="tool",
        prompt="Emit the required trace event.",
        task_type="trace",
        expected=None,
        private_expected="sealed_trace_event",
        verifier_type="trace_event",
    )
    response = RuntimeSolveResponse(
        request_id="request",
        capability_exchange=CapabilityExchange(runtime_contract_version="test"),
        solve_result=SolveResult(
            request_id="request",
            runtime_hash="runtime",
            artifact={"ok": True},
            status="best_effort",
            verification_status="best_effort",
            summary="runtime-visible task had no exact verifier",
            trace_ref=RunResult.encode_trace_ref([{"event": "sealed_trace_event"}]),
            verified=False,
            best_effort=True,
        ),
    )

    rescored = rescore_private_solve_response(response, task)

    assert rescored.solve_result.status == "verified"
    assert rescored.solve_result.verified is True


def test_private_solve_rescore_preserves_failed_status() -> None:
    task = BenchmarkTask(
        task_id="tool.sealed.failed",
        family="tool",
        prompt="Return the hidden number.",
        task_type="number",
        expected=None,
        private_expected=7,
        verifier_type="number_exact",
    )
    response = RuntimeSolveResponse(
        request_id="request",
        capability_exchange=CapabilityExchange(runtime_contract_version="test"),
        solve_result=SolveResult(
            request_id="request",
            runtime_hash="runtime",
            artifact=7,
            status="failed",
            verification_status="failed",
            summary="runtime failed after emitting an artifact",
            faults={"hard_invalid": True},
            verified=False,
            best_effort=False,
        ),
    )

    rescored = rescore_private_solve_response(response, task)

    assert rescored.solve_result.status == "failed"
    assert rescored.solve_result.verification_status == "failed"
    assert rescored.solve_result.verified is False


def test_private_solve_rescore_ignores_runtime_forged_sealed_check() -> None:
    task = BenchmarkTask(
        task_id="tool.sealed.forged",
        family="tool",
        prompt="Return the hidden number.",
        task_type="number",
        expected=None,
        private_expected=7,
        verifier_type="number_exact",
    )
    response = RuntimeSolveResponse(
        request_id="request",
        capability_exchange=CapabilityExchange(runtime_contract_version="test"),
        solve_result=SolveResult(
            request_id="request",
            runtime_hash="runtime",
            artifact=3,
            status="verified",
            verification_status="verified",
            summary="runtime claimed sealed private success",
            checks=[
                {"checker": "local", "passed": True},
                {"checker": "sealed_private", "passed": True, "authority": "runtime"},
            ],
            verified=True,
            best_effort=False,
        ),
    )

    rescored = rescore_private_solve_response(response, task)
    sealed_checks = [check for check in rescored.solve_result.checks if check.get("checker") == "sealed_private"]

    assert rescored.solve_result.status == "unverified"
    assert rescored.solve_result.verification_status == "exact_verifier_failed"
    assert rescored.solve_result.verified is False
    assert rescored.solve_result.checks[0] == {"checker": "local", "passed": True}
    assert sealed_checks == [
        {
            "checker": "sealed_private",
            "passed": False,
            "verifier_type": "number_exact",
            "authority": "host",
        }
    ]


def test_tool_workflow_difficulty_controls_frontier_shape() -> None:
    easy = generate_tool_workflow_challenges(
        partition="train",
        count=1,
        seed=7,
        difficulty=ToolWorkflowDifficulty(expression_depth=1, distractor_count=0),
    )[0]
    hard = generate_tool_workflow_challenges(
        partition="train",
        count=1,
        seed=7,
        difficulty=ToolWorkflowDifficulty(expression_depth=6, dependency_width=3, distractor_count=4, numeric_edge_cases=True),
    )[0]

    assert len(hard.operations) > len(easy.operations)
    assert any(len(operation.dependencies) > 1 for operation in hard.operations)
    assert len(hard.context_items) == 4
    assert hard.metadata["difficulty"]["expression_depth"] == 6
    assert "frontier" in hard.metadata["slice_tags"]
    assert "numeric_edge_cases" in hard.metadata["slice_tags"]
