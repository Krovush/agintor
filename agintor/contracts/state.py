from __future__ import annotations

import base64
import json
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, NamedTuple, Optional, Sequence
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from ..utils import cheap_embedding, now_ts, stable_hash

class AgentTemplate(BaseModel):
    agent_id: str
    description: str
    capability_set: List[str]
    symbol_set: List[str]
    default_tool_scope: List[str]
    success_stats: Dict[str, float]
    staleness_clock: int
    model_policy_tag: str
    _canonical: bool = PrivateAttr(default=False)
    _clone: bool = PrivateAttr(default=False)


class ChildSpec(BaseModel):
    child_id: str
    role: str
    instruction: str
    tool_scope: List[str]
    model_class: str
    required_capabilities: List[str]
    required_permissions: List[str]
    dependency_ids: List[str]
    comm_mode: str
    resume_policy: str
    init_summary: Dict[str, Any]


class ToolSpec(BaseModel):
    name: str
    category_path: List[str]
    signature: str
    description: str
    runtime: str
    deps: List[str]
    permissions: List[str]
    tests: List[Dict[str, Any]]
    backgroundable: bool
    state_schema: Dict[str, Any]
    source_digest: str
    build_cmd: str
    run_cmd: str
    timeout_s: int
    determinism_class: str


class SummaryRecord(BaseModel):
    objective: str
    evidence: List[str]
    artifacts: List[str]
    unresolved: List[str]
    open_handles: List[str]
    next_actions: List[str]
    symbols: List[str]
    verifier_state: Dict[str, Any]
    provenance: Dict[str, Any]


class Checkpoint(BaseModel):
    summary: SummaryRecord
    artifact_refs: List[str]
    open_handles: List[str]
    unresolved_goals: List[str]
    budget_state: Dict[str, Any]
    verifier_state: Dict[str, Any]
    resume_constraints: Dict[str, Any]

    @field_validator("summary", mode="before")
    @classmethod
    def normalize_summary_record(cls, value: Any) -> Any:
        if hasattr(value, "model_dump"):
            return value.model_dump()
        return value


class AsyncHandle(BaseModel):
    handle_id: str
    tool_name: str
    sandbox_hash: str
    working_directory: str
    launch_time: float
    timeout: float
    stdout_path: Optional[str] = None
    stderr_path: Optional[str] = None
    state: str
    artifact_refs: List[str]
    process_pid: Optional[int] = None


class MemoryNode(BaseModel):
    node_id: str
    type: str
    label: str
    content: str
    embedding: List[float]
    symbol_set: List[str]
    file_paths: List[str]
    source_task_id: str
    verifier_support: float
    timestamps: Dict[str, float]
    provenance: Dict[str, Any]
    tombstoned: bool = False

    @model_validator(mode="before")
    @classmethod
    def default_embedding(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        if values.get("embedding"):
            values["embedding"] = list(values["embedding"])
            return values
        content = values.get("content", "")
        label = values.get("label", "")
        values["embedding"] = cheap_embedding(f"{label} {content}")
        return values


class NodeType(str, Enum):
    AGENT_RUN = "AgentRun"
    EVENT = "Event"
    SUMMARY = "Summary"
    ARTIFACT = "Artifact"
    RAW_BLOB = "RawBlob"
    OPEN_HANDLE = "OpenHandle"
    VERIFIER_EVIDENCE = "VerifierEvidence"


class EdgeType(str, Enum):
    CALLS_AGENT = "CALLS_AGENT"
    EMITS = "EMITS"
    SUMMARIZES = "SUMMARIZES"
    PRODUCES = "PRODUCES"
    BACKLINKS_TO = "BACKLINKS_TO"
    WAITS_ON = "WAITS_ON"
    CONTINUES_FROM = "CONTINUES_FROM"
    VALIDATED_BY = "VALIDATED_BY"


class LongTermNodeType(str, Enum):
    SYMBOL = "Symbol"
    FILE = "File"
    QUERY = "Query"
    ANSWER = "Answer"
    TOOL_FAILURE = "ToolFailure"
    FIX_PATTERN = "FixPattern"
    TASK_NOTE = "TaskNote"
    PROCEDURE = "Procedure"
    ENVIRONMENT_FINGERPRINT = "EnvironmentFingerprint"
    ARTIFACT_SIGNATURE = "ArtifactSignature"


class StrictPersistenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class VerifiedFactRef(StrictPersistenceModel):
    fact_id: str
    content: str
    supporting_receipt_ids: List[str] = Field(default_factory=list)
    supporting_verifier_ids: List[str] = Field(default_factory=list)


class WorkingMemorySnapshot(StrictPersistenceModel):
    current_objective: Optional[str] = None
    accepted_constraints: List[str] = Field(default_factory=list)
    active_plan_summary: Optional[str] = None
    verified_facts: List[VerifiedFactRef] = Field(default_factory=list)
    unresolved_critical_items: List[str] = Field(default_factory=list)
    active_branch_refs: List[str] = Field(default_factory=list)
    selected_checkpoint_refs: List[str] = Field(default_factory=list)
    active_recovery_warnings: List[str] = Field(default_factory=list)
    captured_at: float = Field(default_factory=now_ts)


class TraceCursorSnapshot(StrictPersistenceModel):
    runtime_trace_length: int = 0
    latest_runtime_event: Optional[str] = None
    latest_runtime_event_sequence_no: int = 0
    last_session_id: Optional[str] = None
    last_build_id: Optional[str] = None
    last_solve_request_id: Optional[str] = None
    last_runtime_task_key: Optional[str] = None
    linked_call_ids: List[str] = Field(default_factory=list)
    materialization_state_ref: Optional[str] = None
    captured_at: float = Field(default_factory=now_ts)


class FingerprintDelta(StrictPersistenceModel):
    field: str
    previous: Any = None
    current: Any = None


class EnvironmentFingerprint(StrictPersistenceModel):
    fingerprint_id: str = ""
    runtime_backend: str
    runtime_hash: str
    runtime_contract_version: str
    runtime_isolation_policy: str
    supported_guarantees: List[str] = Field(default_factory=list)
    provider_identity: List[str] = Field(default_factory=list)
    model_class: Optional[str] = None
    sandbox_hash: Optional[str] = None
    tool_runtime_ids: List[str] = Field(default_factory=list)
    dependency_digest: Optional[str] = None
    filesystem_policy: Optional[str] = None
    network_policy: Optional[str] = None
    captured_at: float = Field(default_factory=now_ts)
    source_attempt_id: Optional[str] = None
    source_checkpoint_ref: Optional[str] = None

    @staticmethod
    def content_field_names() -> List[str]:
        return [
            "runtime_backend",
            "runtime_hash",
            "runtime_contract_version",
            "runtime_isolation_policy",
            "supported_guarantees",
            "provider_identity",
            "model_class",
            "sandbox_hash",
            "tool_runtime_ids",
            "dependency_digest",
            "filesystem_policy",
            "network_policy",
        ]

    @classmethod
    def content_payload(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        for field_name in cls.content_field_names():
            value = values.get(field_name)
            if field_name in {"supported_guarantees", "provider_identity", "tool_runtime_ids"}:
                payload[field_name] = sorted(str(item) for item in (value or []) if str(item).strip())
            else:
                payload[field_name] = value
        return payload

    @model_validator(mode="after")
    def validate_fingerprint_id(self) -> "EnvironmentFingerprint":
        values = self.model_dump()
        expected = "environment-fingerprint." + stable_hash(
            "environment-fingerprint",
            self.content_payload(values),
        )[:24]
        supplied = str(self.fingerprint_id or "").strip()
        if supplied and supplied != expected:
            raise ValueError("fingerprint_id must be the stable hash of EnvironmentFingerprint content fields")
        self.fingerprint_id = expected
        return self


class RecoveryAttempt(StrictPersistenceModel):
    recovery_attempt_id: str
    run_id: str
    attempt_id: str
    selected_checkpoint_ref: str
    source_checkpoint_ref: Optional[str] = None
    origin_request_id: Optional[str] = None
    rebound_request_id: Optional[str] = None
    reconciliation_policy: Literal["strict", "best_effort"]
    compatibility_result: Literal["exact_compatible", "degraded_compatible", "fail_closed"]
    source_fingerprint_id: Optional[str] = None
    current_fingerprint_id: str
    fingerprint_deltas: List[FingerprintDelta] = Field(default_factory=list)
    receipts_reused: List[str] = Field(default_factory=list)
    receipts_reissued: List[str] = Field(default_factory=list)
    receipts_blocked: List[str] = Field(default_factory=list)
    receipts_invalidated: List[str] = Field(default_factory=list)
    blocked_node_ids: List[str] = Field(default_factory=list)
    degraded_plan_node_ids: List[str] = Field(default_factory=list)
    resume_explanation: str
    attempted_at: float = Field(default_factory=now_ts)
    completed_at: Optional[float] = None


class LongTermWriteRecord(StrictPersistenceModel):
    write_id: str
    target_node_id: str
    action: Literal["upsert", "merge", "refine", "tombstone", "conflict"]
    payload_ref: str
    source_task_id: Optional[str] = None
    source_attempt_id: str = ""
    source_checkpoint_ref: Optional[str] = None
    verifier_support_refs: List[str] = Field(default_factory=list)
    prior_write_id: Optional[str] = None
    contradiction_target_write_id: Optional[str] = None
    written_at: float = Field(default_factory=now_ts)


class LongTermEdgeType(str, Enum):
    DERIVED_FROM = "DERIVED_FROM"
    REFINES = "REFINES"
    CONTRADICTS = "CONTRADICTS"
    SUPPORTED_BY = "SUPPORTED_BY"


class LongTermEdgeRecord(StrictPersistenceModel):
    edge_id: str
    source_node_id: str
    target_node_id: str
    edge_type: str
    introducing_write_id: str
    tombstoned: bool = False
    tombstone_write_id: Optional[str] = None
    written_at: float = Field(default_factory=now_ts)

    @field_validator("edge_type")
    @classmethod
    def validate_long_term_edge_type(cls, value: str) -> str:
        allowed = {item.value for item in LongTermEdgeType}
        if value not in allowed:
            raise ValueError(f"unsupported long-term edge type {value}")
        return value


class RetrievalSignalRow(StrictPersistenceModel):
    node_id: str
    rank: int
    exact_file_path_hit: bool = False
    exact_symbol_hit: bool = False
    node_id_match: bool = False
    verifier_support_score: float = 0.0
    lexical_overlap_score: float = 0.0
    embedding_similarity_score: float = 0.0
    same_task_affinity_score: float = 0.0
    synthesized_neighbor_expansion: bool = False


class RetrievalDiagnosticRecord(StrictPersistenceModel):
    diagnostic_id: str
    query_hash: str
    task_id: Optional[str] = None
    seed: Optional[int] = None
    request_id: Optional[str] = None
    scope_id: Optional[str] = None
    returned_node_ids: List[str] = Field(default_factory=list)
    signals: List[RetrievalSignalRow] = Field(default_factory=list)
    exact_first_preserved: bool = False
    retrieved_at: float = Field(default_factory=now_ts)


class RuntimeBudgetTotalsSnapshot(BaseModel):
    normalized: Dict[str, float] = Field(default_factory=dict)
    cost: float = 0.0
    latency: float = 0.0
    calls: int = 0
    checks: int = 0
    tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


class VerifierStateSnapshot(BaseModel):
    checker_ladder: List[str] = Field(default_factory=list)
    required: bool = False
    exact_verifier_required: bool = False
    verifier_type: str = "none"
    terminal_nodes: List[str] = Field(default_factory=list)
    last_verifier_score: float = 0.0


class MessageBoardSnapshot(BaseModel):
    entries: List[Dict[str, Any]] = Field(default_factory=list)
    cursors: Dict[str, int] = Field(default_factory=dict)


class ShortTermGraphSnapshot(BaseModel):
    nodes: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    edges: List[Dict[str, Any]] = Field(default_factory=list)
    hidden_nodes: List[str] = Field(default_factory=list)
    summary_backlinks: List[Dict[str, Any]] = Field(default_factory=list)
    artifact_lineage: List[Dict[str, Any]] = Field(default_factory=list)
    branch_publication_lineage: List[Dict[str, Any]] = Field(default_factory=list)
    open_handle_lineage: List[Dict[str, Any]] = Field(default_factory=list)
    verifier_evidence_refs: List[str] = Field(default_factory=list)
    receipt_refs: List[str] = Field(default_factory=list)
    event_refs: List[str] = Field(default_factory=list)


class LongTermGraphSnapshot(BaseModel):
    nodes: List[MemoryNode] = Field(default_factory=list)
    edges: List[LongTermEdgeRecord] = Field(default_factory=list)
    write_records: List[LongTermWriteRecord] = Field(default_factory=list)
    retrieval_diagnostics: List[RetrievalDiagnosticRecord] = Field(default_factory=list)
    write_log_refs: List[str] = Field(default_factory=list)
    diagnostic_refs: List[str] = Field(default_factory=list)


class OpenHandleTableSnapshot(StrictPersistenceModel):
    handles: List[AsyncHandle] = Field(default_factory=list)


class TaskLocalToolSnapshot(BaseModel):
    spec: ToolSpec
    source: str
    historical_passes: int = 0
    historical_runs: int = 0
    distinct_tasks: List[str] = Field(default_factory=list)
    sandbox_hash: Optional[str] = None
    safety_validated: bool = False

    @field_validator("spec", mode="before")
    @classmethod
    def normalize_tool_spec(cls, value: Any) -> Any:
        if hasattr(value, "model_dump"):
            return value.model_dump()
        return value


class TaskLocalToolRegistrySnapshot(BaseModel):
    tools: List[TaskLocalToolSnapshot] = Field(default_factory=list)
    category_summaries: Dict[str, str] = Field(default_factory=dict)


class PredictorLogisticRegressorSnapshot(StrictPersistenceModel):
    weights: List[float] = Field(default_factory=list)
    x_points: List[float] = Field(default_factory=list)
    y_points: List[float] = Field(default_factory=list)
    p_min: float = 0.02
    p_max: float = 0.98


class PredictorLogLinearHuberSnapshot(StrictPersistenceModel):
    weights: List[float] = Field(default_factory=list)


class PredictorEnsembleSnapshot(StrictPersistenceModel):
    probability_models: List[PredictorLogisticRegressorSnapshot] = Field(default_factory=list)
    positive_models: List[PredictorLogLinearHuberSnapshot] = Field(default_factory=list)


class PredictorRankingMixerSnapshot(StrictPersistenceModel):
    alpha: List[float] = Field(default_factory=list)


class PredictorSnapshot(StrictPersistenceModel):
    ensemble_size: int = 5
    max_observations_per_family: int = 200
    frozen: bool = False
    observations: Dict[str, List[Dict[str, Any]]] = Field(default_factory=dict)
    models: Dict[str, PredictorEnsembleSnapshot] = Field(default_factory=dict)
    ranking_weights: Dict[str, PredictorRankingMixerSnapshot] = Field(default_factory=dict)


class ShellStateSnapshot(BaseModel):
    short_term_graph: ShortTermGraphSnapshot = Field(default_factory=ShortTermGraphSnapshot)
    long_term_graph: LongTermGraphSnapshot = Field(default_factory=LongTermGraphSnapshot)
    message_board: MessageBoardSnapshot = Field(default_factory=MessageBoardSnapshot)
    open_handles: OpenHandleTableSnapshot = Field(default_factory=OpenHandleTableSnapshot)
    task_local_tool_registry: TaskLocalToolRegistrySnapshot = Field(default_factory=TaskLocalToolRegistrySnapshot)
    predictor_snapshot: PredictorSnapshot = Field(default_factory=PredictorSnapshot)
    current_task_id: str = ""
    current_episode_id: Optional[str] = None
    memory_scope_kind: str = ""
    memory_scope_id: str = ""

    @field_validator("open_handles", mode="before")
    @classmethod
    def normalize_open_handles(cls, value: Any) -> Any:
        if isinstance(value, list):
            return {"handles": value}
        return value


class AttemptSnapshot(BaseModel):
    run_id: str = ""
    run_root: str = ""
    attempt_id: str = ""
    resumed_from_checkpoint_ref: str = ""
    published_boundary: str = ""
    published_at: float = 0.0


class RuntimeStateSnapshot(BaseModel):
    request_id: str = ""
    plan_id: str = ""
    execution_state: str = "idle"
    active_branch_count: int = 0
    checkpoint_sequence_no: int = 0
    event_sequence_no: int = 0
    active_frame: Optional["QueuedFrameSnapshot"] = None
    queued_frames: List["QueuedFrameSnapshot"] = Field(default_factory=list)
    visible_tool_names: List[str] = Field(default_factory=list)
    unresolved_goals: List[str] = Field(default_factory=list)
    confidence: float = 0.0
    mode: Optional[str] = None
    created_tools: int = 0
    promoted_nodes: int = 0
    checks_used: int = 0
    interface_usage: Dict[str, float] = Field(
        default_factory=lambda: {"top": 0.0, "mem": 0.0, "tool": 0.0, "ctl": 0.0}
    )
    artifacts: Dict[str, Any] = Field(default_factory=dict)
    checkpoints: Dict[str, Checkpoint] = Field(default_factory=dict)
    worker_plans: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    open_handle_ids: List[str] = Field(default_factory=list)
    plan_node_status: Dict[str, str] = Field(default_factory=dict)
    branch_states: Dict[str, "BranchState"] = Field(default_factory=dict)
    branch_publications: List["BranchPublication"] = Field(default_factory=list)
    branch_resume_snapshots: Dict[str, "BranchResumeSnapshot"] = Field(default_factory=dict)
    latest_checkpoint_ref: Optional[str] = None
    subgoal_negative_steps: Dict[str, int] = Field(default_factory=dict)
    subgoal_last_model: Dict[str, str] = Field(default_factory=dict)
    last_unresolved_goal: Optional[str] = None
    budget_totals: RuntimeBudgetTotalsSnapshot = Field(default_factory=RuntimeBudgetTotalsSnapshot)
    verifier_state: VerifierStateSnapshot = Field(default_factory=VerifierStateSnapshot)
