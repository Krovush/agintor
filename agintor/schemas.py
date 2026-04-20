from __future__ import annotations

import base64
import json
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, NamedTuple, Optional, Sequence
from urllib.parse import urlparse

from pydantic import BaseModel, Field, PrivateAttr, root_validator, validator

from .utils import cheap_embedding, now_ts, stable_hash


class ArtifactMetadata(BaseModel):
    artifact_id: str
    schema_version: str
    content_digest: str
    creation_stage: str


class OpenAITraceContext(BaseModel):
    session_id: Optional[str] = None
    provider_role: Optional[str] = None
    build_id: Optional[str] = None
    runtime_hash: Optional[str] = None
    runtime_dir: Optional[str] = None
    task_id: Optional[str] = None
    seed: Optional[int] = None
    request_id: Optional[str] = None
    iteration: Optional[int] = None
    objective: Optional[str] = None
    touched_scope: List[str] = Field(default_factory=list)
    agent_id: Optional[str] = None
    frame_role: Optional[str] = None
    worker_id: Optional[str] = None
    op_id: Optional[str] = None
    run_node_id: Optional[str] = None


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
            row["trace_context"] = self.trace_context.dict(exclude_none=True)
        if self.frame_id:
            row["frame_id"] = self.frame_id
        if self.branch_id:
            row["branch_id"] = self.branch_id
        if self.node_id:
            row["node_id"] = self.node_id
        row.update(self.payload)
        return row


class GoalAssumption(BaseModel):
    assumption_id: str
    statement: str
    category: str = "default"
    source: str = "goal_normalization"
    hard_constraint: bool = False


class AssumptionRegister(BaseModel):
    register_id: str
    goal_id: str
    assumptions: List[GoalAssumption] = Field(default_factory=list)
    artifact_metadata: Optional[ArtifactMetadata] = None


class PlanningIssue(BaseModel):
    issue_id: str
    severity: str
    message: str
    repair_action: Optional[str] = None
    artifact_refs: List[str] = Field(default_factory=list)


class PlanningDiagnostics(BaseModel):
    diagnostics_id: str
    goal_id: str
    issues: List[PlanningIssue] = Field(default_factory=list)
    repaired: bool = False
    blocked: bool = False
    artifact_metadata: Optional[ArtifactMetadata] = None


class ReplanContract(BaseModel):
    contract_id: str
    goal_id: str
    repairable_artifacts: List[str] = Field(default_factory=list)
    blocked_stages: List[str] = Field(default_factory=list)
    raw_goal_reparse_allowed: bool = False
    status: str = "stable"
    artifact_metadata: Optional[ArtifactMetadata] = None


class ProviderRole(BaseModel):
    name: str
    api_key_env: Optional[str] = None
    api_key_file_env: Optional[str] = None
    model_map: Dict[str, str] = Field(default_factory=dict)


class ProviderPlan(BaseModel):
    plan_id: str
    agintor_provider: ProviderRole
    runtime_provider: ProviderRole
    runtime_backend: str
    artifact_metadata: Optional[ArtifactMetadata] = None


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
    artifact_metadata: Optional[ArtifactMetadata] = None


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
    artifact_metadata: Optional[ArtifactMetadata] = None


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
    artifact_metadata: Optional[ArtifactMetadata] = None


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
    artifact_metadata: Optional[ArtifactMetadata] = None


class RuntimeIsolationPolicy(BaseModel):
    timeout_envelope: Dict[str, Any] = Field(default_factory=dict)
    workspace_root: str = ""
    environment_allowlist: List[str] = Field(default_factory=list)
    network_policy: str = "none"
    filesystem_policy: str = "workspace-read-write"
    required_guarantees: List[str] = Field(default_factory=list)
    desired_guarantees: List[str] = Field(default_factory=list)


class DeploymentContract(BaseModel):
    entry_command: str
    runtime_abi: str
    kernel_version: str = ""
    storage_schema_version: str = ""
    python_version: str
    supported_backends: List[str] = Field(default_factory=list)
    required_env_names: List[str] = Field(default_factory=list)
    required_env_any_of: List[List[str]] = Field(default_factory=list)
    environment_allowlist: List[str] = Field(default_factory=list)
    network_policy: str
    filesystem_policy: str
    dependency_digest_set: List[str] = Field(default_factory=list)
    container_image_digest: Optional[str] = None
    capability_flags: List[str] = Field(default_factory=list)
    runtime_isolation_policy: Optional[RuntimeIsolationPolicy] = None
    notes: List[str] = Field(default_factory=list)
    artifact_metadata: Optional[ArtifactMetadata] = None


class FactoryProfile(BaseModel):
    agintor_provider: str
    evaluation: Dict[str, Any] = Field(default_factory=dict)
    evolution: Dict[str, Any] = Field(default_factory=dict)
    mutation: Dict[str, Any] = Field(default_factory=dict)
    benchmark_generation: Dict[str, Any] = Field(default_factory=dict)
    leader_selection: Dict[str, Any] = Field(default_factory=dict)
    runtime_backend: str
    artifact_metadata: Optional[ArtifactMetadata] = None


class RuntimePlan(BaseModel):
    plan_id: str
    goal_id: str
    runtime_abi: str
    kernel_version: str = ""
    storage_schema_version: str = ""
    seed_template: str
    mutable_files: List[str] = Field(default_factory=list)
    immutable_manifest: List[str] = Field(default_factory=list)
    runtime_profile: Dict[str, Any] = Field(default_factory=dict)
    provider_plan: ProviderPlan
    tooling_scope: List[str] = Field(default_factory=list)
    deployment_contract: DeploymentContract
    artifact_metadata: Optional[ArtifactMetadata] = None


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
    factory_profile_path: str = ""
    deployment_contract_path: str = ""
    planning_diagnostics_path: str = ""
    replan_contract_path: str = ""
    workspace: str
    output_runtime_dir: str
    history_path: str = ""
    archive_index_path: str = ""
    validation_history_path: str = ""
    stage_failures_path: str = ""
    leaderboard_path: str = ""
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
    export_validation_path: str = ""
    artifact_metadata: Optional[ArtifactMetadata] = None


class ExportSummary(BaseModel):
    export_id: str
    build_id: str
    goal_id: str
    goal_prompt: str
    runtime_hash: str
    code_hash: str
    runtime_id: str
    runtime_abi: str
    kernel_version: str
    storage_schema_version: str
    source_runtime_dir: str
    source_runtime_hash: str
    runtime_profile_path: str
    deployment_contract_path: str
    export_bundle_path: str
    provenance_bundle_path: str
    leaderboard_path: str = ""
    runtime_plan_path: str = ""
    artifact_metadata: Optional[ArtifactMetadata] = None


class ExportValidationCheck(BaseModel):
    check_id: str
    check_type: str
    status: str
    summary: str
    request_mode: str = ""
    provider_name: str = ""
    verification_status: str = ""
    observed_artifact: Any = None
    observed_model_calls: int = 0


class ExportValidationReceipt(BaseModel):
    validation_id: str
    build_id: str
    goal_id: str
    runtime_hash: str
    runtime_id: str
    runtime_abi: str
    certified_properties: List[str] = Field(default_factory=list)
    uncertified_properties: List[str] = Field(default_factory=list)
    checks: List[ExportValidationCheck] = Field(default_factory=list)
    artifact_metadata: Optional[ArtifactMetadata] = None


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

    @validator("embedding", pre=True, always=True, allow_reuse=True)
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


class LongTermGraphSnapshot(BaseModel):
    nodes: List[MemoryNode] = Field(default_factory=list)


class TaskLocalToolSnapshot(BaseModel):
    spec: ToolSpec
    source: str
    historical_passes: int = 0
    historical_runs: int = 0
    distinct_tasks: List[str] = Field(default_factory=list)
    sandbox_hash: Optional[str] = None
    safety_validated: bool = False


class TaskLocalToolRegistrySnapshot(BaseModel):
    tools: List[TaskLocalToolSnapshot] = Field(default_factory=list)
    category_summaries: Dict[str, str] = Field(default_factory=dict)


class ShellStateSnapshot(BaseModel):
    short_term_graph: ShortTermGraphSnapshot = Field(default_factory=ShortTermGraphSnapshot)
    long_term_graph: LongTermGraphSnapshot = Field(default_factory=LongTermGraphSnapshot)
    message_board: MessageBoardSnapshot = Field(default_factory=MessageBoardSnapshot)
    open_handles: List[AsyncHandle] = Field(default_factory=list)
    task_local_tool_registry: TaskLocalToolRegistrySnapshot = Field(default_factory=TaskLocalToolRegistrySnapshot)
    current_task_id: str = ""
    current_episode_id: Optional[str] = None
    memory_scope_kind: str = ""
    memory_scope_id: str = ""


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


class InputBinding(BaseModel):
    target_arg: str
    source_kind: Literal["request_context", "request_file", "upstream_output", "plan_constant"]
    source_ref: str
    required: bool = True


class RequestFileRef(BaseModel):
    file_ref_id: str
    source_path: str
    runtime_path: str
    path_root: Literal["host_absolute", "runtime_workspace_relative"]
    host_path: Optional[str] = None
    workspace_relative_path: Optional[str] = None

    @root_validator(pre=False, allow_reuse=True)
    def validate_request_file_ref(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        path_root = str(values.get("path_root") or "").strip()
        host_path = str(values.get("host_path") or "").strip()
        workspace_relative_path = str(values.get("workspace_relative_path") or "").strip()
        runtime_path = str(values.get("runtime_path") or "").strip()
        if not runtime_path:
            raise ValueError("request file refs require runtime_path")
        if path_root == "host_absolute":
            if not host_path:
                raise ValueError("host_absolute request file refs require host_path")
            if workspace_relative_path:
                raise ValueError("host_absolute request file refs may not set workspace_relative_path")
        elif path_root == "runtime_workspace_relative":
            if not workspace_relative_path:
                raise ValueError("runtime_workspace_relative request file refs require workspace_relative_path")
            if host_path:
                raise ValueError("runtime_workspace_relative request file refs may not set host_path")
        else:
            raise ValueError(f"unsupported request file ref path_root {path_root!r}")
        return values


class PlanOrigin(BaseModel):
    origin_kind: Literal["benchmark", "user_request"]
    source_task_id: Optional[str] = None
    source_request_id: Optional[str] = None
    source_suite: Optional[str] = None
    adapter_kind: str
    adaptation_assumptions: List[str] = Field(default_factory=list)


PlanNodeKind = Literal[
    "builtin_op",
    "memory_lookup",
    "tool_call",
    "tool_synthesis",
    "direct_response",
    "repo_patch",
    "service_action",
    "merge",
    "verify",
]


class PlanNodeDescriptor(BaseModel):
    executor_name: str
    value_producing: bool = True
    branchable: bool = False
    requires_default_provider: bool = False
    prompt_local_only_allowed: bool = False
    provider_backed_metadata_key: Optional[str] = None
    validation_tags: List[str] = Field(default_factory=list)


class PlanNode(BaseModel):
    node_id: str
    op_id: str = ""
    node_kind: PlanNodeKind
    instruction: str
    kind: str = ""
    description: str = ""
    output_key: str
    args: Dict[str, Any] = Field(default_factory=dict)
    expression: Optional[str] = None
    dependencies: List[str] = Field(default_factory=list)
    tool_hint: Optional[str] = None
    allowed_tool_categories: List[str] = Field(default_factory=list)
    static_args: Dict[str, Any] = Field(default_factory=dict)
    input_bindings: List[InputBinding] = Field(default_factory=list)
    verification_required: bool = False
    externally_visible: bool = False
    frame_role: str = "worker"
    branch_group_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ExecutionPlanRequirements(BaseModel):
    request_mode: Literal["benchmark", "user_request"] = "benchmark"
    requires_default_provider: bool = False
    default_provider_nodes: List[str] = Field(default_factory=list)
    requires_network_access: bool = False
    network_nodes: List[str] = Field(default_factory=list)
    required_network_transports: List[str] = Field(default_factory=list)
    network_transport_nodes: Dict[str, List[str]] = Field(default_factory=dict)
    requires_filesystem_write: bool = False
    filesystem_write_nodes: List[str] = Field(default_factory=list)
    required_tool_categories: List[str] = Field(default_factory=list)


_SERVICE_ACTION_TRANSPORT_SCHEMES: Dict[str, tuple[str, ...]] = {
    "http": ("http", "https"),
}

_SERVICE_ACTION_CATEGORY_TO_TRANSPORT: Dict[str, str] = {
    "service/http": "http",
}


def normalize_capability_scope(scope: Any) -> str:
    return str(scope or "").strip().strip("/").lower()


def normalize_capability_scopes(scopes: Sequence[Any]) -> List[str]:
    normalized: List[str] = []
    seen: set[str] = set()
    for scope in scopes:
        key = normalize_capability_scope(scope)
        if not key or key in seen:
            continue
        seen.add(key)
        normalized.append(key)
    return normalized


def expand_capability_scopes(scopes: Sequence[Any]) -> List[str]:
    expanded: List[str] = []
    for scope in normalize_capability_scopes(scopes):
        if scope == "service/*":
            expanded.extend(sorted(_SERVICE_ACTION_CATEGORY_TO_TRANSPORT))
            continue
        expanded.append(scope)
    return normalize_capability_scopes(expanded)


def capability_scope_allows(granted_scopes: Sequence[Any], required_scope: Any) -> bool:
    normalized_required = normalize_capability_scope(required_scope)
    if not normalized_required:
        return True
    normalized_grants = normalize_capability_scopes(granted_scopes)
    if not normalized_grants:
        return True
    for granted_scope in normalized_grants:
        if granted_scope == normalized_required:
            return True
        if granted_scope.endswith("/*") and normalized_required.startswith(granted_scope[:-1]):
            return True
    return False


def capability_scope_requires_network_access(scope: Any) -> bool:
    return normalize_capability_scope(scope).startswith("service/")


def capability_scope_requires_filesystem_write(scope: Any) -> bool:
    return capability_scope_allows([scope], "filesystem/write") or capability_scope_allows([scope], "filesystem/patch")


def capability_scope_service_categories(scopes: Sequence[Any]) -> List[str]:
    return [scope for scope in expand_capability_scopes(scopes) if scope.startswith("service/")]


def normalize_service_transport(transport: Any) -> str:
    normalized = normalize_capability_scope(transport)
    if not normalized:
        return ""
    if normalized in _SERVICE_ACTION_TRANSPORT_SCHEMES:
        return normalized
    return _SERVICE_ACTION_CATEGORY_TO_TRANSPORT.get(normalized, "")


def normalize_service_transports(transports: Sequence[Any]) -> List[str]:
    normalized: List[str] = []
    seen: set[str] = set()
    for transport in transports:
        key = normalize_service_transport(transport)
        if not key or key in seen:
            continue
        seen.add(key)
        normalized.append(key)
    return normalized


def capability_scope_service_transports(scopes: Sequence[Any]) -> List[str]:
    return normalize_service_transports(capability_scope_service_categories(scopes))


def _service_transport_candidates(value: Any) -> List[str]:
    normalized = normalize_capability_scope(value)
    if not normalized:
        return []
    if normalized == "service/*":
        return normalize_service_transports(_SERVICE_ACTION_TRANSPORT_SCHEMES.keys())
    transport = normalize_service_transport(normalized)
    if transport:
        return [transport]
    raise ValueError(f"service_action declares unsupported transport hint {value!r}")


class ServiceActionTransportCompatibility(NamedTuple):
    transport: str
    allowed_schemes: tuple[str, ...]
    url_scheme: str


def service_action_transport_compatibility(
    *,
    url: str,
    service_transport: Any = None,
    category_hint: Any = None,
    allowed_tool_categories: Sequence[str] | None = None,
) -> ServiceActionTransportCompatibility:
    normalized_url = str(url or "").strip()
    if not normalized_url:
        raise ValueError("service_action url may not be empty")
    normalized_allowed_categories = normalize_capability_scopes(allowed_tool_categories or [])
    allowed_transports = set(capability_scope_service_transports(normalized_allowed_categories))
    if normalized_allowed_categories and not allowed_transports:
        raise ValueError(
            "service_action must declare a supported service transport via service_transport or service/* allowed_tool_categories"
        )

    explicit_transports: set[str] = set()
    for candidate in (service_transport, category_hint):
        explicit_transports.update(_service_transport_candidates(candidate))

    candidate_transports = explicit_transports or allowed_transports or set(_SERVICE_ACTION_TRANSPORT_SCHEMES)
    if allowed_transports:
        candidate_transports &= allowed_transports
    if explicit_transports and not candidate_transports:
        raise ValueError(
            "service_action declares a transport that is not permitted by "
            f"allowed_tool_categories {capability_scope_service_categories(normalized_allowed_categories)!r}"
        )
    if not candidate_transports:
        raise ValueError(
            "service_action must declare a supported service transport via service_transport or service/* allowed_tool_categories"
        )

    url_scheme = str(urlparse(normalized_url).scheme or "").strip().lower()
    viable_transports = [
        transport
        for transport in sorted(candidate_transports)
        if url_scheme in _SERVICE_ACTION_TRANSPORT_SCHEMES[transport]
    ]
    if not viable_transports:
        allowed_schemes = sorted(
            {
                scheme
                for transport in sorted(candidate_transports)
                for scheme in _SERVICE_ACTION_TRANSPORT_SCHEMES[transport]
            }
        )
        if len(candidate_transports) == 1:
            transport = next(iter(candidate_transports))
            raise ValueError(
                f"service_action transport {transport!r} only permits URL schemes {allowed_schemes!r}; got {normalized_url!r}"
            )
        raise ValueError(
            f"service_action transports {sorted(candidate_transports)!r} only permit URL schemes {allowed_schemes!r}; got {normalized_url!r}"
        )
    if len(viable_transports) != 1:
        raise ValueError(
            f"service_action declares conflicting transports {viable_transports!r}; declare exactly one transport family"
        )
    transport = viable_transports[0]
    return ServiceActionTransportCompatibility(
        transport=transport,
        allowed_schemes=_SERVICE_ACTION_TRANSPORT_SCHEMES[transport],
        url_scheme=url_scheme,
    )


PLAN_NODE_DESCRIPTOR_REGISTRY: Dict[str, PlanNodeDescriptor] = {
    "builtin_op": PlanNodeDescriptor(
        executor_name="_execute_builtin_node",
        value_producing=True,
        branchable=True,
        requires_default_provider=False,
        prompt_local_only_allowed=True,
    ),
    "memory_lookup": PlanNodeDescriptor(
        executor_name="_execute_memory_lookup_node",
        value_producing=True,
        branchable=True,
        requires_default_provider=False,
        prompt_local_only_allowed=True,
    ),
    "tool_call": PlanNodeDescriptor(
        executor_name="_execute_tool_call_node",
        value_producing=True,
        branchable=True,
        requires_default_provider=False,
        prompt_local_only_allowed=True,
        provider_backed_metadata_key="provider_backed",
        validation_tags=["tool_like"],
    ),
    "tool_synthesis": PlanNodeDescriptor(
        executor_name="_execute_tool_synthesis_node",
        value_producing=True,
        branchable=True,
        requires_default_provider=False,
        prompt_local_only_allowed=True,
        provider_backed_metadata_key="provider_backed",
        validation_tags=["tool_like"],
    ),
    "direct_response": PlanNodeDescriptor(
        executor_name="_execute_direct_response_node",
        value_producing=True,
        branchable=True,
        requires_default_provider=True,
        prompt_local_only_allowed=False,
    ),
    "repo_patch": PlanNodeDescriptor(
        executor_name="_execute_repo_patch_node",
        value_producing=True,
        branchable=True,
        requires_default_provider=False,
        prompt_local_only_allowed=False,
        provider_backed_metadata_key="provider_backed",
        validation_tags=["repo_patch"],
    ),
    "service_action": PlanNodeDescriptor(
        executor_name="_execute_service_action_node",
        value_producing=True,
        branchable=True,
        requires_default_provider=False,
        prompt_local_only_allowed=False,
        validation_tags=["service_action"],
    ),
    "merge": PlanNodeDescriptor(
        executor_name="_execute_merge_node",
        value_producing=True,
        branchable=False,
        requires_default_provider=False,
        prompt_local_only_allowed=True,
        validation_tags=["merge"],
    ),
    "verify": PlanNodeDescriptor(
        executor_name="_execute_verify_node",
        value_producing=False,
        branchable=False,
        requires_default_provider=False,
        prompt_local_only_allowed=True,
        validation_tags=["verify"],
    ),
}


def get_plan_node_descriptor(node_kind: str) -> PlanNodeDescriptor:
    normalized = str(node_kind or "").strip()
    if normalized not in PLAN_NODE_DESCRIPTOR_REGISTRY:
        raise ValueError(f"unsupported execution plan node kind {normalized!r}")
    return PLAN_NODE_DESCRIPTOR_REGISTRY[normalized]


def plan_node_requires_default_provider(node: PlanNode) -> bool:
    descriptor = get_plan_node_descriptor(str(node.node_kind))
    if descriptor.provider_backed_metadata_key:
        return bool(node.metadata.get(descriptor.provider_backed_metadata_key))
    return descriptor.requires_default_provider


def plan_node_allowed_in_prompt_mode_local_only(node: PlanNode) -> bool:
    descriptor = get_plan_node_descriptor(str(node.node_kind))
    if descriptor.provider_backed_metadata_key:
        return not bool(node.metadata.get(descriptor.provider_backed_metadata_key))
    return descriptor.prompt_local_only_allowed


def _validate_tool_like_node(node: PlanNode, node_map: Dict[str, PlanNode], plan_values: Dict[str, Any]) -> None:
    if not str(node.tool_hint or "").strip() and not node.allowed_tool_categories:
        category_hint = str(node.metadata.get("tool_category_hint", "") or "").strip()
        if not category_hint:
            raise ValueError(f"{node.node_kind} node {node.node_id!r} must declare a tool hint or category hint")
    if str(node.node_kind) == "tool_synthesis":
        expression = str(node.expression or "").strip()
        synthesis_template = str(node.metadata.get("synthesis_template", "") or "").strip()
        if not expression and not synthesis_template:
            raise ValueError(
                f"tool_synthesis node {node.node_id!r} must declare an expression or synthesis template metadata"
            )


def _validate_merge_node(node: PlanNode, node_map: Dict[str, PlanNode], plan_values: Dict[str, Any]) -> None:
    branch_group = str(node.metadata.get("consumes_branch_group", "") or "").strip()
    consumes_node_ids = [str(node_id).strip() for node_id in node.metadata.get("consumes_node_ids", []) if str(node_id).strip()]
    if not branch_group:
        raise ValueError(f"merge node {node.node_id!r} must declare metadata.consumes_branch_group")
    members = [candidate.node_id for candidate in node_map.values() if candidate.branch_group_id == branch_group]
    if not members:
        raise ValueError(f"merge node {node.node_id!r} references unknown branch group {branch_group!r}")
    if list(node.dependencies) != members:
        raise ValueError(
            f"merge node {node.node_id!r} must depend on every member of branch group {branch_group!r}"
        )
    if consumes_node_ids and consumes_node_ids != members:
        raise ValueError(f"merge node {node.node_id!r} consumes_node_ids must match branch-group members")


def _validate_verify_node(node: PlanNode, node_map: Dict[str, PlanNode], plan_values: Dict[str, Any]) -> None:
    if not node.dependencies:
        raise ValueError(f"verify node {node.node_id!r} must depend on at least one value-producing node")
    terminal_output_keys = set(plan_values.get("terminal_output_keys", []))
    if str(node.output_key or "").strip() in terminal_output_keys:
        raise ValueError(f"verify node {node.node_id!r} may not produce a terminal output key")
    for dependency_id in node.dependencies:
        dependency_node = node_map[dependency_id]
        if not get_plan_node_descriptor(str(dependency_node.node_kind)).value_producing:
            raise ValueError(
                f"verify node {node.node_id!r} must depend only on value-producing nodes, found {dependency_node.node_kind!r}"
            )


def _validate_repo_patch_node(node: PlanNode, node_map: Dict[str, PlanNode], plan_values: Dict[str, Any]) -> None:
    target_file_paths = [
        str(path).strip()
        for path in node.static_args.get("target_file_paths", node.metadata.get("target_file_paths", []))
        if str(path).strip()
    ]
    if not target_file_paths:
        raise ValueError(f"repo_patch node {node.node_id!r} must declare target_file_paths")


def _validate_service_action_node(node: PlanNode, node_map: Dict[str, PlanNode], plan_values: Dict[str, Any]) -> None:
    url = str(node.static_args.get("url", node.metadata.get("url", "")) or "").strip()
    if not url:
        raise ValueError(f"service_action node {node.node_id!r} must declare a target url")
    method = str(node.static_args.get("method", node.metadata.get("method", "GET")) or "GET").strip().upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise ValueError(f"service_action node {node.node_id!r} has unsupported method {method!r}")
    service_action_transport_compatibility(
        url=url,
        service_transport=node.static_args.get("service_transport", node.metadata.get("service_transport")),
        category_hint=node.metadata.get("tool_category_hint", node.metadata.get("service_category_hint")),
        allowed_tool_categories=node.allowed_tool_categories,
    )


PLAN_NODE_VALIDATION_HOOKS: Dict[str, Callable[[PlanNode, Dict[str, PlanNode], Dict[str, Any]], None]] = {
    "tool_like": _validate_tool_like_node,
    "repo_patch": _validate_repo_patch_node,
    "service_action": _validate_service_action_node,
    "merge": _validate_merge_node,
    "verify": _validate_verify_node,
}


class VerificationPlan(BaseModel):
    mode: str = "none"
    required: bool = False
    checker_ladder: List[str] = Field(default_factory=list)
    exact_verifier_required: bool = False
    artifact_contract: Dict[str, Any] = Field(default_factory=dict)
    terminal_nodes: List[str] = Field(default_factory=list)
    verifier_type: str = "none"
    expected: Any = None


class ExecutionFlags(BaseModel):
    allow_best_effort: bool = False
    allow_resume: bool = True
    allow_branching: bool = True
    allow_tool_synthesis: bool = True
    allow_async_handles: bool = True
    requires_terminal_verification: bool = False


class ExecutionPlan(BaseModel):
    plan_schema_version: str = "agintor.execution-plan.v1"
    plan_digest: str = ""
    plan_id: str
    request_id: str
    origin: PlanOrigin
    objective: str
    context_refs: List[Dict[str, Any]] = Field(default_factory=list)
    file_refs: List[str] = Field(default_factory=list)
    file_ref_specs: List[RequestFileRef] = Field(default_factory=list)
    plan_constants: Dict[str, Any] = Field(default_factory=dict)
    nodes: List[PlanNode] = Field(default_factory=list)
    root_node_ids: List[str] = Field(default_factory=list)
    terminal_output_keys: List[str] = Field(default_factory=list)
    verification_plan: VerificationPlan = Field(default_factory=VerificationPlan)
    execution_flags: ExecutionFlags = Field(default_factory=ExecutionFlags)
    allowed_tool_categories: List[str] = Field(default_factory=list)
    budget_overrides: Dict[str, Any] = Field(default_factory=dict)
    externally_visible: bool = True
    trace_context: Optional[OpenAITraceContext] = None
    lifecycle_state: Literal[
        "compiled",
        "validated",
        "loaded",
        "running",
        "completed",
        "cancelled",
        "failed",
    ] = "compiled"

    @root_validator(pre=False, allow_reuse=True)
    def validate_execution_plan(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        def to_jsonable(value: Any) -> Any:
            if hasattr(value, "dict"):
                return to_jsonable(value.dict())
            if isinstance(value, dict):
                return {str(key): to_jsonable(item) for key, item in value.items()}
            if isinstance(value, list):
                return [to_jsonable(item) for item in value]
            return value

        nodes = list(values.get("nodes", []))
        node_map = {node.node_id: node for node in nodes}
        if len(node_map) != len(nodes):
            raise ValueError("execution plan node_id values must be unique")

        verification_plan = values.get("verification_plan") or VerificationPlan()

        for root_id in values.get("root_node_ids", []):
            if root_id not in node_map:
                raise ValueError(f"execution plan root node {root_id!r} does not exist")

        for terminal_id in verification_plan.terminal_nodes:
            if terminal_id not in node_map:
                raise ValueError(f"verification terminal node {terminal_id!r} does not exist")

        for node in nodes:
            descriptor = get_plan_node_descriptor(str(node.node_kind))
            for dep in node.dependencies:
                if dep not in node_map:
                    raise ValueError(f"execution plan dependency {dep!r} for node {node.node_id!r} does not exist")
            if node.branch_group_id and not descriptor.branchable:
                raise ValueError(
                    f"execution plan node {node.node_id!r} of kind {node.node_kind!r} may not declare branch_group_id"
                )
            for tag in descriptor.validation_tags:
                PLAN_NODE_VALIDATION_HOOKS[tag](node, node_map, values)

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visited:
                return
            if node_id in visiting:
                raise ValueError("execution plan graph must be acyclic")
            visiting.add(node_id)
            for dep_id in node_map[node_id].dependencies:
                visit(dep_id)
            visiting.remove(node_id)
            visited.add(node_id)

        for node in nodes:
            visit(node.node_id)

        reachable: set[str] = set()

        def mark(node_id: str) -> None:
            if node_id in reachable:
                return
            reachable.add(node_id)
            for child in nodes:
                if node_id in child.dependencies:
                    mark(child.node_id)

        for root_id in values.get("root_node_ids", []):
            mark(root_id)

        produced_outputs: Dict[str, PlanNode] = {}
        for node in nodes:
            if node.node_id not in reachable or not str(node.output_key).strip():
                continue
            if get_plan_node_descriptor(str(node.node_kind)).value_producing:
                if node.output_key in produced_outputs:
                    raise ValueError(
                        f"duplicate execution plan output_key {node.output_key!r} is not allowed"
                    )
                produced_outputs[node.output_key] = node
        for terminal_key in values.get("terminal_output_keys", []):
            producer = produced_outputs.get(terminal_key)
            if producer is None:
                raise ValueError(
                    f"terminal output key {terminal_key!r} is not produced by a reachable value-producing node"
                )
            if str(producer.node_kind) == "verify":
                raise ValueError(f"terminal output key {terminal_key!r} may not be produced by a verify node")

        branch_groups: Dict[str, List[PlanNode]] = {}
        for node in nodes:
            if node.branch_group_id:
                branch_groups.setdefault(node.branch_group_id, []).append(node)
        for branch_group_id, grouped_nodes in branch_groups.items():
            dependency_signatures = {tuple(node.dependencies) for node in grouped_nodes}
            if len(dependency_signatures) > 1:
                raise ValueError(
                    f"branch group {branch_group_id!r} must be reachable from one live frontier with identical dependencies"
                )

        if verification_plan.required or verification_plan.exact_verifier_required:
            if not any(str(node.node_kind) == "verify" for node in nodes):
                raise ValueError("execution plan requires an explicit verify node when terminal verification is required")

        if not values.get("plan_digest"):
            digest_payload = {key: value for key, value in values.items() if key != "plan_digest"}
            digest_payload["request_id"] = None
            trace_context = digest_payload.get("trace_context")
            if hasattr(trace_context, "dict"):
                trace_context = trace_context.dict()
            if isinstance(trace_context, dict):
                normalized_trace_context = {str(key): to_jsonable(item) for key, item in trace_context.items()}
                normalized_trace_context["request_id"] = None
                digest_payload["trace_context"] = normalized_trace_context
            values["plan_digest"] = stable_hash(to_jsonable(digest_payload))
        return values


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
    request_file_refs: List[RequestFileRef] = Field(default_factory=list)
    output_schema: Dict[str, Any] = Field(default_factory=dict)
    allowed_tool_categories: List[str] = Field(default_factory=list)
    verification_preference: str = "verified_if_available"
    budget_overrides: Dict[str, Any] = Field(default_factory=dict)


class SolveResult(BaseModel):
    request_id: str
    runtime_hash: str
    run_id: str = ""
    run_root: str = ""
    attempt_id: str = ""
    latest_checkpoint_ref: Optional[str] = None
    run_lifecycle_state: Optional[Literal["running", "paused", "completed", "failed", "cancelled", "pruned"]] = None
    run_resumable: bool = False
    run_prune_eligible: bool = False
    mode: str = "benchmark"
    artifact: Any
    status: str
    verification_status: str = "best_effort"
    summary: str
    checks: List[Dict[str, Any]] = Field(default_factory=list)
    trace_ref: Optional[str] = None
    checkpoint_ref: Optional[str] = None
    budget: Dict[str, Any] = Field(default_factory=dict)
    provider_usage: Dict[str, Any] = Field(default_factory=dict)
    faults: Dict[str, Any] = Field(default_factory=dict)
    recoverability: str = "none"
    verified: bool = False
    best_effort: bool = False


class RuntimeSolveRequest(BaseModel):
    request_id: str
    evaluation_unit_id: str = ""
    run_id: str = ""
    run_root: str = ""
    attempt_id: str = ""
    runtime_backend: str
    mode: Literal["benchmark", "user_request"]
    seed: int = 0
    task: Optional["BenchmarkTask"] = None
    solve_request: Optional[SolveRequest] = None
    budget_overrides: Dict[str, Any] = Field(default_factory=dict)
    trace_context: Optional[OpenAITraceContext] = None

    @root_validator(pre=False, allow_reuse=True)
    def validate_mode_payload(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        mode = values.get("mode")
        task = values.get("task")
        solve_request = values.get("solve_request")
        if mode == "benchmark" and task is None:
            raise ValueError("benchmark solve requests require a benchmark task")
        if mode == "user_request" and solve_request is None:
            raise ValueError("user_request solve requests require a solve_request payload")
        return values


class RunResult(BaseModel):
    request_id: str = ""
    plan_id: str = ""
    run_id: str = ""
    run_root: str = ""
    attempt_id: str = ""
    runtime_hash: str = ""
    runtime_backend: str = ""
    latest_checkpoint_ref: Optional[str] = None
    run_lifecycle_state: Optional[Literal["running", "paused", "completed", "failed", "cancelled", "pruned"]] = None
    run_resumable: bool = False
    run_prune_eligible: bool = False
    task_id: str
    seed: int
    artifact: Any
    verifier_score: float
    cost: float
    latency: float
    faults: int
    trace: List[Dict[str, Any]] = Field(default_factory=list)
    trace_context: Optional[OpenAITraceContext] = None
    trace_path: Optional[str] = None
    hard_invalid: bool = False
    invalid_reason: Optional[str] = None
    failure_kind: Optional[str] = None
    mode: Optional[str] = None
    lifecycle_state: Optional[str] = None
    created_tools: int = 0
    promoted_nodes: int = 0
    checks_used: int = 0
    model_calls: int = 0
    tokens_used: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    utility: Optional[float] = None
    checkpoint_ref: Optional[str] = None
    provider_usage: Dict[str, Any] = Field(default_factory=dict)

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


class RunManifest(BaseModel):
    run_id: str
    run_root: str
    request_id: str = ""
    evaluation_unit_id: str = ""
    request_mode: Literal["benchmark", "user_request", "batch"] = "benchmark"
    runtime_hash: str = ""
    runtime_abi: str = ""
    storage_schema_version: str = ""
    runtime_backend: str = "local"
    task_id: Optional[str] = None
    seed: Optional[int] = None
    trace_context: Optional[OpenAITraceContext] = None
    current_attempt_id: Optional[str] = None
    latest_checkpoint_ref: Optional[str] = None
    lifecycle_state: Literal["running", "paused", "completed", "failed", "cancelled", "pruned"] = "running"
    resumable: bool = False
    prune_eligible: bool = False
    last_failure_kind: Optional[str] = None
    created_at: float = 0.0
    updated_at: float = 0.0


class AttemptManifest(BaseModel):
    attempt_id: str
    run_id: str
    run_root: str
    sequence_no: int
    launch_kind: Literal["solve", "run_batch", "resume"]
    lifecycle_state: Literal["running", "completed", "paused", "failed", "crashed", "cancelled"] = "running"
    resumed_from_checkpoint_ref: Optional[str] = None
    workspace_root: str
    latest_checkpoint_ref: Optional[str] = None
    failure_kind: Optional[str] = None
    started_at: float = 0.0
    updated_at: float = 0.0
    finished_at: Optional[float] = None


class KernelManifest(BaseModel):
    schema_version: str
    runtime_abi: str
    kernel_version: str
    storage_schema_version: str
    package_name: str
    entry_module: str
    files: Dict[str, str] = Field(default_factory=dict)
    capability_flags: List[str] = Field(default_factory=list)


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


class RecoveryFailureKind(str, Enum):
    CHECKPOINT_NOT_FOUND = "checkpoint_not_found"
    CHECKPOINT_CORRUPT = "checkpoint_corrupt"
    REQUEST_MISMATCH = "request_mismatch"
    RUNTIME_ABI_MISMATCH = "runtime_abi_mismatch"
    STORAGE_SCHEMA_MISMATCH = "storage_schema_mismatch"
    RUNTIME_HASH_MISMATCH = "runtime_hash_mismatch"
    PLAN_DIGEST_MISMATCH = "plan_digest_mismatch"
    FRAME_RECONSTRUCTION_FAILED = "frame_reconstruction_failed"
    RECEIPT_RECONCILIATION_FAILED = "receipt_reconciliation_failed"


class CheckpointReference(BaseModel):
    ref: str
    run_id: str = ""
    run_root: str = ""
    attempt_id: str = ""
    task_id: str = ""
    seed: int = 0
    request_id: str = ""
    plan_id: str = ""
    checkpoint_id: str = ""
    sequence_no: int = 0
    boundary: str = ""
    created_at: float = 0.0
    checkpoint_count: int = 0
    latest: bool = False
    resume_eligible: bool = True
    resume_ineligibility_reason: Optional[str] = None


class BranchBudget(BaseModel):
    model_calls_max: int = 0
    checks_max: int = 0
    latency_max: float = 0.0
    allow_tool_synthesis: bool = False


class ReplayAllocation(BaseModel):
    allocation_key: str
    cursor_start: int = 0
    cursor_end: int = 0
    next_cursor: int = 0


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

    @root_validator(pre=False, allow_reuse=True)
    def validate_terminal_contract(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        status = str(values.get("status", "") or "")
        cancellation_record = values.get("cancellation_record")
        failure_kind = values.get("failure_kind")
        failure_details = dict(values.get("failure_details") or {})
        if status == "cancelled":
            if cancellation_record is None:
                raise ValueError("cancelled branches require cancellation_record")
            if failure_kind is not None or failure_details:
                raise ValueError("cancelled branches may not carry failure_kind/failure_details")
            return values
        if status == "failed":
            if cancellation_record is not None:
                raise ValueError("failed branches may not carry cancellation_record")
            if failure_kind is None:
                raise ValueError("failed branches require failure_kind")
            return values
        if cancellation_record is not None:
            raise ValueError("only cancelled branches may carry cancellation_record")
        if failure_kind is not None or failure_details:
            raise ValueError("only failed branches may carry failure_kind/failure_details")
        return values


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


class ReceiptReconciliationRecord(BaseModel):
    status: Literal[
        "reused_terminal_receipt",
        "terminalized_from_handle",
        "terminalized_from_provider_hook",
        "abandoned_by_cancellation",
        "blocked_strict",
        "blocked_best_effort",
    ]
    source: Literal["resume_reconciliation", "branch_cancellation"] = "resume_reconciliation"
    details: Dict[str, Any] = Field(default_factory=dict)
    reconciled_at: float = 0.0


class SideEffectReceipt(BaseModel):
    side_effect_id: str
    action_fingerprint: str
    idempotency_key: str
    action_kind: Literal[
        "tool_launch",
        "tool_completion",
        "provider_request",
        "provider_completion",
        "service_action",
        "filesystem_write",
    ]
    request_id: str = ""
    plan_id: str = ""
    frame_id: str = ""
    node_id: str = ""
    branch_id: Optional[str] = None
    trace_context: Optional[OpenAITraceContext] = None
    request_digest: str
    backend: str
    status: Literal["launched", "completed", "failed", "reconciled", "abandoned"] = "launched"
    result_ref: Dict[str, Any] = Field(default_factory=dict)
    replay_policy: str = "reuse_if_completed"
    reconciliation_policy: str = "strict"
    reconciliation: Optional[ReceiptReconciliationRecord] = None
    created_at: float = 0.0


TERMINAL_RECEIPT_STATUSES = frozenset({"completed", "failed", "reconciled", "abandoned"})
TerminalReceiptStatus = Literal["completed", "failed", "reconciled", "abandoned"]


def is_terminal_receipt(receipt: "SideEffectReceipt" | Dict[str, Any]) -> bool:
    normalized = receipt if isinstance(receipt, SideEffectReceipt) else SideEffectReceipt(**dict(receipt))
    return str(normalized.status or "") in TERMINAL_RECEIPT_STATUSES


def terminalize_receipt(
    receipt: "SideEffectReceipt" | Dict[str, Any],
    *,
    status: TerminalReceiptStatus,
    reconciliation_status: Literal[
        "reused_terminal_receipt",
        "terminalized_from_handle",
        "terminalized_from_provider_hook",
        "abandoned_by_cancellation",
        "blocked_strict",
        "blocked_best_effort",
    ],
    reconciliation_source: Literal["resume_reconciliation", "branch_cancellation"],
    reconciliation_details: Optional[Dict[str, Any]] = None,
    result_ref_updates: Optional[Dict[str, Any]] = None,
) -> "SideEffectReceipt":
    normalized = receipt if isinstance(receipt, SideEffectReceipt) else SideEffectReceipt(**dict(receipt))
    if is_terminal_receipt(normalized):
        return normalized
    merged_result_ref = dict(normalized.result_ref or {})
    merged_result_ref.update(dict(result_ref_updates or {}))
    return normalized.copy(
        update={
            "status": status,
            "result_ref": merged_result_ref,
            "reconciliation": ReceiptReconciliationRecord(
                status=reconciliation_status,
                source=reconciliation_source,
                details=dict(reconciliation_details or {}),
                reconciled_at=now_ts(),
            ),
        },
        deep=True,
    )


class CheckpointEnvelope(BaseModel):
    checkpoint_schema_version: str = "agintor.checkpoint-envelope.v3"
    checkpoint_id: str
    runtime_abi: str
    storage_schema_version: str
    runtime_hash: str
    run_id: str = ""
    run_root: str = ""
    attempt_id: str = ""
    runtime_backend: str = ""
    request_id: str
    origin_request_id: Optional[str] = None
    source_checkpoint_ref: Optional[str] = None
    plan_id: str
    task_id: str
    seed: int
    sequence_no: int = 0
    boundary: str = ""
    created_at: float = 0.0
    resume_eligible: bool = True
    resume_ineligibility_reason: Optional[str] = None
    plan_snapshot: Dict[str, Any] = Field(default_factory=dict)
    task_payload: Dict[str, Any] = Field(default_factory=dict)
    runtime_state_snapshot: RuntimeStateSnapshot = Field(default_factory=RuntimeStateSnapshot)
    shell_state_snapshot: ShellStateSnapshot = Field(default_factory=ShellStateSnapshot)
    side_effect_ledger: Dict[str, List[SideEffectReceipt]] = Field(default_factory=lambda: {"receipts": []})
    attempt_snapshot: AttemptSnapshot = Field(default_factory=AttemptSnapshot)
    working_state_summary: Dict[str, Any] = Field(default_factory=dict)
    trace_cursor: Dict[str, Any] = Field(default_factory=dict)


class InspectRequest(BaseModel):
    request_id: str
    requested_backend: str = "local"
    expected_runtime_abi: str
    expected_kernel_version: Optional[str] = None
    expected_storage_schema_version: Optional[str] = None


class ResumeRequest(BaseModel):
    request_id: str = ""
    run_ref: Optional[str] = None
    checkpoint_ref: Optional[str] = None
    trace_context: Optional[OpenAITraceContext] = None
    reconciliation_policy: Literal["strict", "best_effort"] = "strict"

    @root_validator(pre=False, allow_reuse=True)
    def validate_resume_target(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        run_ref = str(values.get("run_ref") or "").strip()
        checkpoint_ref = str(values.get("checkpoint_ref") or "").strip()
        if not run_ref and not checkpoint_ref:
            raise ValueError("resume requires run_ref or checkpoint_ref")
        return values


class RuntimeResumeRequest(BaseModel):
    request_id: str = ""
    evaluation_unit_id: str = ""
    run_ref: Optional[str] = None
    checkpoint_ref: Optional[str] = None
    run_id: str = ""
    run_root: str = ""
    attempt_id: str = ""
    runtime_backend: str = "local"
    checkpoint_store_dir: str = ""
    trace_context: Optional[OpenAITraceContext] = None
    reconciliation_policy: Literal["strict", "best_effort"] = "strict"

    @root_validator(pre=False, allow_reuse=True)
    def validate_resume_target(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        run_ref = str(values.get("run_ref") or "").strip()
        checkpoint_ref = str(values.get("checkpoint_ref") or "").strip()
        if not run_ref and not checkpoint_ref:
            raise ValueError("resume requires run_ref or checkpoint_ref")
        return values


class CapabilityExchange(BaseModel):
    runtime_abi: str
    kernel_version: str
    storage_schema_version: str
    supported_backends: List[str] = Field(default_factory=list)
    tool_runtimes: List[str] = Field(default_factory=list)
    checkpoint_support: bool = True
    runtime_asset_capabilities: Dict[str, bool] = Field(default_factory=dict)
    side_effect_receipts: bool = False
    resume_support: bool = True
    runtime_isolation_policy: Optional[RuntimeIsolationPolicy] = None
    supported_guarantees: List[str] = Field(default_factory=list)
    effective_guarantees: List[str] = Field(default_factory=list)
    required_env_names: List[str] = Field(default_factory=list)
    required_env_any_of: List[List[str]] = Field(default_factory=list)
    capability_flags: List[str] = Field(default_factory=list)


class RuntimeSolveResponse(BaseModel):
    request_id: str
    capability_exchange: CapabilityExchange
    solve_result: SolveResult


class RuntimeTaskInvocation(BaseModel):
    request_id: str
    evaluation_unit_id: str = ""
    episode_kind: Literal["single_task", "transfer_episode", "benchmark_duplicate"] = "single_task"
    episode_step_index: Optional[int] = None
    run_id: str = ""
    run_root: str = ""
    attempt_id: str = ""
    runtime_backend: str = ""
    seed: int
    task: BenchmarkTask
    trace_context: Optional[OpenAITraceContext] = None

    @root_validator(pre=False, allow_reuse=True)
    def validate_episode_grouping(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        task = values.get("task")
        episode_kind = str(values.get("episode_kind") or "single_task")
        if (
            episode_kind == "single_task"
            and task is not None
            and bool(getattr(task, "transfer_scored", False))
            and str(getattr(task, "episode_id", "") or "").strip()
        ):
            episode_kind = "transfer_episode"
            values["episode_kind"] = episode_kind
        if episode_kind == "transfer_episode":
            if values.get("episode_step_index") is None and task is not None:
                values["episode_step_index"] = int(getattr(task, "episode_order", 0) or 0)
        else:
            values["episode_step_index"] = None
        return values


class RuntimeBatchRequest(BaseModel):
    request_id: str
    runtime_backend: str
    budget_overrides: Dict[str, Any] = Field(default_factory=dict)
    invocations: List[RuntimeTaskInvocation] = Field(default_factory=list)
    trace_context: Optional[OpenAITraceContext] = None


class ExecutionUnitRequestEnvelope(BaseModel):
    request_kind: Literal["runtime_solve_request", "runtime_task_invocation", "runtime_task_invocation_group"]
    request_mode: Literal["benchmark", "user_request", "batch"]
    request_id: str
    evaluation_unit_id: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    member_invocations: List[RuntimeTaskInvocation] = Field(default_factory=list)


class RuntimeBatchResponse(BaseModel):
    request_id: str
    capability_exchange: CapabilityExchange
    run_results: List[RunResult] = Field(default_factory=list)
    provider_usage: Dict[str, Any] = Field(default_factory=dict)


_FORWARD_REF_MODELS = [
    RuntimeStateSnapshot,
    BranchResumeSnapshot,
    BranchResult,
    CheckpointEnvelope,
]

for _model in _FORWARD_REF_MODELS:
    if hasattr(_model, "model_rebuild"):
        _model.model_rebuild()
    else:
        _model.update_forward_refs()
