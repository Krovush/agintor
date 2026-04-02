from __future__ import annotations

import base64
import json
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, PrivateAttr, validator

from .utils import cheap_embedding, stable_hash


class GoalSpec(BaseModel):
    goal_id: str
    raw_prompt: str
    normalized_goal: str
    goal_keywords: List[str] = Field(default_factory=list)
    goal_phrases: List[str] = Field(default_factory=list)
    required_capabilities: List[str] = Field(default_factory=list)
    constraints: Dict[str, Any] = Field(default_factory=dict)
    success_criteria: List[str] = Field(default_factory=list)
    target_families: List[str] = Field(default_factory=list)
    deployment_preferences: Dict[str, Any] = Field(default_factory=dict)
    assumptions: List[str] = Field(default_factory=list)


class SuccessCriterion(BaseModel):
    criterion_id: str
    description: str
    required: bool
    priority: int
    measurable_signal: str
    verifier_hint: str
    target_family: str
    weight: float


class SuccessCriteriaBundle(BaseModel):
    bundle_id: str
    goal_id: str
    criteria: List[SuccessCriterion] = Field(default_factory=list)
    assumptions: List[str] = Field(default_factory=list)


class BenchmarkPlan(BaseModel):
    plan_id: str
    goal_id: str
    family_targets: List[str] = Field(default_factory=list)
    train_task_ids: List[str] = Field(default_factory=list)
    proxy_task_ids: List[str] = Field(default_factory=list)
    val_task_ids: List[str] = Field(default_factory=list)
    test_task_ids: List[str] = Field(default_factory=list)
    synthetic_task_ids: List[str] = Field(default_factory=list)
    verifier_bundle_id: str
    frozen: bool = True


class VerifierSpec(BaseModel):
    verifier_id: str
    verifier_type: str
    artifact_contract: Dict[str, Any] = Field(default_factory=dict)
    tolerance: float = 0.0
    uses_trace: bool = False
    local_only: bool = True
    expected_signal: str


class VerifierBundle(BaseModel):
    bundle_id: str
    plan_id: str
    verifiers: List[VerifierSpec] = Field(default_factory=list)
    checker_chain_defaults: List[str] = Field(default_factory=list)
    frozen: bool = True
    created_from: Dict[str, Any] = Field(default_factory=dict)


class DeploymentContract(BaseModel):
    entry_command: str
    runtime_abi: str
    python_version: str
    supported_backends: List[str] = Field(default_factory=list)
    required_env_names: List[str] = Field(default_factory=list)
    network_policy: str
    filesystem_policy: str
    notes: List[str] = Field(default_factory=list)


class FactoryProfile(BaseModel):
    agintor_provider: str
    evaluation: Dict[str, Any] = Field(default_factory=dict)
    evolution: Dict[str, Any] = Field(default_factory=dict)
    mutation: Dict[str, Any] = Field(default_factory=dict)
    benchmark_generation: Dict[str, Any] = Field(default_factory=dict)
    leader_selection: Dict[str, Any] = Field(default_factory=dict)
    runtime_backend: str


class RuntimePlan(BaseModel):
    plan_id: str
    goal_id: str
    runtime_abi: str
    seed_template: str
    mutable_files: List[str] = Field(default_factory=list)
    immutable_manifest: List[str] = Field(default_factory=list)
    runtime_profile: Dict[str, Any] = Field(default_factory=dict)
    provider_plan: Dict[str, Any] = Field(default_factory=dict)
    tooling_scope: List[str] = Field(default_factory=list)
    deployment_contract: DeploymentContract


class BuildSummary(BaseModel):
    build_id: str
    goal_id: str
    goal_prompt: str
    goal_task_ids: List[str] = Field(default_factory=list)
    goal_spec_path: str
    success_criteria_path: str
    benchmark_plan_path: str
    verifier_bundle_path: str
    runtime_plan_path: str
    workspace: str
    output_runtime_dir: str
    history_path: str = ""
    leader_runtime_hash: str = ""
    leader_runtime_dir: str = ""
    runtime_abi: str = ""
    selection_policy: str = ""
    best_train_score: float
    best_goal_score: float
    best_val_score: float
    accepted_mutations: int
    archive_cells: int
    agintor_provider: str
    runtime_provider: str
    export_bundle_file: str
    provenance_bundle_file: str
    export_summary_path: str = ""


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
    allowed_tool_categories: List[str] = Field(default_factory=list)
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
    proxy_scope_tags: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    class Config:
        allow_mutation = False


class SolveRequest(BaseModel):
    request_id: str
    prompt: str
    context_items: List[Dict[str, Any]] = Field(default_factory=list)
    file_paths: List[str] = Field(default_factory=list)
    output_schema: Dict[str, Any] = Field(default_factory=dict)
    allowed_tool_categories: List[str] = Field(default_factory=list)
    verification_preference: str = "verified_if_available"
    budget_overrides: Dict[str, Any] = Field(default_factory=dict)


class SolveResult(BaseModel):
    request_id: str
    runtime_hash: str
    artifact: Any
    status: str
    summary: str
    checks: List[Dict[str, Any]] = Field(default_factory=list)
    trace_ref: Optional[str] = None
    budget: Dict[str, Any] = Field(default_factory=dict)
    faults: Dict[str, Any] = Field(default_factory=dict)
    verified: bool = False
    best_effort: bool = False


class RunResult(BaseModel):
    task_id: str
    seed: int
    artifact: Any
    verifier_score: float
    cost: float
    latency: float
    faults: int
    trace: List[Dict[str, Any]] = Field(default_factory=list)
    trace_path: Optional[str] = None
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

    @staticmethod
    def _inline_trace_prefix() -> str:
        return "inline-json:"

    @classmethod
    def encode_trace_ref(cls, trace: List[Dict[str, Any]]) -> str:
        payload = json.dumps(trace, sort_keys=True, separators=(",", ":")).encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).decode("ascii")
        return cls._inline_trace_prefix() + encoded

    @classmethod
    def decode_trace_ref(cls, trace_ref: str) -> List[Dict[str, Any]]:
        if not trace_ref.startswith(cls._inline_trace_prefix()):
            return []
        encoded = trace_ref[len(cls._inline_trace_prefix()) :]
        try:
            payload = base64.urlsafe_b64decode(encoded.encode("ascii"))
            trace = json.loads(payload.decode("utf-8"))
        except Exception:
            return []
        return trace if isinstance(trace, list) else []

    def trace_rows(self) -> List[Dict[str, Any]]:
        if self.trace:
            return [dict(row) for row in self.trace]
        if not self.trace_path:
            return []
        inline_trace = self.decode_trace_ref(self.trace_path)
        if inline_trace:
            return inline_trace
        try:
            payload = json.loads(Path(self.trace_path).read_text(encoding="utf-8"))
        except Exception:
            return []
        return payload if isinstance(payload, list) else []

    def trace_ref(self) -> str:
        if self.trace_path:
            return self.trace_path
        return self.encode_trace_ref(self.trace)


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
