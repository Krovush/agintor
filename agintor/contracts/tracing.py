from __future__ import annotations

import base64
import json
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, NamedTuple, Optional, Sequence
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from ..utils import cheap_embedding, now_ts, stable_hash

class OpenAITraceContext(BaseModel):
    session_id: Optional[str] = None
    provider_role: Optional[str] = None
    build_id: Optional[str] = None
    runtime_hash: Optional[str] = None
    runtime_dir: Optional[str] = None
    task_id: Optional[str] = None
    seed: Optional[int] = None
    request_id: Optional[str] = None
    evaluation_unit_id: Optional[str] = None
    request_mode: Optional[Literal["benchmark", "user_request", "batch"]] = None
    episode_kind: Optional[Literal["transfer_episode"]] = None
    episode_step_index: Optional[int] = None
    factory_chat_id: Optional[str] = None
    factory_message_id: Optional[str] = None
    factory_message_index: Optional[int] = None
    runtime_session_id: Optional[str] = None
    runtime_message_id: Optional[str] = None
    runtime_message_index: Optional[int] = None
    iteration: Optional[int] = None
    objective: Optional[str] = None
    touched_scope: List[str] = Field(default_factory=list)
    agent_id: Optional[str] = None
    frame_role: Optional[str] = None
    worker_id: Optional[str] = None
    op_id: Optional[str] = None
    run_node_id: Optional[str] = None

    @field_validator("episode_kind", mode="before")
    @classmethod
    def normalize_episode_kind(cls, value: Any) -> Any:
        text = str(value or "").strip()
        if text == "transfer_episode":
            return text
        return None

    @model_validator(mode="after")
    def normalize_episode_scope(self) -> "OpenAITraceContext":
        if self.episode_kind != "transfer_episode":
            self.episode_step_index = None
        return self


class RuntimeEvent(BaseModel):
    event: Literal[
        "run_started",
        "plan_compiled",
        "plan_loaded",
        "plan_validation_failed",
        "mode_selected",
        "node_started",
        "node_completed",
        "node_failed",
        "node_reused_from_checkpoint",
        "node_recovery_blocked",
        "branch_started",
        "branch_completed",
        "branch_cancelled",
        "branch_failed",
        "branch_frontier_restored",
        "branch_skipped",
        "side_effect_recorded",
        "side_effect_reconciled",
        "checkpoint_published",
        "checkpoint_restored",
        "merge_started",
        "merge_completed",
        "model_response",
        "model_assigned",
        "tool_operation",
        "tool_fault",
        "tool_promoted",
        "checks_skipped",
        "checks_trimmed",
        "checks_requested",
        "check_result",
        "memory_promoted",
        "context_ingested",
        "compaction",
        "agent_created",
        "agent_reused",
        "terminal_emitted",
        "run_failed",
        "run_cancelled",
    ]
    event_id: str
    sequence_no: int = 0
    created_at: float = 0.0
    execution_state: Literal[
        "idle",
        "compiling",
        "validating",
        "running",
        "branching",
        "merging",
        "completing",
        "completed",
        "failed",
        "cancelled",
    ] = "idle"
    request_id: str
    plan_id: str
    trace_context: Optional[OpenAITraceContext] = None
    frame_id: Optional[str] = None
    branch_id: Optional[str] = None
    node_id: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)

    def trace_row(self) -> Dict[str, Any]:
        row: Dict[str, Any] = {
            "event": self.event,
            "event_id": self.event_id,
            "sequence_no": self.sequence_no,
            "created_at": self.created_at,
            "execution_state": self.execution_state,
            "request_id": self.request_id,
            "plan_id": self.plan_id,
        }
        if self.trace_context is not None:
            row["trace_context"] = self.trace_context.model_dump(exclude_none=True)
        if self.frame_id:
            row["frame_id"] = self.frame_id
        if self.branch_id:
            row["branch_id"] = self.branch_id
        if self.node_id:
            row["node_id"] = self.node_id
        row.update(self.payload)
        return row
