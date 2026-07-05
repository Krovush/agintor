from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ...contracts import OpenAITraceContext


class LangGraphRuntimeState(BaseModel):
    request_id: str = ""
    task_id: str = ""
    seed: int = 0
    prompt: str = ""
    runtime_hash: str = ""
    runtime_spec_digest: str = ""
    trace_context: OpenAITraceContext | None = None
    current_node_id: str = ""
    artifacts: dict[str, Any] = Field(default_factory=dict)
    node_results: dict[str, Any] = Field(default_factory=dict)
    trace: list[dict[str, Any]] = Field(default_factory=list)
    budget: dict[str, Any] = Field(default_factory=dict)
    side_effect_receipts: list[dict[str, Any]] = Field(default_factory=list)
    status: Literal["running", "completed", "failed"] = "running"
    error: str = ""


class LangGraphNodeResult(BaseModel):
    node_id: str
    output_key: str = ""
    output: Any = None
    status: Literal["completed", "failed", "skipped"] = "completed"
    trace_rows: list[dict[str, Any]] = Field(default_factory=list)


__all__ = ["LangGraphNodeResult", "LangGraphRuntimeState"]
