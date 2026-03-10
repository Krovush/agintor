from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, PrivateAttr, validator

from .utils import cheap_embedding, stable_hash


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


class AsyncHandle(BaseModel):
    handle_id: str
    tool_name: str
    sandbox_hash: str
    working_directory: str
    launch_time: float
    timeout: float
    stdout_path: str
    stderr_path: str
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

    @validator("embedding", pre=True, always=True)
    def default_embedding(cls, value: Any, values: Dict[str, Any]) -> List[float]:
        if value:
            return list(value)
        content = values.get("content", "")
        label = values.get("label", "")
        return cheap_embedding(f"{label} {content}")


class ArchiveEntry(BaseModel):
    code_hash: str
    runtime_hash: str
    scores: Dict[str, float]
    behavior_bin: List[str]
    scope_tag: str
    complexity_bucket: int
    mutable_loc: int
    trace_refs: List[str]


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


class RuntimeStateSnapshot(BaseModel):
    queue_length: int
    budget_state: Dict[str, Any]
    unresolved_count: int
    visible_tool_count: int
    open_handle_count: int
    confidence: float
    active_mode: Optional[str] = None


class ModelRequest(BaseModel):
    instructions: str
    prompt: str
    model_class: str
    seed: int
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ModelResponse(BaseModel):
    text: str
    raw: Dict[str, Any] = Field(default_factory=dict)
    model_name: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    token_estimate: int = 0
    latency_s: float = 0.0
    dollar_cost: float = 0.0


class ToolExecutionResult(BaseModel):
    tool_name: str
    output: Any
    stdout: str = ""
    stderr: str = ""
    latency_s: float = 0.0
    success: bool = True
    artifacts: List[str] = Field(default_factory=list)
    async_handle_id: Optional[str] = None
    verifier_ok: Optional[bool] = None


class OperationSpec(BaseModel):
    op_id: str
    kind: str
    output_key: str
    description: str
    tool_hint: Optional[str] = None
    expression: Optional[str] = None
    args: Dict[str, Any] = Field(default_factory=dict)
    requires_exact_symbol: Optional[str] = None
    dependencies: List[str] = Field(default_factory=list)
    externally_visible: bool = False


class BenchmarkTask(BaseModel):
    task_id: str
    family: Literal["top", "mem", "tool", "e2e"]
    prompt: str
    task_type: str
    symbolic_seeds: List[str] = Field(default_factory=list)
    file_paths: List[str] = Field(default_factory=list)
    context_items: List[Dict[str, Any]] = Field(default_factory=list)
    operations: List[OperationSpec] = Field(default_factory=list)
    expected: Any
    verifier_type: str = "json_exact"
    externally_visible: bool = True
    verification_required: bool = True
    allow_best_effort: bool = False
    transfer_scored: bool = False
    episode_id: Optional[str] = None
    episode_order: int = 0
    min_source_count: int = 0
    required_citation_count: int = 0
    live_web: bool = False
    frozen_corpus_id: Optional[str] = None
    proxy_scope_tags: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    class Config:
        allow_mutation = False


class RunResult(BaseModel):
    task_id: str
    seed: int
    artifact: Any
    verifier_score: float
    cost: float
    latency: float
    faults: int
    trace_path: str
    hard_invalid: bool = False
    invalid_reason: Optional[str] = None
    mode: Optional[str] = None
    created_tools: int = 0
    promoted_nodes: int = 0
    checks_used: int = 0
    model_calls: int = 0
    tokens_used: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    utility: Optional[float] = None


class TaskScore(BaseModel):
    s: float
    rho: float
    cvar: float
    utilities: List[float]
    verifier_scores: List[float]
    costs: List[float]
    latencies: List[float]
    faults: List[int]


class SuiteEvaluation(BaseModel):
    runtime_hash: str
    objective_scores: Dict[str, float]
    task_scores: Dict[str, TaskScore]
    family_scores: Dict[str, Dict[str, float]]
    run_results: List[RunResult]
    invalid: bool = False


class PredictorObservation(BaseModel):
    family: str
    feature_vector: List[float]
    label_probability: Optional[float] = None
    label_positive_scalar: Optional[float] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class MutationCandidate(BaseModel):
    runtime_dir: str
    patch_text: str
    touched_scope: List[str]
    prompt: str
    objective: str


class EvaluationStageResult(BaseModel):
    stage: int
    passed: bool
    reason: str
    metrics: Dict[str, Any] = Field(default_factory=dict)
    suite_evaluation: Optional[SuiteEvaluation] = None


class ObjectiveKind(str, Enum):
    SINGLE_TASK = "single_task"
    FAMILY = "family"
    FAMILY_ROBUST = "family_robust"
    GLOBAL = "global"
    GLOBAL_ROBUST = "global_robust"


class ObjectiveSpec(BaseModel):
    name: str
    kind: ObjectiveKind
    task_id: Optional[str] = None
    family: Optional[str] = None


class RuntimeManifest(BaseModel):
    runtime_id: str
    version: str
    policy_modules: Dict[str, str]
    mutable_files: List[str]
    immutable_manifest: List[str]
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ArchiveRecord(BaseModel):
    objective: str
    key: str
    entry: ArchiveEntry
    runtime_dir: str


class EvolutionHistoryRow(BaseModel):
    step: int
    objective: str
    parent_runtime_hash: str
    child_runtime_hash: Optional[str] = None
    scope: List[str]
    stage_results: List[EvaluationStageResult]
    accepted: bool = False
    inserted_keys: List[str] = Field(default_factory=list)


class RuntimeDescriptor(BaseModel):
    code_hash: str
    runtime_hash: str
    behavior_bin: List[str]
    interface_diff_mask: str = "0000"
    scope_tag: str
    complexity_bucket: int
    mutable_loc: int
    mutable_ast_nodes: int = 0

    @classmethod
    def from_runtime_hash(
        cls,
        runtime_hash: str,
        behavior_bin: List[str],
        scope_tag: str,
        complexity_bucket: int,
        mutable_loc: int,
        mutable_ast_nodes: int = 0,
        interface_diff_mask: str = "0000",
    ) -> "RuntimeDescriptor":
        return cls(
            code_hash=stable_hash(runtime_hash, behavior_bin, scope_tag, complexity_bucket, mutable_loc),
            runtime_hash=runtime_hash,
            behavior_bin=behavior_bin,
            interface_diff_mask=interface_diff_mask,
            scope_tag=scope_tag,
            complexity_bucket=complexity_bucket,
            mutable_loc=mutable_loc,
            mutable_ast_nodes=mutable_ast_nodes,
        )
