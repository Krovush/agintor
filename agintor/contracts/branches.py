from __future__ import annotations

import base64
import json
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, NamedTuple, Optional, Sequence
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from ..utils import cheap_embedding, now_ts, stable_hash

from .providers import ReplayAllocation
from .state import AgentTemplate, Checkpoint, RuntimeBudgetTotalsSnapshot, ShellStateSnapshot
from .tracing import OpenAITraceContext

class BranchBudget(BaseModel):
    model_calls_max: int = 0
    checks_max: int = 0
    latency_max: float = 0.0
    allow_tool_synthesis: bool = False


class BranchPlan(BaseModel):
    branch_id: str
    parent_frame_id: str
    request_id: str
    trace_context: Optional[OpenAITraceContext] = None
    assigned_node_ids: List[str] = Field(default_factory=list)
    merge_priority: int = 0
    predicted_solve: float = 0.0
    reserved_budget: BranchBudget = Field(default_factory=BranchBudget)
    replay_allocation: Optional[ReplayAllocation] = None
    cancel_on_parent_stop: bool = True


class CancellationRecord(BaseModel):
    reason: Literal[
        "fatal_branch_fault",
        "budget_exhaustion",
        "superior_branch_dominance",
        "verification_failure",
        "parent_stop_policy",
        "external_interrupt",
    ]
    details: Dict[str, Any] = Field(default_factory=dict)
    created_at: float = 0.0


class BranchPublication(BaseModel):
    publication_id: str
    publication_kind: str
    logical_key: str
    sequence_no: int
    accepted: bool = False
    branch_id: str = ""
    trace_context: Optional[OpenAITraceContext] = None
    verifier_support: float = 0.0
    unresolved_critical: int = 0
    branch_rank: int = 0
    payload: Dict[str, Any] = Field(default_factory=dict)


BranchFailureKind = Literal[
    "reservation_exceeded",
    "branch_execution_error",
    "cleanup_failure",
    "verification_failure",
    "protocol_failure",
]


class BranchState(BaseModel):
    branch_id: str
    status: Literal["pending", "running", "completed", "cancelled", "failed"]
    parent_frame_id: str = ""
    assigned_node_ids: List[str] = Field(default_factory=list)
    merge_priority: int = 0
    predicted_solve: float = 0.0
    reserved_budget: BranchBudget = Field(default_factory=BranchBudget)
    publications: List[BranchPublication] = Field(default_factory=list)
    budget_consumed: Dict[str, Any] = Field(default_factory=dict)
    verifier_support: float = 0.0
    unresolved_critical: int = 0
    cancellation_record: Optional[CancellationRecord] = None
    failure_kind: Optional[BranchFailureKind] = None
    failure_details: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None

    @model_validator(mode="after")
    def validate_terminal_contract(self) -> "BranchState":
        status = str(self.status or "")
        cancellation_record = self.cancellation_record
        failure_kind = self.failure_kind
        failure_details = dict(self.failure_details or {})
        if status == "cancelled":
            if cancellation_record is None:
                raise ValueError("cancelled branches require cancellation_record")
            if failure_kind is not None or failure_details:
                raise ValueError("cancelled branches may not carry failure_kind/failure_details")
            return self
        if status == "failed":
            if cancellation_record is not None:
                raise ValueError("failed branches may not carry cancellation_record")
            if failure_kind is None:
                raise ValueError("failed branches require failure_kind")
            return self
        if cancellation_record is not None:
            raise ValueError("only cancelled branches may carry cancellation_record")
        if failure_kind is not None or failure_details:
            raise ValueError("only failed branches may carry failure_kind/failure_details")
        return self


class BranchResult(BaseModel):
    branch_plan: BranchPlan
    branch_state: BranchState
    artifact: Any = None
    verifier_support: float = 0.0
    unresolved_critical: int = 0
    side_effect_receipts: List["SideEffectReceipt"] = Field(default_factory=list)
    provider_usage: Dict[str, Any] = Field(default_factory=dict)


class QueuedAgentSnapshot(BaseModel):
    restore_mode: Literal["canonical_clone", "serialized_ephemeral"]
    canonical_agent_id: Optional[str] = None
    agent_payload: AgentTemplate


class QueuedFrameSnapshot(BaseModel):
    frame_id: str
    request_id: str
    plan_id: str
    objective: str
    operation_ids: List[str] = Field(default_factory=list)
    depth: int
    checkpoint: Optional[Checkpoint] = None
    parent_id: Optional[str] = None
    worker_id: Optional[str] = None
    role: str = "root"
    tool_scope: List[str] = Field(default_factory=list)
    model_class: str = "small"
    branch_group_id: Optional[str] = None
    trace_context: Optional[OpenAITraceContext] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    agent_snapshot: QueuedAgentSnapshot


class BranchResumeSnapshot(BaseModel):
    branch_plan: BranchPlan
    execution_state: str = "branching"
    active_frame: Optional[QueuedFrameSnapshot] = None
    queued_frames: List[QueuedFrameSnapshot] = Field(default_factory=list)
    visible_tool_names: List[str] = Field(default_factory=list)
    artifacts: Dict[str, Any] = Field(default_factory=dict)
    open_handle_ids: List[str] = Field(default_factory=list)
    plan_node_status: Dict[str, str] = Field(default_factory=dict)
    branch_publications: List[BranchPublication] = Field(default_factory=list)
    side_effect_receipts: List["SideEffectReceipt"] = Field(default_factory=list)
    budget_totals: RuntimeBudgetTotalsSnapshot = Field(default_factory=RuntimeBudgetTotalsSnapshot)
    shell_state_snapshot: ShellStateSnapshot = Field(default_factory=ShellStateSnapshot)
    created_tools: int = 0
    promoted_nodes: int = 0
    checks_used: int = 0
