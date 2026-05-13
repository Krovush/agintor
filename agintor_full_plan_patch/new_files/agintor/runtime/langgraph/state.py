from __future__ import annotations

from typing import Any, TypedDict


class LangGraphRuntimeState(TypedDict, total=False):
    request_id: str
    plan_id: str
    runtime_id: str
    runtime_spec_digest: str
    current_node_id: str
    completed_node_ids: list[str]
    artifacts: dict[str, Any]
    trace_rows: list[dict[str, Any]]
    side_effect_receipts: list[dict[str, Any]]
    budget: dict[str, Any]
    checkpoint_ref: str
    error: str


def initial_langgraph_state(*, request_id: str, plan_id: str, runtime_id: str, runtime_spec_digest: str) -> LangGraphRuntimeState:
    return {
        "request_id": request_id,
        "plan_id": plan_id,
        "runtime_id": runtime_id,
        "runtime_spec_digest": runtime_spec_digest,
        "completed_node_ids": [],
        "artifacts": {},
        "trace_rows": [],
        "side_effect_receipts": [],
        "budget": {},
    }


__all__ = ["LangGraphRuntimeState", "initial_langgraph_state"]
