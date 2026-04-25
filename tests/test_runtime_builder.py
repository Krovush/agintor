from __future__ import annotations

from agintor.goal_rubric import build_goal_spec
from agintor.runtime_builder import (
    _build_benchmark_plan,
    _build_verifier_bundle,
    _normalize_benchmark_plan_against_suite,
    build_goal_conditioned_suite,
)
from agintor.runtime_profile import load_runtime_profile
from agintor.schemas import BenchmarkPlan


def _profile():
    return load_runtime_profile()


def _goal(prompt: str):
    profile = _profile()
    return build_goal_spec(prompt, runtime_provider_name=profile.runtime_provider.name)


def _suite(goal_spec):
    return build_goal_conditioned_suite(goal_spec, _profile())


def _covered_families(task_ids, family_map, *, excluded_task_ids=()):
    excluded = set(excluded_task_ids)
    return {
        family_map[task_id]
        for task_id in task_ids
        if task_id in family_map and task_id not in excluded
    }


def test_normalization_resets_train_partition_when_synthetic_ids_are_updated_but_train_is_stale():
    old_goal = _goal("Build an end-to-end reporting runtime")
    new_goal = _goal("Build a memory retrieval runtime")
    old_plan = _build_benchmark_plan(old_goal, _suite(old_goal))
    new_suite = _suite(new_goal)
    expected_plan = _build_benchmark_plan(new_goal, new_suite)

    payload = (old_plan).model_dump()
    payload["family_targets"] = list(new_goal.target_families)
    payload["synthetic_task_ids"] = list(expected_plan.synthetic_task_ids)

    normalized = _normalize_benchmark_plan_against_suite(
        (BenchmarkPlan).model_validate(payload),
        new_suite,
        goal_spec=new_goal,
    )

    assert normalized.synthetic_task_ids == expected_plan.synthetic_task_ids
    assert normalized.train_task_ids == expected_plan.train_task_ids
    assert all(task_id in normalized.train_task_ids for task_id in normalized.synthetic_task_ids)


def test_normalization_resets_stale_synthetic_and_train_task_ids_to_rebuilt_defaults():
    old_goal = _goal("Build an end-to-end reporting runtime")
    new_goal = _goal("Build a memory retrieval runtime")
    old_plan = _build_benchmark_plan(old_goal, _suite(old_goal))
    new_suite = _suite(new_goal)
    expected_plan = _build_benchmark_plan(new_goal, new_suite)

    payload = (old_plan).model_dump()
    payload["family_targets"] = list(new_goal.target_families)

    normalized = _normalize_benchmark_plan_against_suite(
        (BenchmarkPlan).model_validate(payload),
        new_suite,
        goal_spec=new_goal,
    )

    assert normalized.synthetic_task_ids == expected_plan.synthetic_task_ids
    assert normalized.train_task_ids == expected_plan.train_task_ids


def test_normalization_preserves_valid_train_coverage_when_synthetic_ids_are_already_present():
    goal_spec = _goal("Build a memory retrieval runtime")
    suite = _suite(goal_spec)
    plan = _build_benchmark_plan(goal_spec, suite)

    normalized = _normalize_benchmark_plan_against_suite(
        (BenchmarkPlan).model_validate((plan).model_dump()),
        suite,
        goal_spec=goal_spec,
    )

    assert normalized.train_task_ids == plan.train_task_ids
    assert normalized.synthetic_task_ids == plan.synthetic_task_ids


def test_verifier_bundle_covers_goal_conditioned_tasks_after_partial_plan_update_normalization():
    old_goal = _goal("Build an end-to-end reporting runtime")
    new_goal = _goal("Build a memory retrieval runtime")
    old_plan = _build_benchmark_plan(old_goal, _suite(old_goal))
    new_suite = _suite(new_goal)
    expected_plan = _build_benchmark_plan(new_goal, new_suite)

    payload = (old_plan).model_dump()
    payload["family_targets"] = list(new_goal.target_families)
    payload["synthetic_task_ids"] = list(expected_plan.synthetic_task_ids)

    normalized = _normalize_benchmark_plan_against_suite(
        (BenchmarkPlan).model_validate(payload),
        new_suite,
        goal_spec=new_goal,
    )
    verifier_bundle = _build_verifier_bundle(normalized, new_suite)
    covered_task_ids = {
        spec.artifact_contract["task_id"]
        for spec in verifier_bundle.verifiers
        if spec.artifact_contract.get("task_id")
    }

    assert set(normalized.synthetic_task_ids).issubset(covered_task_ids)


def test_normalization_restores_missing_target_family_pressure_for_partial_partition_hints():
    goal_spec = _goal("Build a checkpoint orchestration runtime")
    suite = _suite(goal_spec)
    plan = _build_benchmark_plan(goal_spec, suite)

    payload = (plan).model_dump()
    payload["train_task_ids"] = [
        task_id for task_id in plan.train_task_ids if task_id.startswith("top.")
    ] + list(plan.synthetic_task_ids)
    payload["val_task_ids"] = [
        task_id for task_id in plan.val_task_ids if task_id.startswith("val.top.")
    ]
    payload["test_task_ids"] = [
        task_id for task_id in plan.test_task_ids if task_id.startswith("test.top.")
    ]

    normalized = _normalize_benchmark_plan_against_suite(
        (BenchmarkPlan).model_validate(payload),
        suite,
        goal_spec=goal_spec,
    )

    assert payload["train_task_ids"] == normalized.train_task_ids[: len(payload["train_task_ids"])]
    assert payload["val_task_ids"] == normalized.val_task_ids[: len(payload["val_task_ids"])]
    assert payload["test_task_ids"] == normalized.test_task_ids[: len(payload["test_task_ids"])]
    assert set(goal_spec.target_families).issubset(
        _covered_families(
            normalized.train_task_ids,
            suite.task_family_map("train"),
            excluded_task_ids=normalized.synthetic_task_ids,
        )
    )
    assert set(goal_spec.target_families).issubset(
        _covered_families(normalized.val_task_ids, suite.task_family_map("val"))
    )
    assert set(goal_spec.target_families).issubset(
        _covered_families(normalized.test_task_ids, suite.task_family_map("test"))
    )


def test_normalization_preserves_partial_partition_hints_when_family_coverage_is_already_satisfied():
    goal_spec = _goal("Build a checkpoint orchestration runtime")
    suite = _suite(goal_spec)
    plan = _build_benchmark_plan(goal_spec, suite)
    train_family_map = suite.task_family_map("train")
    val_family_map = suite.task_family_map("val")
    test_family_map = suite.task_family_map("test")

    payload = (plan).model_dump()
    payload["train_task_ids"] = [
        next(task_id for task_id in plan.train_task_ids if train_family_map[task_id] == "top"),
        next(task_id for task_id in plan.train_task_ids if train_family_map[task_id] == "mem"),
        *plan.synthetic_task_ids,
    ]
    payload["val_task_ids"] = [
        next(task_id for task_id in plan.val_task_ids if val_family_map[task_id] == "top"),
        next(task_id for task_id in plan.val_task_ids if val_family_map[task_id] == "mem"),
    ]
    payload["test_task_ids"] = [
        next(task_id for task_id in plan.test_task_ids if test_family_map[task_id] == "top"),
        next(task_id for task_id in plan.test_task_ids if test_family_map[task_id] == "mem"),
    ]

    normalized = _normalize_benchmark_plan_against_suite(
        (BenchmarkPlan).model_validate(payload),
        suite,
        goal_spec=goal_spec,
    )

    assert normalized.train_task_ids == payload["train_task_ids"]
    assert normalized.val_task_ids == payload["val_task_ids"]
    assert normalized.test_task_ids == payload["test_task_ids"]
