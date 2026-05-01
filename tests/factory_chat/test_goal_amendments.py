from __future__ import annotations

import pytest

from agintor.factory.goals import amend_goal_spec, build_goal_spec


def test_amend_goal_spec_preserves_goal_id_and_extends_history() -> None:
    initial = build_goal_spec(
        "Build a memory retrieval runtime",
        runtime_provider_name="local",
        default_runtime_backend="local",
    )
    assert initial.amendment_index == 0
    assert initial.amendment_history == []

    amended = amend_goal_spec(
        initial,
        "Also surface citations alongside retrieved evidence.",
        runtime_provider_name="local",
        default_runtime_backend="local",
    )
    assert amended.goal_id == initial.goal_id
    assert amended.amendment_index == 1
    assert amended.amendment_history == [
        "Also surface citations alongside retrieved evidence.",
    ]
    assert "citations" in amended.normalized_goal
    assert amended.raw_prompt == "Also surface citations alongside retrieved evidence."

    twice = amend_goal_spec(
        amended,
        "Prefer cheaper providers when quality is comparable.",
        runtime_provider_name="local",
        default_runtime_backend="local",
    )
    assert twice.goal_id == initial.goal_id
    assert twice.amendment_index == 2
    assert len(twice.amendment_history) == 2


def test_amend_goal_spec_rejects_empty_instruction() -> None:
    initial = build_goal_spec(
        "Build a memory retrieval runtime",
        runtime_provider_name="local",
    )
    with pytest.raises(ValueError):
        amend_goal_spec(initial, "")
