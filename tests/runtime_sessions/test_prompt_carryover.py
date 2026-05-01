from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace


def test_session_seed_short_term_carryover_reaches_direct_response_prompt() -> None:
    from agintor.contracts import BenchmarkTask
    from agintor.runtime.kernel.operations import OperationsMixin

    task = BenchmarkTask(
        task_id="user.req.direct",
        family="e2e",
        prompt="What was the launch keyword?",
        task_type="direct_answer",
        operations=[],
        expected=None,
    )
    context = SimpleNamespace(
        task=task,
        shell=SimpleNamespace(
            message_board=SimpleNamespace(
                entries=[
                    {
                        "kind": "session_carryover",
                        "payload": {
                            "kind": "assistant_summary",
                            "content": "The launch keyword was rosebud.",
                        },
                    }
                ]
            )
        ),
    )

    prompt = OperationsMixin()._direct_response_prompt(context, {})

    assert "Session carryover:" in prompt
    assert "rosebud" in prompt


def test_session_seed_short_term_carryover_reaches_repo_patch_prompt(tmp_path: Path) -> None:
    from agintor.contracts import BenchmarkTask
    from agintor.runtime.kernel.io import BoundedIOMixin
    from agintor.runtime.kernel.operations import OperationsMixin

    class RuntimeHarness(OperationsMixin, BoundedIOMixin):
        def __init__(self, workspace: Path) -> None:
            self.runtime = SimpleNamespace(
                deployment_contract=SimpleNamespace(
                    filesystem_policy="read-only",
                    runtime_isolation_policy=SimpleNamespace(workspace_root=str(workspace)),
                )
            )

    target_file = tmp_path / "answer.txt"
    target_file.write_text("before", encoding="utf-8")
    captured: dict[str, str] = {}

    def run_model_request(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        return SimpleNamespace(
            text=json.dumps(
                {
                    "summary": "ok",
                    "files": [
                        {
                            "path": str(target_file),
                            "updated_content": "after",
                        }
                    ],
                }
            )
        )

    context = SimpleNamespace(
        request_id="solve.user.seed_0",
        runtime_backend="local",
        plan=SimpleNamespace(plan_id="plan.1"),
        active_frame=SimpleNamespace(frame_id="frame.root", worker_id=None),
        state=SimpleNamespace(side_effect_receipts=[]),
        task=BenchmarkTask(
            task_id="user.req.patch",
            family="e2e",
            prompt="Patch the answer file.",
            task_type="bounded_repo_patch",
            file_paths=[str(target_file)],
            operations=[],
            expected=None,
        ),
        shell=SimpleNamespace(
            workspace=tmp_path,
            message_board=SimpleNamespace(
                entries=[
                    {
                        "kind": "session_carryover",
                        "payload": {
                            "kind": "assistant_summary",
                            "content": "The launch keyword was rosebud.",
                        },
                    }
                ]
            ),
        ),
        run_model_request=run_model_request,
        record=lambda *args, **kwargs: None,
        record_side_effect=lambda receipt: context.state.side_effect_receipts.append(receipt.model_dump()),
        publish_checkpoint_boundary=lambda *args, **kwargs: None,
        raise_if_cancelled=lambda: None,
    )

    output = RuntimeHarness(tmp_path)._execute_repo_patch_node(
        context,
        SimpleNamespace(node_id="patch.1"),
        {
            "target_file_paths": [str(target_file)],
            "file_snapshots": [
                {
                    "path": str(target_file),
                    "content": "before",
                    "exists": True,
                }
            ],
        },
        "default",
        None,
    )

    assert output["applied"] is False
    assert "Session carryover:" in captured["prompt"]
    assert "rosebud" in captured["prompt"]
