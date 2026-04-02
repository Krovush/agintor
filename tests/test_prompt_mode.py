from __future__ import annotations

import json
from pathlib import Path

import pytest

from agintor.benchmarks import BenchmarkSuite
from agintor.evaluator import RuntimeEvaluator
from agintor.provider_common import LocalDeterministicProvider
from agintor.runtime_api import load_solve_request, solve_request_to_task
from agintor.schemas import BenchmarkTask, ModelRequest, OperationSpec


def test_solve_request_to_task_skips_tool_path_when_tool_scope_blocks_it() -> None:
    request = load_solve_request(prompt="Compute the sum of squares modulo 7 for [2, 3].").copy(
        update={
            "allowed_tool_categories": ["memory"],
            "verification_preference": "required",
        }
    )

    task = solve_request_to_task(request)

    assert task.task_type == "user_request"
    assert task.verifier_type == "none"
    assert task.verification_required is True
    assert task.allow_best_effort is False


@pytest.mark.parametrize(
    ("prompt_path", "context_path"),
    [
        (r"C:\repo\main.py", "C:/repo/main.py"),
        ("C:/repo/main.py", r"C:\repo\main.py"),
    ],
)
def test_solve_request_to_task_parses_windows_prompt_paths(prompt_path: str, context_path: str) -> None:
    request = load_solve_request(prompt=f"Who owns {prompt_path}?").copy(
        update={
            "context_items": [{"file_path": context_path, "owner": "alice"}],
        }
    )

    task = solve_request_to_task(request)

    assert task.task_type == "memory_query"
    assert task.expected == "alice"
    assert task.file_paths == [prompt_path]


@pytest.mark.parametrize(
    ("schema", "expected_type"),
    [
        (
            {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                },
            },
            dict,
        ),
        ({"type": "array", "items": {"type": "integer"}}, list),
        ({"type": "boolean"}, bool),
    ],
)
def test_local_deterministic_provider_respects_output_schema(schema: dict[str, object], expected_type: type[object]) -> None:
    provider = LocalDeterministicProvider()
    response = provider.generate(
        ModelRequest(
            instructions="Return a bounded answer.",
            prompt="Write a title and summary for runtime planning.",
            model_class="small",
            seed=0,
            metadata={
                "mode": "user_request",
                "payload": {"output_schema": schema},
            },
        )
    )

    payload = json.loads(response.text)
    assert isinstance(payload, expected_type)
    if isinstance(payload, dict):
        assert set(payload) == {"title", "summary"}


def test_runtime_evaluator_rejects_out_of_scope_tool_usage(runtime_dir: Path, tmp_path: Path) -> None:
    task = BenchmarkTask(
        task_id="restricted.sum",
        family="top",
        prompt="Compute the sum of the provided numbers.",
        task_type="structured_ops",
        allowed_tool_categories=["memory"],
        operations=[
            OperationSpec(
                op_id="sum",
                kind="builtin",
                output_key="sum",
                description="Compute sum of numbers",
                tool_hint="math/basic/sum_numbers",
                args={"numbers": [2, 3, 5]},
            )
        ],
        expected=10,
        verifier_type="number_exact",
    )
    suite = BenchmarkSuite(name="restricted_tools", train=[task], val=[], test=[], proxy=[task])
    evaluator = RuntimeEvaluator(
        suite,
        tmp_path / "restricted_eval",
        LocalDeterministicProvider(),
        baseline_runtime_dir=runtime_dir,
    )

    evaluation = evaluator.evaluate_runtime(runtime_dir, partition="train", seeds=[0], use_cache=False)

    assert evaluation.run_results[0].hard_invalid is True
