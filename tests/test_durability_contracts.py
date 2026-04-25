from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agintor.openai_trace import load_materialization_state, persist_openai_trace
from agintor.schemas import MemoryNode, OpenAITraceContext
from agintor.shell import FixedShell
from agintor.task_runtime.memory import MemoryMixin


def _memory_node(node_id: str, content: str, *, source_task_id: str = "task.1") -> MemoryNode:
    return MemoryNode(
        node_id=node_id,
        type="TaskNote",
        label=node_id,
        content=content,
        embedding=[],
        symbol_set=[node_id],
        file_paths=[],
        source_task_id=source_task_id,
        verifier_support=0.8,
        timestamps={},
        provenance={
            "supporting_receipt_ids": ["receipt.1"],
            "supporting_verifier_ids": ["verifier.1"],
        },
    )


def test_trace_materialization_skips_records_without_runtime_identity(tmp_path: Path, monkeypatch) -> None:
    trace_root = tmp_path / "openai_traces"
    monkeypatch.setenv("AGINTOR_OPENAI_TRACE_DIR", str(trace_root))

    first_call_id = persist_openai_trace(
        provider="openai",
        method_name="responses.create",
        model_class="default",
        model_name="gpt-test",
        reasoning_effort=None,
        instructions="plan",
        input_value="hello",
        request_payload={"model": "gpt-test"},
        request_metadata={"mode": "planning"},
        response_text="ok",
    )
    assert first_call_id
    session_dirs = list((trace_root / "sessions").iterdir())
    assert len(session_dirs) == 1
    session_dir = session_dirs[0]
    state = load_materialization_state(session_dir)
    assert state is not None
    assert state.call_count == 1
    assert state.grouped_call_count == 0
    assert state.runtime_task_keys == []
    assert not list((session_dir / "groups").rglob("calls_index.json"))
    raw_record = json.loads(next((session_dir / "calls").glob("*.json")).read_text(encoding="utf-8"))
    assert "runtime_task_key" not in raw_record

    trace_context = OpenAITraceContext(
        session_id=session_dir.name,
        request_id="request.1",
        evaluation_unit_id="evaluation.1",
        task_id="task.1",
        seed=0,
        episode_kind="single_task",
    )
    second_call_id = persist_openai_trace(
        provider="openai",
        method_name="responses.create",
        model_class="default",
        model_name="gpt-test",
        reasoning_effort=None,
        instructions="solve",
        input_value="hello",
        request_payload={"model": "gpt-test"},
        request_metadata={"mode": "user_request", "trace_context": (trace_context).model_dump()},
        response_text="ok",
    )
    assert second_call_id
    state = load_materialization_state(session_dir)
    assert state is not None
    assert state.call_count == 2
    assert state.grouped_call_count == 1
    assert state.runtime_task_keys == ["evaluation.1.task.1.seed_0.single_task"]
    assert len(list((session_dir / "groups").rglob("calls_index.json"))) == 1


def test_memory_promotion_uses_policy_hook_with_durable_write_scope(tmp_path: Path) -> None:
    shell = FixedShell(tmp_path, run_id="run.1", attempt_id="attempt.1")
    existing = _memory_node("existing", "old content")
    shell.long_term.upsert(existing)

    class RecordingMemoryPolicy:
        def __init__(self) -> None:
            self.upsert_calls: list[tuple[str, str | None]] = []

        def score_memory_unit(self, ctx, unit, existing_nodes) -> float:
            return 1.0

        def should_promote(self, ctx, unit, score) -> bool:
            return True

        def dedup_candidates(self, ctx, unit, existing_nodes) -> tuple[str, str | None]:
            return "merge", existing.node_id

        def upsert_memory(self, ctx, unit, action, target_id) -> None:
            self.upsert_calls.append((action, target_id))
            target = ctx.shell.long_term.nodes[target_id]
            merged = target.model_copy(
                update={
                    "content": unit.content,
                    "symbol_set": sorted(set(target.symbol_set) | set(unit.symbol_set)),
                    "verifier_support": max(target.verifier_support, unit.verifier_support),
                }
            )
            ctx.shell.long_term.upsert(merged)

    class RuntimeMemoryHarness(MemoryMixin):
        pass

    policy = RecordingMemoryPolicy()
    harness = RuntimeMemoryHarness()
    harness.runtime = SimpleNamespace(memory=policy)
    harness.shell = shell
    events: list[tuple[str, dict[str, object]]] = []
    context = SimpleNamespace(
        shell=shell,
        task=SimpleNamespace(task_id="task.1"),
        state=SimpleNamespace(latest_checkpoint_ref="checkpoint.1", promoted_nodes=0),
        record=lambda event, **payload: events.append((event, payload)),
    )

    harness._promote_memory_candidate(context, _memory_node("candidate", "new detailed content"))

    assert policy.upsert_calls == [("merge", existing.node_id)]
    assert context.state.promoted_nodes == 1
    assert events[-1][0] == "memory_promoted"
    assert shell.long_term.nodes[existing.node_id].content == "new detailed content"
    latest_write = shell.long_term.write_log[-1]
    assert latest_write.action == "merge"
    assert latest_write.target_node_id == existing.node_id
    assert latest_write.source_task_id == "task.1"
    assert latest_write.source_attempt_id == "attempt.1"
    assert latest_write.source_checkpoint_ref == "checkpoint.1"
    assert latest_write.verifier_support_refs == ["receipt.1", "verifier.1"]

