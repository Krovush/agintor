from __future__ import annotations

from typing import Any

from ...contracts import CheckpointEnvelope
from .state import LangGraphRuntimeState

LANGGRAPH_STATE_KEY = "langgraph_runtime_state"


def embed_langgraph_state(envelope: CheckpointEnvelope, state: LangGraphRuntimeState | dict[str, Any]) -> CheckpointEnvelope:
    payload = state.model_dump(mode="json") if hasattr(state, "model_dump") else dict(state)
    snapshot = dict(envelope.runtime_state_snapshot or {})
    snapshot[LANGGRAPH_STATE_KEY] = payload
    return envelope.model_copy(update={"runtime_state_snapshot": snapshot}, deep=True)


def extract_langgraph_state(envelope: CheckpointEnvelope) -> LangGraphRuntimeState | None:
    payload = dict(envelope.runtime_state_snapshot or {}).get(LANGGRAPH_STATE_KEY)
    if not isinstance(payload, dict):
        return None
    return LangGraphRuntimeState.model_validate(payload)


__all__ = ["LANGGRAPH_STATE_KEY", "embed_langgraph_state", "extract_langgraph_state"]
