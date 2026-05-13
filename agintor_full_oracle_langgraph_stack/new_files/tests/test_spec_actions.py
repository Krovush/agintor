from __future__ import annotations

from agintor.contracts import SpecAction, apply_spec_action, baseline_langgraph_runtime_spec


def test_spec_action_set_prompt_updates_digest_and_history():
    parent = baseline_langgraph_runtime_spec(runtime_id="r3")
    action = SpecAction(
        action_id="a1",
        action_type="set_prompt",
        target_ids=["root"],
        scope=["top"],
        patch={"output_instructions": "Return JSON only."},
    )
    child, result = apply_spec_action(parent, action)
    assert result.applied is True
    assert result.parent_spec_digest == parent.spec_digest
    assert result.child_spec_digest == child.spec_digest
    assert child.spec_digest != parent.spec_digest
    assert child.mutation_history[-1].action_id == "a1"
