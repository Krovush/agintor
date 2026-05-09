from __future__ import annotations

import random
from typing import Any, Literal, Sequence

from pydantic import BaseModel, Field

from ..contracts import BenchmarkTask, OperationSpec
from ..utils import stable_hash


TOOL_WORKFLOW_GENERATOR_ID = "generated_tool_workflow_v1"
TOOL_WORKFLOW_GENERATOR_VERSION = "1.0"


class ToolWorkflowDifficulty(BaseModel):
    expression_depth: int = Field(default=3, ge=1, le=8)
    dependency_width: int = Field(default=1, ge=1, le=4)
    distractor_count: int = Field(default=1, ge=0, le=8)
    numeric_edge_cases: bool = False
    include_metamorphic_tags: bool = True


class ToolWorkflowStep(BaseModel):
    step_id: str
    output_key: str
    op: Literal["sum_squares", "range_span", "add_offset", "multiply_mod", "abs_shift", "neutral_shift"]
    args: dict[str, Any] = Field(default_factory=dict)
    dependencies: list[str] = Field(default_factory=list)


def _number_value(value: int | float) -> int | float:
    as_float = float(value)
    return int(as_float) if as_float.is_integer() else round(as_float, 9)


def _rng(seed: int, partition: str, index: int, difficulty: ToolWorkflowDifficulty) -> random.Random:
    return random.Random(stable_hash(TOOL_WORKFLOW_GENERATOR_ID, TOOL_WORKFLOW_GENERATOR_VERSION, seed, partition, index, difficulty.model_dump()))


def _choose_numbers(rng: random.Random, *, numeric_edge_cases: bool) -> list[int]:
    if not numeric_edge_cases:
        return [rng.randint(1, 9) for _ in range(rng.randint(3, 6))]
    pool = [-7, -3, -1, 0, 1, 2, 5, 8, 13]
    return [pool[rng.randrange(len(pool))] for _ in range(rng.randint(3, 6))]


def _dependency_value(step: ToolWorkflowStep, values_by_step_id: dict[str, Any]) -> int | float:
    values = [values_by_step_id[dependency] for dependency in step.dependencies]
    return _number_value(sum(values))


def _dependency_expression(step: ToolWorkflowStep, output_key_by_step_id: dict[str, str]) -> str:
    sources = [output_key_by_step_id[dependency] for dependency in step.dependencies]
    if len(sources) == 1:
        return sources[0]
    return "(" + " + ".join(sources) + ")"


def _interpret_step(step: ToolWorkflowStep, values_by_step_id: dict[str, Any]) -> int | float:
    if step.op == "sum_squares":
        return _number_value(sum(value * value for value in step.args["numbers"]))
    if step.op == "range_span":
        numbers = step.args["numbers"]
        return _number_value(max(numbers) - min(numbers))
    source = _dependency_value(step, values_by_step_id)
    if step.op == "add_offset":
        return _number_value(source + step.args["offset"])
    if step.op == "multiply_mod":
        return _number_value((source * step.args["multiplier"]) % step.args["modulus"])
    if step.op == "abs_shift":
        return _number_value(abs(source - step.args["pivot"]) + step.args["offset"])
    if step.op == "neutral_shift":
        return _number_value((source + step.args["neutral"]) - step.args["neutral"])
    raise ValueError(f"unsupported tool workflow op {step.op!r}")


def _step_expression(step: ToolWorkflowStep, output_key_by_step_id: dict[str, str]) -> str:
    if step.op == "sum_squares":
        return "sum(x*x for x in numbers)"
    if step.op == "range_span":
        return "max(numbers) - min(numbers)"
    source = _dependency_expression(step, output_key_by_step_id)
    if step.op == "add_offset":
        return f"{source} + offset"
    if step.op == "multiply_mod":
        return f"({source} * multiplier) % modulus"
    if step.op == "abs_shift":
        return f"abs({source} - pivot) + offset"
    if step.op == "neutral_shift":
        return f"({source} + neutral) - neutral"
    raise ValueError(f"unsupported tool workflow op {step.op!r}")


def _steps_to_operations(steps: Sequence[ToolWorkflowStep]) -> list[OperationSpec]:
    output_key_by_step_id = {step.step_id: step.output_key for step in steps}
    operations: list[OperationSpec] = []
    for step in steps:
        operations.append(
            OperationSpec(
                op_id=step.step_id,
                kind="generated_expression",
                output_key=step.output_key,
                description=f"Compute {step.op.replace('_', ' ')}.",
                expression=_step_expression(step, output_key_by_step_id),
                args=dict(step.args),
                dependencies=list(step.dependencies),
                externally_visible=step is steps[-1],
            )
        )
    return operations


def _build_steps(rng: random.Random, difficulty: ToolWorkflowDifficulty) -> list[ToolWorkflowStep]:
    steps = [
        ToolWorkflowStep(
            step_id="step_0",
            output_key="value_0",
            op="sum_squares" if rng.random() < 0.65 else "range_span",
            args={"numbers": _choose_numbers(rng, numeric_edge_cases=difficulty.numeric_edge_cases)},
        )
    ]
    for index in range(1, difficulty.expression_depth):
        dependency_count = min(difficulty.dependency_width, len(steps))
        previous = steps[-1].step_id
        earlier = [step.step_id for step in steps[:-1]]
        rng.shuffle(earlier)
        dependencies = [previous, *earlier[: max(0, dependency_count - 1)]]
        op = ["add_offset", "multiply_mod", "abs_shift"][rng.randrange(3)]
        if op == "add_offset":
            args: dict[str, Any] = {"offset": rng.randint(-9, 17)}
        elif op == "multiply_mod":
            args = {"multiplier": rng.randint(2, 7), "modulus": rng.choice([5, 7, 11, 13, 17, 19])}
        else:
            args = {"pivot": rng.randint(-12, 24), "offset": rng.randint(0, 9)}
        steps.append(ToolWorkflowStep(step_id=f"step_{index}", output_key=f"value_{index}", op=op, args=args, dependencies=dependencies))
    if difficulty.include_metamorphic_tags:
        dependency_count = min(difficulty.dependency_width, len(steps))
        previous = steps[-1].step_id
        earlier = [step.step_id for step in steps[:-1]]
        rng.shuffle(earlier)
        steps.append(
            ToolWorkflowStep(
                step_id=f"step_{len(steps)}",
                output_key=f"value_{len(steps)}",
                op="neutral_shift",
                args={"neutral": rng.randint(3, 31)},
                dependencies=[previous, *earlier[: max(0, dependency_count - 1)]],
            )
        )
    return steps


def make_tool_workflow_challenge(
    *,
    seed: int,
    partition: Literal["explore", "train", "validation", "confirmatory", "heldout", "val", "test", "proxy"],
    index: int,
    difficulty: ToolWorkflowDifficulty | dict[str, Any] | None = None,
    task_id_prefix: str = "tool.frontier",
) -> BenchmarkTask:
    difficulty_model = difficulty if isinstance(difficulty, ToolWorkflowDifficulty) else ToolWorkflowDifficulty.model_validate(difficulty or {})
    rng = _rng(seed, partition, index, difficulty_model)
    challenge_id = f"{task_id_prefix}.{partition}.{index:03d}.{stable_hash(seed, partition, index, difficulty_model.model_dump())[:8]}"
    steps = _build_steps(rng, difficulty_model)
    values_by_step_id: dict[str, Any] = {}
    for step in steps:
        values_by_step_id[step.step_id] = _interpret_step(step, values_by_step_id)
    expected = _number_value(values_by_step_id[steps[-1].step_id])
    private_digest = stable_hash(TOOL_WORKFLOW_GENERATOR_ID, TOOL_WORKFLOW_GENERATOR_VERSION, challenge_id, expected)[:16]
    slice_tags = [
        "tool",
        "generated",
        "frontier",
        f"depth:{difficulty_model.expression_depth}",
        f"distractors:{difficulty_model.distractor_count}",
    ]
    if difficulty_model.numeric_edge_cases:
        slice_tags.append("numeric_edge_cases")
    prompt = (
        "Evaluate the generated tool workflow. Use only the declared operation inputs and dependencies, "
        f"ignore context items tagged as distractors, and return only the final numeric value for {steps[-1].output_key}."
    )
    return BenchmarkTask(
        task_id=challenge_id,
        family="tool",
        task_type="tool_expression",
        prompt=prompt,
        allowed_tool_categories=["generated/local"],
        context_items=[
            {"symbol": f"DISTRACTOR_{index}_{offset}", "value": rng.randint(-50, 50), "tag": "distractor"}
            for offset in range(difficulty_model.distractor_count)
        ],
        operations=_steps_to_operations(steps),
        expected=None,
        private_expected=expected,
        verifier_type="number_exact",
        proxy_scope_tags=["tool", "ctl"],
        metadata={
            "domain_kind": "generated_tool_workflow",
            "generator_id": TOOL_WORKFLOW_GENERATOR_ID,
            "generator_version": TOOL_WORKFLOW_GENERATOR_VERSION,
            "challenge_id": challenge_id,
            "private_answer_ref": f"private://{TOOL_WORKFLOW_GENERATOR_ID}/{challenge_id}/{private_digest}",
            "private_answer_mechanism": "tool_workflow_interpreter_v1",
            "difficulty": difficulty_model.model_dump(),
            "slice_tags": slice_tags,
            "metamorphic_tags": ["neutral_shift_equivalence", "distractor_insertion"] if difficulty_model.include_metamorphic_tags else [],
            "public_payload": {
                "prompt": prompt,
                "operation_count": len(steps),
                "terminal_output_key": steps[-1].output_key,
                "slice_tags": slice_tags,
                "distractor_count": difficulty_model.distractor_count,
            },
        },
    )


def generate_tool_workflow_challenges(
    *,
    partition: Literal["explore", "train", "validation", "confirmatory", "heldout", "val", "test", "proxy"],
    count: int,
    seed: int = 0,
    difficulty: ToolWorkflowDifficulty | dict[str, Any] | None = None,
    task_id_prefix: str = "tool.frontier",
) -> list[BenchmarkTask]:
    return [
        make_tool_workflow_challenge(seed=seed, partition=partition, index=index, difficulty=difficulty, task_id_prefix=task_id_prefix)
        for index in range(count)
    ]
