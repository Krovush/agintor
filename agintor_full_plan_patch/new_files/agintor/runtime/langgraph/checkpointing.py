from __future__ import annotations

from typing import Any

from ...contracts import CheckpointEnvelope
from ...utils import stable_hash
from .state import LangGraphRuntimeState


def langgraph_state_digest(state: LangGraphRuntimeState) -> str:
    return stable_hash(dict(state))


def state_to_checkpoint_payload(state: LangGraphRuntimeState) -> dict[str, Any]:
    return {"langgraph_state": dict(state), "state_digest": langgraph_state_digest(state)}


def state_from_checkpoint_payload(payload: dict[str, Any]) -> LangGraphRuntimeState:
    return dict(payload.get("langgraph_state", {}))


def embed_langgraph_state_in_checkpoint(envelope: CheckpointEnvelope, state: LangGraphRuntimeState) -> CheckpointEnvelope:
    payload = envelope.model_dump(mode="json", exclude_none=True)
    payload.setdefault("metadata", {})["langgraph_state"] = state_to_checkpoint_payload(state)
    return CheckpointEnvelope.model_validate(payload)


__all__ = [
    "embed_langgraph_state_in_checkpoint",
    "langgraph_state_digest",
    "state_from_checkpoint_payload",
    "state_to_checkpoint_payload",
]
