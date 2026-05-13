from __future__ import annotations

from agintor.contracts import SpecAction, apply_spec_actions, default_langgraph_runtime_spec


def test_spec_action_prompt_mutation_changes_digest():
    spec = default_langgraph_runtime_spec(runtime_id="r1", name="Runtime")
    action = SpecAction(
        action_id="a1",
        action_type="set_prompt",
        target_ids=["agent.default"],
        scope=["top"],
        patch={"prompt": "new prompt"},
    )
    app = apply_spec_actions(spec, [action])
    assert app.changed
    assert app.parent_spec_digest != app.child_spec_digest
