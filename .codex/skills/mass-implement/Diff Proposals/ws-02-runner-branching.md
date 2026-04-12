# Workstream 2 Runtime Execution and Orchestration Proposal

## Technical Direction

- Replace raw `BenchmarkTask.operations` execution with a runtime-owned `ExecutionPlan` state machine.
- Compile both benchmark requests and user solve requests into deterministic plans inside the runtime boundary.
- Turn horizontal mode into real concurrent branch groups with `ThreadPoolExecutor`, `wait(..., return_when=FIRST_EXCEPTION)`, `shutdown(cancel_futures=True)`, and cooperative `threading.Event` cancellation checks at node and side-effect boundaries.
- Make branch isolation copy-in and publication-out only. Parent state mutates exclusively through accepted `BranchPublication` records.
- Promote checkpoints into restartable `CheckpointEnvelope` artifacts with `SideEffectReceipt` reconciliation data.
- Stamp runtime requests, plans, frames, branches, and runtime events with `OpenAITraceContext`; keep `plan_id` as a separate runtime-owned field on events, checkpoints, and results.
- Add an explicit runtime isolation contract that is checked both before launch and again inside the runtime, with fail-closed behavior on mismatch.

## Proposed Diffs

### `agintor/schemas.py`

```text
<<<<<<< SEARCH
class RuntimeStateSnapshot(BaseModel):
    queue_length: int
    budget_state: Dict[str, Any]
    unresolved_count: int
    visible_tool_count: int
    open_handle_count: int
    confidence: float
    active_mode: Optional[str] = None
=======
class RuntimeStateSnapshot(BaseModel):
    queue_length: int
    budget_state: Dict[str, Any]
    unresolved_count: int
    visible_tool_count: int
    open_handle_count: int
    confidence: float
    active_mode: Optional[str] = None
    lifecycle_state: str = "idle"
    plan_id: Optional[str] = None
    active_branch_count: int = 0


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


class InputBinding(BaseModel):
    target_arg: str
    source_kind: Literal["request_context", "request_file", "upstream_output", "plan_constant"]
    source_ref: str
    required: bool = True


class PlanOrigin(BaseModel):
    origin_kind: Literal["benchmark", "user_request"]
    source_task_id: Optional[str] = None
    source_request_id: Optional[str] = None
    source_suite: Optional[str] = None
    adapter_kind: str
    adaptation_assumptions: List[str] = Field(default_factory=list)


class PlanNode(BaseModel):
    node_id: str
    node_kind: Literal[
        "builtin_op",
        "memory_lookup",
        "tool_call",
        "tool_synthesis",
        "direct_response",
        "repo_patch",
        "service_action",
        "checkpoint",
        "merge",
        "verify",
    ]
    instruction: str
    output_key: str
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


class VerificationPlan(BaseModel):
    mode: str
    required: bool
    checker_ladder: List[str] = Field(default_factory=list)
    exact_verifier_required: bool = False
    artifact_contract: Dict[str, Any] = Field(default_factory=dict)
    terminal_nodes: List[str] = Field(default_factory=list)


class ExecutionFlags(BaseModel):
    allow_best_effort: bool = False
    allow_resume: bool = True
    allow_branching: bool = True
    allow_tool_synthesis: bool = True
    allow_async_handles: bool = True
    requires_terminal_verification: bool = True


class ExecutionPlan(BaseModel):
    plan_schema_version: str
    plan_digest: str
    plan_id: str
    request_id: str
    origin: PlanOrigin
    objective: str
    context_refs: List[str] = Field(default_factory=list)
    file_refs: List[str] = Field(default_factory=list)
    nodes: List[PlanNode] = Field(default_factory=list)
    root_node_ids: List[str] = Field(default_factory=list)
    terminal_output_keys: List[str] = Field(default_factory=list)
    verification_plan: VerificationPlan
    execution_flags: ExecutionFlags
    allowed_tool_categories: List[str] = Field(default_factory=list)
    budget_overrides: Dict[str, Any] = Field(default_factory=dict)
    externally_visible: bool = False
    trace_context: OpenAITraceContext = Field(default_factory=OpenAITraceContext)
    lifecycle_state: Literal["compiled", "validated", "loaded", "running", "completed", "cancelled", "failed"] = "compiled"


class BranchBudget(BaseModel):
    branch_id: str
    model_call_budget: int
    checker_budget: int
    latency_budget_s: float
    allow_tool_synthesis: bool = False
    escalation_granted: bool = False


class BranchPlan(BaseModel):
    branch_id: str
    parent_frame_id: str
    request_id: str
    trace_context: OpenAITraceContext = Field(default_factory=OpenAITraceContext)
    assigned_node_ids: List[str] = Field(default_factory=list)
    merge_priority: int = 0
    reserved_budget: BranchBudget
    cancel_on_parent_stop: bool = True
    metadata: Dict[str, Any] = Field(default_factory=dict)


class CancellationRecord(BaseModel):
    reason: Literal[
        "fatal_branch_fault",
        "budget_exhaustion",
        "superior_branch_dominance",
        "verification_failure",
        "parent_stop_policy",
        "external_interrupt",
    ]
    requested_at: float
    requested_by: str
    details: Dict[str, Any] = Field(default_factory=dict)
    cleanup_completed: bool = False


class BranchPublication(BaseModel):
    publication_id: str
    publication_kind: Literal[
        "candidate_artifact",
        "verifier_evidence",
        "trace_rows",
        "budget_usage",
        "handle_reference",
        "cleanup_record",
        "reconciliation_record",
    ]
    logical_key: str
    sequence_no: int
    accepted: bool = False
    plan_id: str
    branch_id: str
    node_id: Optional[str] = None
    trace_context: OpenAITraceContext = Field(default_factory=OpenAITraceContext)
    payload: Dict[str, Any] = Field(default_factory=dict)
    verifier_support: float = 0.0
    unresolved_critical_count: int = 0


class BranchState(BaseModel):
    branch_id: str
    status: Literal["pending", "running", "completed", "cancelled", "failed"] = "pending"
    assigned_node_ids: List[str] = Field(default_factory=list)
    publications: List[BranchPublication] = Field(default_factory=list)
    budget_consumed: Dict[str, Any] = Field(default_factory=dict)
    cancellation_record: Optional[CancellationRecord] = None
    error: Optional[str] = None


class BranchResult(BaseModel):
    branch_id: str
    status: Literal["completed", "cancelled", "failed"]
    publications: List[BranchPublication] = Field(default_factory=list)
    budget_consumed: Dict[str, Any] = Field(default_factory=dict)
    verifier_support: float = 0.0
    unresolved_critical_count: int = 0
    cancellation_record: Optional[CancellationRecord] = None
    error: Optional[str] = None


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
    branch_id: Optional[str] = None
    request_digest: str
    backend: str
    status: str
    result_ref: Optional[str] = None
    replay_policy: str
    reconciliation_policy: str
    created_at: float
    trace_context: OpenAITraceContext = Field(default_factory=OpenAITraceContext)


class RuntimeIsolationPolicy(BaseModel):
    timeout_envelope: Dict[str, Any] = Field(default_factory=dict)
    workspace_root: str = ""
    environment_allowlist: List[str] = Field(default_factory=list)
    network_policy: str = "none"
    filesystem_policy: str = ""
    required_guarantees: List[str] = Field(default_factory=list)
    desired_guarantees: List[str] = Field(default_factory=list)


class RuntimeIsolationReport(BaseModel):
    backend: str
    policy: RuntimeIsolationPolicy
    effective_guarantees: Dict[str, bool] = Field(default_factory=dict)


class CheckpointEnvelope(BaseModel):
    checkpoint_id: str
    runtime_abi: str
    storage_schema_version: str
    runtime_hash: str
    request_id: str
    plan_id: str
    task_id: Optional[str] = None
    seed: Optional[int] = None
    queued_frames: List[Dict[str, Any]] = Field(default_factory=list)
    branch_state: Dict[str, BranchState] = Field(default_factory=dict)
    branch_publications: List[BranchPublication] = Field(default_factory=list)
    unresolved_goals: List[str] = Field(default_factory=list)
    artifact_refs: Dict[str, Any] = Field(default_factory=dict)
    handle_refs: List[str] = Field(default_factory=list)
    budget_state: Dict[str, Any] = Field(default_factory=dict)
    verifier_state: Dict[str, Any] = Field(default_factory=dict)
    working_state_summary: Dict[str, Any] = Field(default_factory=dict)
    trace_cursor: Dict[str, Any] = Field(default_factory=dict)
    side_effect_receipts: List[SideEffectReceipt] = Field(default_factory=list)


class RuntimeEvent(BaseModel):
    event_id: str
    event_kind: Literal[
        "run_started",
        "plan_loaded",
        "plan_compiled",
        "plan_validation_failed",
        "node_started",
        "node_completed",
        "node_failed",
        "branch_started",
        "branch_cancelled",
        "branch_completed",
        "branch_failed",
        "side_effect_recorded",
        "checkpoint_published",
        "checkpoint_restored",
        "side_effect_reconciled",
        "merge_started",
        "merge_completed",
        "terminal_emitted",
        "run_failed",
        "run_cancelled",
    ]
    runtime_state: str
    plan_id: str
    request_id: str
    trace_context: OpenAITraceContext = Field(default_factory=OpenAITraceContext)
    node_id: Optional[str] = None
    branch_id: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)
    created_at: float
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
class SolveRequest(BaseModel):
    request_id: str
    prompt: str
    context_items: List[Dict[str, Any]] = Field(default_factory=list)
    file_paths: List[str] = Field(default_factory=list)
    output_schema: Dict[str, Any] = Field(default_factory=dict)
    allowed_tool_categories: List[str] = Field(default_factory=list)
    verification_preference: str = "verified_if_available"
    budget_overrides: Dict[str, Any] = Field(default_factory=dict)
=======
class SolveRequest(BaseModel):
    request_id: str
    prompt: str
    context_items: List[Dict[str, Any]] = Field(default_factory=list)
    file_paths: List[str] = Field(default_factory=list)
    output_schema: Dict[str, Any] = Field(default_factory=dict)
    allowed_tool_categories: List[str] = Field(default_factory=list)
    verification_preference: str = "verified_if_available"
    budget_overrides: Dict[str, Any] = Field(default_factory=dict)
    trace_context: OpenAITraceContext = Field(default_factory=OpenAITraceContext)


class SolveResult(BaseModel):
    request_id: str
    runtime_hash: str
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
    plan_id: Optional[str] = None
    runtime_state: Optional[str] = None
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
class RuntimeSolveRequest(BaseModel):
    request_id: str
    runtime_backend: str
    mode: Literal["benchmark", "user_request"]
    seed: int = 0
    task: Optional["BenchmarkTask"] = None
    solve_request: Optional[SolveRequest] = None
    budget_overrides: Dict[str, Any] = Field(default_factory=dict)
=======
class RuntimeSolveRequest(BaseModel):
    request_id: str
    runtime_backend: str
    mode: Literal["benchmark", "user_request"]
    seed: int = 0
    task: Optional["BenchmarkTask"] = None
    solve_request: Optional[SolveRequest] = None
    budget_overrides: Dict[str, Any] = Field(default_factory=dict)
    trace_context: OpenAITraceContext = Field(default_factory=OpenAITraceContext)
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
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
    checkpoint_ref: Optional[str] = None
=======
class RunResult(BaseModel):
    task_id: str
    seed: int
    request_id: str
    plan_id: str
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
    checkpoint_ref: Optional[str] = None
    runtime_state: Optional[str] = None
    failure_class: Optional[str] = None
    accepted_publication_ids: List[str] = Field(default_factory=list)
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
class ResumeRequest(BaseModel):
    request_id: str
    checkpoint_ref: CheckpointReference
    prompt: Optional[str] = None
=======
class ResumeRequest(BaseModel):
    request_id: str
    checkpoint_ref: CheckpointReference
    trace_context: OpenAITraceContext = Field(default_factory=OpenAITraceContext)
    reconciliation_policy: Literal["strict", "best_effort"] = "strict"
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
class CapabilityExchange(BaseModel):
    runtime_abi: str
    kernel_version: str
    storage_schema_version: str
    supported_backends: List[str] = Field(default_factory=list)
    tool_runtimes: List[str] = Field(default_factory=list)
    checkpoint_support: bool = True
    runtime_asset_capabilities: Dict[str, bool] = Field(default_factory=dict)
    side_effect_receipts: bool = False
    required_env_names: List[str] = Field(default_factory=list)
    required_env_any_of: List[List[str]] = Field(default_factory=list)
    capability_flags: List[str] = Field(default_factory=list)
=======
class CapabilityExchange(BaseModel):
    runtime_abi: str
    kernel_version: str
    storage_schema_version: str
    supported_backends: List[str] = Field(default_factory=list)
    tool_runtimes: List[str] = Field(default_factory=list)
    checkpoint_support: bool = True
    resume_support: bool = True
    runtime_asset_capabilities: Dict[str, bool] = Field(default_factory=dict)
    side_effect_receipts: bool = True
    required_env_names: List[str] = Field(default_factory=list)
    required_env_any_of: List[List[str]] = Field(default_factory=list)
    capability_flags: List[str] = Field(default_factory=list)
    isolation_report: Optional[RuntimeIsolationReport] = None
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
class RuntimeTaskInvocation(BaseModel):
    seed: int
    task: BenchmarkTask


class RuntimeBatchRequest(BaseModel):
    request_id: str
    runtime_backend: str
    budget_overrides: Dict[str, Any] = Field(default_factory=dict)
    invocations: List[RuntimeTaskInvocation] = Field(default_factory=list)
=======
class RuntimeTaskInvocation(BaseModel):
    request_id: str
    seed: int
    task: BenchmarkTask
    trace_context: OpenAITraceContext = Field(default_factory=OpenAITraceContext)


class RuntimeBatchRequest(BaseModel):
    request_id: str
    runtime_backend: str
    budget_overrides: Dict[str, Any] = Field(default_factory=dict)
    trace_context: OpenAITraceContext = Field(default_factory=OpenAITraceContext)
    invocations: List[RuntimeTaskInvocation] = Field(default_factory=list)
>>>>>>> REPLACE
```

### `agintor/runtime_api.py`

```text
<<<<<<< SEARCH
@dataclass
class AgentFrame:
    agent: AgentTemplate
    objective: str
    operation_ids: list[str]
    depth: int
    checkpoint: Checkpoint | None = None
    parent_id: str | None = None
    worker_id: str | None = None
    role: str = "root"
    tool_scope: list[str] = field(default_factory=list)
    model_class: str = "small"
    metadata: dict[str, Any] = field(default_factory=dict)
=======
@dataclass
class AgentFrame:
    frame_id: str
    agent: AgentTemplate
    objective: str
    plan_id: str
    assigned_node_ids: list[str]
    depth: int
    checkpoint: CheckpointEnvelope | None = None
    parent_frame_id: str | None = None
    branch_id: str | None = None
    role: str = "root"
    tool_scope: list[str] = field(default_factory=list)
    model_class: str = "small"
    trace_context: OpenAITraceContext = field(default_factory=OpenAITraceContext)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RuntimeBudget:
    cost: float = 0.0
    latency: float = 0.0
    calls: int = 0
    checks: int = 0
    tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    C_max: float = 100.0
    L_max: float = 120.0
    M_max: int = 64
    Q_max: int = 16
    context_window_tokens: int = 768
    branch_reservations: dict[str, BranchBudget] = field(default_factory=dict)

    def normalized(self) -> dict[str, float]:
        return {
            "cost": self.cost / max(1.0, self.C_max),
            "latency": self.latency / max(1.0, self.L_max),
            "calls": self.calls / max(1, self.M_max),
            "checks": self.checks / max(1, self.Q_max),
        }

    def exhausted(self) -> bool:
        return any(value >= 1.0 for value in self.normalized().values())

    def reserve_branch_budget(
        self,
        branch_id: str,
        *,
        model_call_budget: int,
        checker_budget: int,
        latency_budget_s: float,
        allow_tool_synthesis: bool,
    ) -> BranchBudget:
        reservation = BranchBudget(
            branch_id=branch_id,
            model_call_budget=model_call_budget,
            checker_budget=checker_budget,
            latency_budget_s=latency_budget_s,
            allow_tool_synthesis=allow_tool_synthesis,
        )
        self.branch_reservations[branch_id] = reservation
        return reservation
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
@dataclass
class RuntimeState:
    queue: list[AgentFrame] = field(default_factory=list)
    visible_tool_names: list[str] = field(default_factory=list)
    unresolved_goals: list[str] = field(default_factory=list)
    confidence: float = 0.0
    mode: str | None = None
    created_tools: int = 0
    promoted_nodes: int = 0
    checks_used: int = 0
    interface_usage: dict[str, float] = field(default_factory=lambda: {"top": 0.0, "mem": 0.0, "tool": 0.0, "ctl": 0.0})
    artifacts: dict[str, Any] = field(default_factory=dict)
    checkpoints: dict[str, Checkpoint] = field(default_factory=dict)
    worker_plans: dict[str, dict[str, Any]] = field(default_factory=dict)
    open_handle_ids: list[str] = field(default_factory=list)
    subgoal_negative_steps: dict[str, int] = field(default_factory=dict)
    subgoal_last_model: dict[str, str] = field(default_factory=dict)
    last_unresolved_goal: str | None = None
=======
@dataclass
class RuntimeState:
    queue: list[AgentFrame] = field(default_factory=list)
    visible_tool_names: list[str] = field(default_factory=list)
    unresolved_goals: list[str] = field(default_factory=list)
    confidence: float = 0.0
    mode: str | None = None
    created_tools: int = 0
    promoted_nodes: int = 0
    checks_used: int = 0
    interface_usage: dict[str, float] = field(default_factory=lambda: {"top": 0.0, "mem": 0.0, "tool": 0.0, "ctl": 0.0})
    artifacts: dict[str, Any] = field(default_factory=dict)
    checkpoints: dict[str, CheckpointEnvelope] = field(default_factory=dict)
    open_handle_ids: list[str] = field(default_factory=list)
    subgoal_negative_steps: dict[str, int] = field(default_factory=dict)
    subgoal_last_model: dict[str, str] = field(default_factory=dict)
    last_unresolved_goal: str | None = None
    lifecycle_state: str = "idle"
    active_plan: ExecutionPlan | None = None
    node_status: dict[str, str] = field(default_factory=dict)
    branch_state: dict[str, BranchState] = field(default_factory=dict)
    accepted_publications: dict[str, BranchPublication] = field(default_factory=dict)
    publication_history: list[BranchPublication] = field(default_factory=list)
    side_effect_receipts: dict[str, SideEffectReceipt] = field(default_factory=dict)
    runtime_events: list[RuntimeEvent] = field(default_factory=list)


@dataclass
class PolicyContext:
    runtime_dir: Path
    shell: Any
    request_id: str
    task: BenchmarkTask | None
    plan: ExecutionPlan
    provider: ModelProvider
    seed: int
    state: RuntimeState
    budget: RuntimeBudget
    trace: list[dict[str, Any]]
    objective: str
    trace_context: OpenAITraceContext = field(default_factory=OpenAITraceContext)
    profile: RuntimeProfile | None = None

    def __post_init__(self) -> None:
        if self.profile is None:
            self.profile = default_runtime_profile()

    def emit_event(self, event_kind: str, *, node_id: str | None = None, branch_id: str | None = None, **payload: Any) -> None:
        event = RuntimeEvent(
            event_id=f"{self.plan.plan_id}.{len(self.state.runtime_events):04d}",
            event_kind=event_kind,
            runtime_state=self.state.lifecycle_state,
            plan_id=self.plan.plan_id,
            request_id=self.request_id,
            trace_context=self.trace_context,
            node_id=node_id,
            branch_id=branch_id,
            payload=dict(payload),
            created_at=time.time(),
        )
        self.state.runtime_events.append(event)
        self.trace.append(model_dump(event))
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
def benchmark_task_to_solve_request(task: BenchmarkTask, *, request_id: str | None = None) -> SolveRequest:
    verification_preference = "verified_if_available"
    if task.allow_best_effort:
        verification_preference = "best_effort"
    elif task.verification_required:
        verification_preference = "required"
    return SolveRequest(
        request_id=request_id or f"benchmark.{task.task_id}",
        prompt=task.prompt,
        context_items=[dict(item) for item in task.context_items],
        file_paths=list(task.file_paths),
        output_schema={},
        allowed_tool_categories=list(task.allowed_tool_categories),
        verification_preference=verification_preference,
        budget_overrides={},
    )
=======
def normalized_benchmark_request_id(task_id: str, seed: int) -> str:
    return f"benchmark.{task_id}.seed_{seed}"


def build_runtime_trace_context(
    *,
    provider_role: str,
    runtime_dir: str,
    runtime_hash: str,
    request_id: str,
    objective: str,
    task_id: str | None = None,
    seed: int | None = None,
) -> OpenAITraceContext:
    return OpenAITraceContext(
        provider_role=provider_role,
        runtime_dir=runtime_dir,
        runtime_hash=runtime_hash,
        request_id=request_id,
        objective=objective,
        task_id=task_id,
        seed=seed,
    )


def benchmark_task_to_solve_request(
    task: BenchmarkTask,
    *,
    request_id: str,
    trace_context: OpenAITraceContext,
) -> SolveRequest:
    verification_preference = "verified_if_available"
    if task.allow_best_effort:
        verification_preference = "best_effort"
    elif task.verification_required:
        verification_preference = "required"
    return SolveRequest(
        request_id=request_id,
        prompt=task.prompt,
        context_items=[dict(item) for item in task.context_items],
        file_paths=list(task.file_paths),
        output_schema={},
        allowed_tool_categories=list(task.allowed_tool_categories),
        verification_preference=verification_preference,
        budget_overrides={},
        trace_context=trace_context,
    )


def compile_benchmark_execution_plan(
    task: BenchmarkTask,
    *,
    request_id: str,
    seed: int,
    source_suite: str | None,
    trace_context: OpenAITraceContext,
    runtime_profile: RuntimeProfile,
) -> ExecutionPlan:
    nodes: list[PlanNode] = []
    for index, op in enumerate(task.operations):
        branch_group_id = f"group.{task.task_id}.{index:02d}" if not op.dependencies else None
        nodes.append(
            PlanNode(
                node_id=op.op_id,
                node_kind={
                    "builtin": "builtin_op",
                    "memory_lookup": "memory_lookup",
                    "generated_expression": "tool_synthesis",
                    "direct_response": "direct_response",
                }.get(op.kind, "tool_call"),
                instruction=op.description,
                output_key=op.output_key,
                dependencies=list(op.dependencies),
                tool_hint=op.tool_hint,
                allowed_tool_categories=list(task.allowed_tool_categories),
                static_args=dict(op.args),
                input_bindings=[],
                verification_required=task.verification_required or op.externally_visible,
                externally_visible=bool(task.externally_visible or op.externally_visible),
                frame_role="worker",
                branch_group_id=branch_group_id,
                metadata={"source_op_kind": op.kind, "source_op_id": op.op_id},
            )
        )
    plan = ExecutionPlan(
        plan_schema_version="execution-plan-v1",
        plan_digest=stable_hash(task.task_id, request_id, nodes),
        plan_id=f"plan.{stable_hash(task.task_id, request_id, seed)[:16]}",
        request_id=request_id,
        origin=PlanOrigin(
            origin_kind="benchmark",
            source_task_id=task.task_id,
            source_request_id=request_id,
            source_suite=source_suite,
            adapter_kind="benchmark_task_v1",
            adaptation_assumptions=[],
        ),
        objective=task.prompt,
        context_refs=[item.get("symbol", item.get("file_path", stable_hash(item))) for item in task.context_items],
        file_refs=list(task.file_paths),
        nodes=nodes,
        root_node_ids=[node.node_id for node in nodes if not node.dependencies],
        terminal_output_keys=[op.output_key for op in task.operations],
        verification_plan=VerificationPlan(
            mode="benchmark" if task.verification_required else "best_effort",
            required=task.verification_required,
            checker_ladder=["local", "subtree", "repo", "benchmark"] if task.verification_required else ["local"],
            exact_verifier_required=task.verification_required,
            artifact_contract={"verifier_type": task.verifier_type},
            terminal_nodes=[node.node_id for node in nodes if node.output_key in {op.output_key for op in task.operations}],
        ),
        execution_flags=ExecutionFlags(
            allow_best_effort=task.allow_best_effort,
            allow_resume=True,
            allow_branching=True,
            allow_tool_synthesis=True,
            allow_async_handles=True,
            requires_terminal_verification=task.verification_required,
        ),
        allowed_tool_categories=list(task.allowed_tool_categories),
        budget_overrides={},
        externally_visible=task.externally_visible,
        trace_context=trace_context,
    )
    return validate_execution_plan(plan, runtime_profile=runtime_profile)
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
def solve_request_to_task(request: SolveRequest) -> BenchmarkTask:
    prompt = request.prompt
    prompt_lower = prompt.lower()
    request_meta = {
        "request_id": request.request_id,
        "solve_mode": "user_request",
        "output_schema": request.output_schema,
        "allowed_tool_categories": list(request.allowed_tool_categories),
    }
=======
def compile_solve_request_execution_plan(
    request: SolveRequest,
    *,
    runtime_profile: RuntimeProfile,
) -> ExecutionPlan:
    prompt = request.prompt
    prompt_lower = prompt.lower()
    if _parse_number_list(prompt) and any(token in prompt_lower for token in ("sum", "product", "min", "max", "median", "square")):
        task = solve_request_to_task(request)
        return compile_benchmark_execution_plan(
            task,
            request_id=request.request_id,
            seed=int(request.trace_context.seed or 0),
            source_suite=None,
            trace_context=request.trace_context,
            runtime_profile=runtime_profile,
        )

    if request.file_paths:
        nodes = [
            PlanNode(
                node_id="inspect_files",
                node_kind="tool_call",
                instruction="Inspect the requested files within the allowed runtime workspace.",
                output_key="inspection",
                dependencies=[],
                tool_hint="filesystem/read",
                allowed_tool_categories=list(request.allowed_tool_categories),
                static_args={"file_paths": list(request.file_paths)},
                input_bindings=[],
                verification_required=False,
                externally_visible=False,
                frame_role="worker",
                metadata={"template": "file_inspection"},
            ),
            PlanNode(
                node_id="respond",
                node_kind="direct_response",
                instruction="Produce the final bounded response from inspected files and request context.",
                output_key="response",
                dependencies=["inspect_files"],
                allowed_tool_categories=list(request.allowed_tool_categories),
                static_args={"output_schema": dict(request.output_schema)},
                input_bindings=[InputBinding(target_arg="inspection", source_kind="upstream_output", source_ref="inspection", required=True)],
                verification_required=False,
                externally_visible=True,
                frame_role="root",
                metadata={"template": "file_inspection"},
            ),
        ]
    else:
        nodes = [
            PlanNode(
                node_id="respond",
                node_kind="direct_response",
                instruction="Produce the strongest bounded answer allowed by the request and runtime profile.",
                output_key="response",
                dependencies=[],
                allowed_tool_categories=list(request.allowed_tool_categories),
                static_args={"output_schema": dict(request.output_schema)},
                input_bindings=[],
                verification_required=False,
                externally_visible=True,
                frame_role="root",
                metadata={"template": "direct_answer"},
            )
        ]

    plan = ExecutionPlan(
        plan_schema_version="execution-plan-v1",
        plan_digest=stable_hash(request.request_id, request.prompt, nodes),
        plan_id=f"plan.{stable_hash(request.request_id, request.prompt)[:16]}",
        request_id=request.request_id,
        origin=PlanOrigin(
            origin_kind="user_request",
            source_request_id=request.request_id,
            adapter_kind="bounded_request_templates_v1",
            adaptation_assumptions=[],
        ),
        objective=request.prompt,
        context_refs=[stable_hash(item) for item in request.context_items],
        file_refs=list(request.file_paths),
        nodes=nodes,
        root_node_ids=[node.node_id for node in nodes if not node.dependencies],
        terminal_output_keys=["response"],
        verification_plan=VerificationPlan(
            mode=request.verification_preference,
            required=request.verification_preference == "required",
            checker_ladder=["local"],
            exact_verifier_required=False,
            artifact_contract=dict(request.output_schema),
            terminal_nodes=["respond"],
        ),
        execution_flags=ExecutionFlags(
            allow_best_effort=request.verification_preference != "required",
            allow_resume=True,
            allow_branching=True,
            allow_tool_synthesis=True,
            allow_async_handles=True,
            requires_terminal_verification=request.verification_preference == "required",
        ),
        allowed_tool_categories=list(request.allowed_tool_categories),
        budget_overrides=dict(request.budget_overrides),
        externally_visible=True,
        trace_context=request.trace_context,
    )
    return validate_execution_plan(plan, runtime_profile=runtime_profile)
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
def runtime_solve_request_for_task(
    *,
    request_id: str,
    runtime_backend: str,
    seed: int,
    task: BenchmarkTask,
    budget_overrides: dict[str, Any] | None = None,
) -> RuntimeSolveRequest:
    return RuntimeSolveRequest(
        request_id=request_id,
        runtime_backend=runtime_backend,
        mode="benchmark",
        seed=int(seed),
        task=task,
        budget_overrides=dict(budget_overrides or {}),
    )
=======
def runtime_solve_request_for_task(
    *,
    request_id: str,
    runtime_backend: str,
    seed: int,
    task: BenchmarkTask,
    budget_overrides: dict[str, Any] | None = None,
) -> RuntimeSolveRequest:
    normalized_request_id = normalized_benchmark_request_id(task.task_id, seed)
    trace_context = OpenAITraceContext(
        provider_role="runtime",
        request_id=normalized_request_id,
        task_id=task.task_id,
        seed=int(seed),
        objective=task.prompt,
    )
    return RuntimeSolveRequest(
        request_id=normalized_request_id,
        runtime_backend=runtime_backend,
        mode="benchmark",
        seed=int(seed),
        task=task,
        budget_overrides=dict(budget_overrides or {}),
        trace_context=trace_context,
    )


def runtime_solve_request_for_user_request(
    *,
    runtime_backend: str,
    seed: int,
    solve_request: SolveRequest,
) -> RuntimeSolveRequest:
    trace_context = solve_request.trace_context or OpenAITraceContext()
    trace_context.request_id = solve_request.request_id
    trace_context.seed = int(seed)
    trace_context.provider_role = "runtime"
    trace_context.objective = solve_request.prompt
    solve_request.trace_context = trace_context
    return RuntimeSolveRequest(
        request_id=solve_request.request_id,
        runtime_backend=runtime_backend,
        mode="user_request",
        seed=int(seed),
        solve_request=solve_request,
        budget_overrides=dict(solve_request.budget_overrides),
        trace_context=trace_context,
    )
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
def runtime_batch_request_for_tasks(
    *,
    request_id: str,
    runtime_backend: str,
    task_runs: list[tuple[BenchmarkTask, int]],
    budget_overrides: dict[str, Any] | None = None,
) -> RuntimeBatchRequest:
    return RuntimeBatchRequest(
        request_id=request_id,
        runtime_backend=runtime_backend,
        budget_overrides=dict(budget_overrides or {}),
        invocations=[
            RuntimeTaskInvocation(seed=int(seed), task=task)
            for task, seed in task_runs
        ],
    )
=======
def runtime_batch_request_for_tasks(
    *,
    request_id: str,
    runtime_backend: str,
    task_runs: list[tuple[BenchmarkTask, int]],
    budget_overrides: dict[str, Any] | None = None,
) -> RuntimeBatchRequest:
    invocations = []
    for task, seed in task_runs:
        invocation_request_id = normalized_benchmark_request_id(task.task_id, int(seed))
        trace_context = OpenAITraceContext(
            provider_role="runtime",
            request_id=invocation_request_id,
            task_id=task.task_id,
            seed=int(seed),
            objective=task.prompt,
        )
        invocations.append(
            RuntimeTaskInvocation(
                request_id=invocation_request_id,
                seed=int(seed),
                task=task,
                trace_context=trace_context,
            )
        )
    return RuntimeBatchRequest(
        request_id=request_id,
        runtime_backend=runtime_backend,
        budget_overrides=dict(budget_overrides or {}),
        trace_context=OpenAITraceContext(provider_role="runtime", request_id=request_id),
        invocations=invocations,
    )
>>>>>>> REPLACE
```

### `agintor/shell.py`

```text
<<<<<<< SEARCH
@dataclass
class MessageBoard:
    entries: list[dict[str, Any]] = field(default_factory=list)
    cursors: dict[str, int] = field(default_factory=dict)

    def append(self, worker_id: str, message: dict[str, Any]) -> None:
        self.entries.append({"worker_id": worker_id, **message})

    def read_since(self, worker_id: str) -> list[dict[str, Any]]:
        cursor = self.cursors.get(worker_id, 0)
        result = self.entries[cursor:]
        self.cursors[worker_id] = len(self.entries)
        return result
=======
@dataclass
class MessageBoard:
    entries: list[BranchPublication] = field(default_factory=list)
    cursors: dict[str, int] = field(default_factory=dict)

    def append_publication(self, publication: BranchPublication) -> None:
        self.entries.append(publication)

    def read_since(self, reader_id: str, *, accepted_only: bool = True) -> list[BranchPublication]:
        cursor = self.cursors.get(reader_id, 0)
        result = self.entries[cursor:]
        self.cursors[reader_id] = len(self.entries)
        if accepted_only:
            return [entry for entry in result if entry.accepted]
        return result

    def accepted_snapshot(self) -> list[BranchPublication]:
        return [entry for entry in self.entries if entry.accepted]
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
    def save_checkpoints(self, task_id: str, seed: int, checkpoints: Mapping[str, Any]) -> CheckpointReference | None:
        if not checkpoints:
            return None
        ensure_directory(self.checkpoint_dir)
        path = self.checkpoint_dir / f"{task_id.replace('/', '_')}_{seed}.json"
        payload = {
            "task_id": task_id,
            "seed": seed,
            "checkpoints": {
                key: model_dump(value) if hasattr(value, "model_dump") or hasattr(value, "dict") else value
                for key, value in checkpoints.items()
            },
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return CheckpointReference(
            ref=str(path),
            task_id=task_id,
            seed=seed,
            checkpoint_count=len(checkpoints),
        )
=======
    def save_checkpoint_envelope(self, request_id: str, envelope: CheckpointEnvelope) -> CheckpointReference:
        ensure_directory(self.checkpoint_dir / request_id)
        path = self.checkpoint_dir / request_id / f"{envelope.checkpoint_id}.json"
        path.write_text(json.dumps(model_dump(envelope), indent=2, sort_keys=True), encoding="utf-8")
        latest_path = self.checkpoint_dir / request_id / "latest.json"
        latest_path.write_text(json.dumps({"checkpoint_id": envelope.checkpoint_id, "path": str(path)}, indent=2, sort_keys=True), encoding="utf-8")
        return CheckpointReference(
            ref=str(path),
            task_id=envelope.task_id or request_id,
            seed=int(envelope.seed or 0),
            checkpoint_count=1,
        )

    def load_checkpoint_envelope(self, checkpoint_ref: CheckpointReference | str) -> CheckpointEnvelope:
        ref = checkpoint_ref.ref if isinstance(checkpoint_ref, CheckpointReference) else str(checkpoint_ref)
        return model_validate(CheckpointEnvelope, json.loads(Path(ref).read_text(encoding="utf-8")))

    def branch_snapshot(self) -> dict[str, Any]:
        return {
            "visible_tool_names": sorted(self.tool_registry.tools),
            "long_term_nodes": model_dump(self.long_term.all_nodes()),
            "accepted_publications": [model_dump(publication) for publication in self.message_board.accepted_snapshot()],
            "open_handles": self.open_handles.to_jsonable(),
        }
>>>>>>> REPLACE
```

### `agintor/runner.py`

```text
<<<<<<< SEARCH
    def run_task(self, task: BenchmarkTask, seed: int) -> RunResult:
        with self._isolated_provider_environment():
            task = model_copy(task, deep=True)
            episode_scope = None
            if task.transfer_scored:
                episode_scope = f"{getattr(task, 'episode_id', None) or task.task_id}::seed::{seed}"
            self.shell.reset_for_task(
                task.task_id,
                transfer_scored=task.transfer_scored,
                episode_id=episode_scope,
            )
            budget = RuntimeBudget(**self._runtime_budget_overrides())
            state = RuntimeState(visible_tool_names=sorted(self.shell.tool_registry.tools))
            trace: list[dict[str, Any]] = []
            context = PolicyContext(
                runtime_dir=self.runtime.runtime_dir,
                shell=self.shell,
                task=task,
                provider=self.provider,
                profile=self.runtime_profile,
                seed=seed,
                state=state,
                budget=budget,
                trace=trace,
                objective=task.prompt,
            )
=======
    def run_task(
        self,
        task: BenchmarkTask,
        seed: int,
        *,
        request_id: str | None = None,
        trace_context: OpenAITraceContext | None = None,
        source_suite: str | None = None,
    ) -> RunResult:
        normalized_request_id = request_id or normalized_benchmark_request_id(task.task_id, seed)
        root_trace = trace_context or build_runtime_trace_context(
            provider_role="runtime",
            runtime_dir=str(self.runtime.runtime_dir),
            runtime_hash=self.runtime.runtime_hash,
            request_id=normalized_request_id,
            objective=task.prompt,
            task_id=task.task_id,
            seed=seed,
        )
        plan = compile_benchmark_execution_plan(
            task,
            request_id=normalized_request_id,
            seed=seed,
            source_suite=source_suite,
            trace_context=root_trace,
            runtime_profile=self.runtime_profile,
        )
        return self._execute_plan(
            plan=plan,
            benchmark_task=task,
            seed=seed,
            request_id=normalized_request_id,
            trace_context=root_trace,
        )

    def run_solve_request(self, solve_request: SolveRequest, seed: int) -> RunResult:
        plan = compile_solve_request_execution_plan(solve_request, runtime_profile=self.runtime_profile)
        return self._execute_plan(
            plan=plan,
            benchmark_task=None,
            seed=seed,
            request_id=solve_request.request_id,
            trace_context=solve_request.trace_context,
        )

    def resume(self, request: ResumeRequest) -> RunResult:
        envelope = self.shell.load_checkpoint_envelope(request.checkpoint_ref)
        return self._resume_from_checkpoint(envelope, request)
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
    def _run_root_frame(
        self,
        context: PolicyContext,
        frame: AgentFrame,
        task: BenchmarkTask,
        verifier_score: float,
        verified_terminal: bool,
    ) -> tuple[Any, int, float, bool]:
        faults = 0
        artifact: Any = None
        mode = self.runtime.topology.select_mode(context, frame, task.operations)
        context.state.mode = mode
        context.record("mode_selected", mode=mode)
=======
    def _execute_plan(
        self,
        *,
        plan: ExecutionPlan,
        benchmark_task: BenchmarkTask | None,
        seed: int,
        request_id: str,
        trace_context: OpenAITraceContext,
    ) -> RunResult:
        with self._isolated_provider_environment():
            task_id = benchmark_task.task_id if benchmark_task is not None else request_id
            transfer_scored = bool(benchmark_task.transfer_scored) if benchmark_task is not None else False
            episode_scope = f"{task_id}::seed::{seed}" if transfer_scored else None
            self.shell.reset_for_task(task_id, transfer_scored=transfer_scored, episode_id=episode_scope)
            budget = RuntimeBudget(**self._runtime_budget_overrides())
            state = RuntimeState(
                visible_tool_names=sorted(self.shell.tool_registry.tools),
                lifecycle_state="idle",
                active_plan=plan,
            )
            trace: list[dict[str, Any]] = []
            context = PolicyContext(
                runtime_dir=self.runtime.runtime_dir,
                shell=self.shell,
                request_id=request_id,
                task=benchmark_task,
                plan=plan,
                provider=self.provider,
                profile=self.runtime_profile,
                seed=seed,
                state=state,
                budget=budget,
                trace=trace,
                objective=plan.objective,
                trace_context=trace_context,
            )
            return self._run_plan_state_machine(context)

    def _run_plan_state_machine(self, context: PolicyContext) -> RunResult:
        context.state.lifecycle_state = "compiling"
        context.emit_event("plan_compiled", plan_digest=context.plan.plan_digest)
        context.state.lifecycle_state = "validating"
        self._validate_plan_runtime_contract(context)
        context.emit_event("plan_loaded", node_count=len(context.plan.nodes))
        context.state.lifecycle_state = "running"
        root_frame = self._make_root_frame(context)
        context.state.queue.append(root_frame)

        artifact: Any = None
        verifier_score = 0.0
        faults = 0
        start = time.perf_counter()

        try:
            while context.state.queue:
                frame = context.state.queue.pop(0)
                runnable_nodes = self._resolve_runnable_nodes(context, frame)
                if not runnable_nodes:
                    continue
                mode = self.runtime.topology.select_mode(context, frame, context.plan, runnable_nodes)
                context.state.mode = mode
                if mode == "horizontal" and self._has_branch_group(runnable_nodes):
                    context.state.lifecycle_state = "branching"
                    branch_results, branch_faults = self._run_branch_group(context, frame, runnable_nodes)
                    faults += branch_faults
                    context.state.lifecycle_state = "merging"
                    artifact, verifier_score = self._merge_branch_group(context, frame, branch_results)
                    context.state.lifecycle_state = "running"
                    continue
                if mode == "vertical":
                    self._enqueue_plan_children(context, frame, runnable_nodes)
                    continue
                artifact, local_faults = self._execute_nodes(context, frame, runnable_nodes)
                faults += local_faults

            context.state.lifecycle_state = "completing"
            artifact, verifier_score = self._finalize_plan_artifact(context, artifact, verifier_score)
            context.state.lifecycle_state = "completed"
            context.emit_event("terminal_emitted", artifact_keys=sorted(context.state.artifacts))
            return self._build_run_result(
                context.task.task_id if context.task is not None else context.request_id,
                seed=context.seed,
                request_id=context.request_id,
                plan_id=context.plan.plan_id,
                artifact=artifact,
                verifier_score=verifier_score,
                faults=faults,
                start=start,
                budget=context.budget,
                state=context.state,
                trace=context.trace,
                hard_invalid=False,
                invalid_reason=None,
                failure_class=None,
            )
        except HardInvalidation as exc:
            context.state.lifecycle_state = "failed"
            context.emit_event("run_failed", failure_class="controlled_failure", reason=str(exc))
            return self._build_run_result(
                context.task.task_id if context.task is not None else context.request_id,
                seed=context.seed,
                request_id=context.request_id,
                plan_id=context.plan.plan_id,
                artifact={"error": str(exc)},
                verifier_score=0.0,
                faults=faults,
                start=start,
                budget=context.budget,
                state=context.state,
                trace=context.trace,
                hard_invalid=True,
                invalid_reason=str(exc),
                failure_class="controlled_failure",
            )
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
        workers = self.runtime.topology.select_workers(context, frame, task.operations)
        worker_outputs = []
        for worker in workers:
            op_order = worker["op_ids"]
            worker_frame = AgentFrame(
                agent=self.shell.agent_pool.clone(worker.get("agent_id", "root")),
                objective=worker["instruction"],
                operation_ids=op_order,
                depth=frame.depth + 1,
                role="worker",
                worker_id=worker["worker_id"],
                tool_scope=worker.get("tool_scope", context.state.visible_tool_names),
                model_class=worker.get("model_class", "small"),
                metadata={**worker, "parent_run_node_id": frame.metadata.get("run_node_id")},
            )
=======
        branch_candidates = self._build_branch_plans(context, frame, runnable_nodes)
        selected_branches = self.runtime.topology.select_workers(context, frame, branch_candidates)
        cancel_event = threading.Event()
        branch_results: list[BranchResult] = []
        branch_faults = 0

        self._publish_checkpoint(context, frame, boundary="before_branch_fanout")
        with ThreadPoolExecutor(max_workers=len(selected_branches), thread_name_prefix=f"branch-{context.plan.plan_id[:8]}") as executor:
            future_map = {
                executor.submit(self._run_branch_plan, context, frame, branch_plan, cancel_event): branch_plan
                for branch_plan in selected_branches
            }
            done, pending = wait(list(future_map), return_when=FIRST_EXCEPTION)
            failing_future = next((future for future in done if future.exception() is not None), None)
            if failing_future is not None:
                cancel_event.set()
                for pending_future in pending:
                    pending_future.cancel()
                executor.shutdown(wait=True, cancel_futures=True)
            for future, branch_plan in future_map.items():
                if future.cancelled():
                    branch_results.append(
                        BranchResult(
                            branch_id=branch_plan.branch_id,
                            status="cancelled",
                            cancellation_record=CancellationRecord(
                                reason="fatal_branch_fault" if failing_future is not None else "parent_stop_policy",
                                requested_at=time.time(),
                                requested_by="parent",
                                cleanup_completed=True,
                            ),
                        )
                    )
                    continue
                try:
                    branch_results.append(future.result())
                except Exception as exc:
                    branch_faults += 1
                    branch_results.append(
                        BranchResult(
                            branch_id=branch_plan.branch_id,
                            status="failed",
                            error=str(exc),
                            cancellation_record=CancellationRecord(
                                reason="fatal_branch_fault",
                                requested_at=time.time(),
                                requested_by="parent",
                                details={"exception": str(exc)},
                                cleanup_completed=False,
                            ),
                        )
                    )
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
    def _execute_operations(self, context: PolicyContext, frame: AgentFrame, operations: Sequence[Any]) -> tuple[Any, int]:
        task = context.task
        results: dict[str, Any] = {}
        faults = 0
        run_node_id = frame.metadata.get("run_node_id")
        for operation in operations:
            event_id = self.shell.short_term.add_node("Event", operation.op_id, {"kind": operation.kind, "description": operation.description})
=======
    def _execute_nodes(self, context: PolicyContext, frame: AgentFrame, nodes: Sequence[PlanNode]) -> tuple[Any, int]:
        results: dict[str, Any] = {}
        faults = 0
        run_node_id = frame.metadata.get("run_node_id")
        for node in nodes:
            context.emit_event("node_started", node_id=node.node_id, frame_id=frame.frame_id, node_kind=node.node_kind)
            event_id = self.shell.short_term.add_node("Event", node.node_id, {"kind": node.node_kind, "instruction": node.instruction})
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
    def _execute_direct_response(
        self,
        context: PolicyContext,
        operation: Any,
        resolved_args: Mapping[str, Any],
        model_class: str,
    ) -> Any:
        output_schema = resolved_args.get("output_schema", {})
=======
    def _execute_direct_response(
        self,
        context: PolicyContext,
        node: PlanNode,
        resolved_args: Mapping[str, Any],
        model_class: str,
        trace_context: OpenAITraceContext,
    ) -> Any:
        output_schema = resolved_args.get("output_schema", {})
        request_receipt = self._record_side_effect(
            context,
            action_kind="provider_request",
            branch_id=trace_context.worker_id,
            trace_context=trace_context,
            action_fingerprint=stable_hash(node.node_id, resolved_args, model_class),
        )
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
        response = context.provider.generate(
            ModelRequest(
                instructions="Return the strongest bounded answer you can for the request. Use JSON only when an output schema is provided.",
                prompt="\n".join(prompt_lines),
                model_class=model_class,
                seed=context.seed,
                metadata={
                    "mode": "user_request",
                    "payload": {
                        "prompt": context.task.prompt,
                        "output_schema": output_schema,
                    },
                },
            )
        )
        context.consume_model_response(response, purpose="user_request")
=======
        response = context.provider.generate(
            ModelRequest(
                instructions="Return the strongest bounded answer you can for the request. Use JSON only when an output schema is provided.",
                prompt="\n".join(prompt_lines),
                model_class=model_class,
                seed=context.seed,
                metadata={
                    "mode": "user_request",
                    "trace_context": model_dump(trace_context),
                    "payload": {
                        "prompt": context.plan.objective,
                        "output_schema": output_schema,
                        "plan_id": context.plan.plan_id,
                        "node_id": node.node_id,
                    },
                },
            )
        )
        self._complete_side_effect(context, request_receipt, status="completed", result_ref=f"provider:{node.node_id}")
        context.consume_model_response(response, purpose="user_request")
>>>>>>> REPLACE
```

### `agintor/templates/baseline_runtime/topology_policy.py`

```text
<<<<<<< SEARCH
    def select_mode(self, ctx, frame, operations: Sequence[Any]) -> str:
        config = ctx.profile.topology
        op_count = len(operations)
        dependency_count = sum(len(op.dependencies) for op in operations)
        generated_count = sum(1 for op in operations if op.kind == "generated_expression")
        exact_verifier_hint = 1.0 if ctx.task.verification_required else 0.0
        context_saturation = min(1.0, len(ctx.shell.short_term.nodes) / 20.0)
        candidate_utilities = {}
        for mode in ("single", "vertical", "horizontal"):
=======
    def select_mode(self, ctx, frame, plan, runnable_nodes: Sequence[Any]) -> str:
        config = ctx.profile.topology
        node_count = len(runnable_nodes)
        dependency_count = sum(len(node.dependencies) for node in runnable_nodes)
        branchable = [node for node in runnable_nodes if getattr(node, "branch_group_id", None)]
        exact_verifier_hint = 1.0 if plan.verification_plan.required else 0.0
        context_saturation = min(1.0, len(ctx.shell.short_term.nodes) / 20.0)
        candidate_utilities = {}
        for mode in ("single", "vertical", "horizontal"):
            if mode == "single":
                solve = 0.58 + 0.12 * (node_count == 1) + 0.06 * (not branchable)
                cost = 0.10 + 0.05 * node_count
                latency = 0.10 + 0.04 * node_count
                coordination = 0.02
            elif mode == "vertical":
                solve = 0.62 + 0.05 * min(3, node_count) + 0.04 * dependency_count + 0.05 * exact_verifier_hint
                cost = 0.16 + 0.04 * node_count
                latency = 0.14 + 0.03 * node_count
                coordination = 0.03 * node_count + 0.03 * context_saturation
            else:
                solve = 0.48 + 0.12 * min(config.k_max, len(branchable))
                cost = 0.20 + 0.06 * min(config.k_max, len(branchable))
                latency = 0.14 + 0.02 * node_count
                coordination = 0.05 * len(branchable) + 0.04 * dependency_count
            candidate_utilities[mode] = solve - 0.25 * cost - 0.18 * latency - 0.18 * coordination
        if len(branchable) >= 2:
            return max(candidate_utilities, key=candidate_utilities.get)
        if node_count <= 1:
            return "single"
        return max(("single", "vertical"), key=lambda mode: candidate_utilities[mode])
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
    def select_workers(self, ctx, frame, operations: Sequence[Any]) -> list[dict[str, Any]]:
        config = ctx.profile.topology
        op_ids = [op.op_id for op in operations]
        candidates = [
            {
                "worker_id": "w0",
                "instruction": "Sequential canonical plan",
                "op_ids": op_ids,
                "predicted_solve": 0.62,
                "tool_scope": ctx.state.visible_tool_names,
                "agent_id": "root",
            },
=======
    def select_workers(self, ctx, frame, branch_plans: Sequence[Any]) -> list[Any]:
        config = ctx.profile.topology
        ranked = sorted(
            branch_plans,
            key=lambda branch: (
                -len(branch.assigned_node_ids),
                branch.merge_priority,
                branch.branch_id,
            ),
        )
        selected = []
        for index, branch in enumerate(ranked):
            if index >= config.k_max:
                break
            branch.merge_priority = index
            selected.append(branch)
        return selected
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
    def merge_ensemble(self, ctx, worker_outputs: Sequence[dict[str, Any]]) -> Any:
        ordered = sorted(
            worker_outputs,
            key=lambda item: (
                0 if item.get("verifier_support", 0.0) >= 1.0 else 1,
                -item.get("verifier_support", 0.0),
                -item.get("predicted_solve", 0.0),
                item.get("unresolved_critical", 0),
                item.get("worker_id", ""),
            ),
        )
        return ordered[0]["artifact"] if ordered else {}
=======
    def merge_ensemble(self, ctx, publications: Sequence[Any]) -> Any:
        ordered = sorted(
            publications,
            key=lambda publication: (
                publication.payload.get("merge_priority", 0),
                -publication.verifier_support,
                publication.unresolved_critical_count,
                publication.payload.get("branch_rank", 0),
                publication.branch_id,
                publication.sequence_no,
            ),
        )
        for publication in ordered:
            if publication.publication_kind == "candidate_artifact":
                return publication.payload.get("artifact", {})
        return {}
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
    def make_checkpoint(self, ctx, frame, artifacts, unresolved, open_handles) -> Checkpoint:
        summary = SummaryRecord(
            objective=frame.objective,
            evidence=[f"completed_ops={frame.operation_ids}"],
            artifacts=list(artifacts.keys()),
            unresolved=list(unresolved),
            open_handles=list(open_handles),
            next_actions=["resume" if unresolved else "stop"],
            symbols=ctx.task.symbolic_seeds,
            verifier_state={"verified": len(unresolved) == 0},
            provenance={"agent_id": frame.agent.agent_id, "role": frame.role},
        )
        return Checkpoint(
            summary=summary,
            artifact_refs=list(artifacts.keys()),
            open_handles=list(open_handles),
            unresolved_goals=list(unresolved),
            budget_state=ctx.budget.normalized(),
            verifier_state={"verified": len(unresolved) == 0},
            resume_constraints={"tool_scope": frame.tool_scope, "model_class": frame.model_class},
        )
=======
    def make_checkpoint(self, ctx, frame, artifacts, unresolved, open_handles) -> CheckpointEnvelope:
        return CheckpointEnvelope(
            checkpoint_id=f"checkpoint.{ctx.plan.plan_id}.{len(ctx.state.checkpoints):04d}",
            runtime_abi="agintor-runtime-abi-v4",
            storage_schema_version="agintor-storage-v2",
            runtime_hash=ctx.trace_context.runtime_hash or "",
            request_id=ctx.request_id,
            plan_id=ctx.plan.plan_id,
            task_id=ctx.task.task_id if ctx.task is not None else None,
            seed=ctx.seed,
            queued_frames=[model_dump(queued_frame) for queued_frame in ctx.state.queue],
            branch_state=ctx.state.branch_state,
            branch_publications=ctx.state.publication_history,
            unresolved_goals=list(unresolved),
            artifact_refs=dict(artifacts),
            handle_refs=list(open_handles),
            budget_state=ctx.budget.normalized(),
            verifier_state={"required": ctx.plan.verification_plan.required},
            working_state_summary={"mode": ctx.state.mode, "confidence": ctx.state.confidence},
            trace_cursor={"event_count": len(ctx.state.runtime_events)},
            side_effect_receipts=list(ctx.state.side_effect_receipts.values()),
        )
>>>>>>> REPLACE
```

### `agintor/runtime_host.py`

```text
<<<<<<< SEARCH
from .runtime_api import inspect_request_for_runtime, runtime_batch_request_for_tasks, solve_request_to_task
=======
from .runtime_api import inspect_request_for_runtime, runtime_batch_request_for_tasks
from .schemas import ResumeRequest
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
    def solve(
        self,
        runtime_dir: str | Path,
        request: RuntimeSolveRequest,
        *,
        provider: ModelProvider,
        runtime_profile: object | None = None,
    ) -> RuntimeSolveResponse:
=======
    def solve(
        self,
        runtime_dir: str | Path,
        request: RuntimeSolveRequest,
        *,
        provider: ModelProvider,
        runtime_profile: object | None = None,
    ) -> RuntimeSolveResponse:
        capability_exchange = self.inspect(runtime_dir)
        self._preflight_solve_contract(runtime_dir, capability_exchange, request, provider=provider, runtime_profile=runtime_profile)
        self._ensure_isolation_contract(capability_exchange)
        ...

    def resume(
        self,
        runtime_dir: str | Path,
        request: ResumeRequest,
        *,
        provider: ModelProvider,
        runtime_profile: object | None = None,
    ) -> RuntimeSolveResponse:
        capability_exchange = self.inspect(runtime_dir)
        self._ensure_isolation_contract(capability_exchange)
        if self.runtime_backend == "docker" and self.container_executor is not None:
            response = self.container_executor.resume_protocol(runtime_dir, request, provider=provider, runtime_profile=runtime_profile)
        else:
            response = self._run_local_resume(Path(runtime_dir), request, provider=provider, runtime_profile=runtime_profile)
        if response.capability_exchange != capability_exchange:
            raise RuntimeLoadError("runtime capability exchange changed between inspect and resume")
        return response
>>>>>>> REPLACE
```

### `agintor/runtime_sdk/runtime_entry.py`

```text
<<<<<<< SEARCH
from .runtime_api import (
    benchmark_task_to_solve_request,
    runtime_solve_failure_response,
    solve_request_to_task,
    solve_result_from_run_result_with_context,
)
=======
from .runtime_api import (
    benchmark_task_to_solve_request,
    runtime_solve_failure_response,
    solve_result_from_run_result_with_context,
)
from .schemas import ResumeRequest
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
        task = model_validate(BenchmarkTask, model_dump(request.task))
        solve_request = benchmark_task_to_solve_request(task, request_id=request.request_id)
    else:
        if request.solve_request is None:
            raise ValueError("user_request solve requires a solve_request payload")
        solve_request = model_validate(SolveRequest, model_dump(request.solve_request))
        task = solve_request_to_task(solve_request)
=======
        task = model_validate(BenchmarkTask, model_dump(request.task))
        solve_request = benchmark_task_to_solve_request(
            task,
            request_id=request.request_id,
            trace_context=request.trace_context,
        )
    else:
        if request.solve_request is None:
            raise ValueError("user_request solve requires a solve_request payload")
        solve_request = model_validate(SolveRequest, model_dump(request.solve_request))
        solve_request.trace_context = request.trace_context or solve_request.trace_context
        task = None
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
        run_result = runner.run_task(task, request.seed)
=======
        if request.mode == "benchmark":
            run_result = runner.run_task(task, request.seed, request_id=request.request_id, trace_context=request.trace_context)
        else:
            run_result = runner.run_solve_request(solve_request, request.seed)
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
    solve_parser = subparsers.add_parser("solve")
    solve_parser.add_argument("--runtime-dir", required=True)
    solve_parser.add_argument("--input-json", required=True)
    solve_parser.add_argument("--provider-json", required=True)
    solve_parser.add_argument("--profile-json")
    solve_parser.add_argument("--workspace", required=True)
    solve_parser.add_argument("--artifact-mode", default=ArtifactMode.NONE.value)
    solve_parser.add_argument("--output-json", required=True)
=======
    solve_parser = subparsers.add_parser("solve")
    solve_parser.add_argument("--runtime-dir", required=True)
    solve_parser.add_argument("--input-json", required=True)
    solve_parser.add_argument("--provider-json", required=True)
    solve_parser.add_argument("--profile-json")
    solve_parser.add_argument("--workspace", required=True)
    solve_parser.add_argument("--artifact-mode", default=ArtifactMode.NONE.value)
    solve_parser.add_argument("--output-json", required=True)

    resume_parser = subparsers.add_parser("resume")
    resume_parser.add_argument("--runtime-dir", required=True)
    resume_parser.add_argument("--input-json", required=True)
    resume_parser.add_argument("--provider-json", required=True)
    resume_parser.add_argument("--profile-json")
    resume_parser.add_argument("--workspace", required=True)
    resume_parser.add_argument("--artifact-mode", default=ArtifactMode.NONE.value)
    resume_parser.add_argument("--output-json", required=True)
>>>>>>> REPLACE
```

### `agintor/runtime_loader.py`

```text
<<<<<<< SEARCH
RUNTIME_ABI_VERSION = "agintor-runtime-abi-v3"
=======
RUNTIME_ABI_VERSION = "agintor-runtime-abi-v4"
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
    capability_exchange = CapabilityExchange(
        runtime_abi=RUNTIME_ABI_VERSION,
        kernel_version=kernel_manifest.kernel_version,
        storage_schema_version=kernel_manifest.storage_schema_version,
        supported_backends=list(deployment_contract.supported_backends),
        tool_runtimes=["python"],
        checkpoint_support=True,
        runtime_asset_capabilities={"traces": True, "checkpoints": True, "runtime_sdk": True},
        side_effect_receipts=False,
        required_env_names=list(deployment_contract.required_env_names),
        required_env_any_of=[list(group) for group in deployment_contract.required_env_any_of],
        capability_flags=list(deployment_contract.capability_flags or kernel_manifest.capability_flags),
    )
=======
    capability_exchange = CapabilityExchange(
        runtime_abi=RUNTIME_ABI_VERSION,
        kernel_version=kernel_manifest.kernel_version,
        storage_schema_version=kernel_manifest.storage_schema_version,
        supported_backends=list(deployment_contract.supported_backends),
        tool_runtimes=["python"],
        checkpoint_support=True,
        resume_support=True,
        runtime_asset_capabilities={"traces": True, "checkpoints": True, "runtime_sdk": True},
        side_effect_receipts=True,
        required_env_names=list(deployment_contract.required_env_names),
        required_env_any_of=[list(group) for group in deployment_contract.required_env_any_of],
        capability_flags=list(deployment_contract.capability_flags or kernel_manifest.capability_flags),
        isolation_report=RuntimeIsolationReport(
            backend=str(runtime_backend or "local"),
            policy=RuntimeIsolationPolicy(
                timeout_envelope={},
                workspace_root=str(runtime_path),
                environment_allowlist=list(deployment_contract.environment_allowlist),
                network_policy=deployment_contract.network_policy or "none",
                filesystem_policy=deployment_contract.filesystem_policy,
                required_guarantees=[],
                desired_guarantees=[],
            ),
            effective_guarantees={
                "timeout_enforcement": True,
                "workspace_isolation": True,
                "environment_filtering": True,
                "process_cleanup": True,
                "network_disablement": str(runtime_backend or "").strip().lower() == "docker",
            },
        ),
    )
>>>>>>> REPLACE
```

### `agintor/container_runtime.py`

```text
<<<<<<< SEARCH
class DockerRuntimeExecutor:
=======
class DockerRuntimeExecutor:
    DOCKER_GUARANTEES = {
        "timeout_enforcement": True,
        "workspace_isolation": True,
        "environment_filtering": True,
        "process_cleanup": True,
        "network_disablement": True,
    }
>>>>>>> REPLACE
```

## Notes

### Assumptions

- `solve_request_to_task()` remains temporarily available only as a bounded-template helper for structured user requests; the runtime no longer treats the returned `BenchmarkTask` as the canonical execution unit.
- Branch rank is the stable order of `BranchPlan` selection after topology scoring. That rank feeds deterministic publication ordering and merge tie-breaks.
- `ThreadPoolExecutor` is the correct MVP concurrency primitive because the current provider and tool stacks are synchronous and largely I/O-bound; process-based isolation is deferred to backend-level runtime isolation rather than per-branch executors.
- Pending branch futures are cancelled with `cancel_futures=True`; already running branches stop cooperatively by checking a shared cancellation event before node execution, before provider launch, after tool launch, and before publication.
- `merge` and `verify` plan nodes are runtime-owned structural nodes. Topology policy may select branching and agent assignments, but it may not invent new semantic work beyond compiler-emitted nodes.

### Risk Areas

- `agintor/runner.py` becomes the largest WS2 integration point. Land the schema and runtime API contract changes first, then replace the runner around those new types, not the other way around.
- Receipt-backed cancellation is only as good as the tool executor’s cleanup hooks. If a launched async tool cannot be cancelled or reconciled, the parent runtime must fail closed rather than silently continuing.
- File-backed checkpoint envelopes are same-run restart artifacts for WS2. Indexed retention, cleanup policies, and long-lived recovery UX remain Workstream 3 territory.
- The current `DeploymentContract.notes` usage is too weak for isolation policy transport. A dedicated `runtime_isolation_policy` field on the deployment contract is the cleaner final shape even if the first implementation stages it through a temporary compatibility shim.

### Implementation Order

1. Add the frozen plan, branch, checkpoint, receipt, event, and isolation schemas.
2. Add runtime API builders for normalized request IDs, trace-context derivation, deterministic plan compilation, and plan validation.
3. Upgrade the shell to persist checkpoint envelopes and accepted branch publications.
4. Rewrite the runner around the runtime state machine and concurrent branch executor.
5. Update baseline topology policy to consume runnable plan nodes and branch plans rather than raw operation lists.
6. Finish the host, entrypoint, loader, and docker isolation changes so solve, batch, and resume all flow through the same runtime-owned execution path.

### Additional Runner Result Contract

```text
<<<<<<< SEARCH
    def _build_run_result(
        self,
        task: BenchmarkTask,
        seed: int,
        artifact: Any,
        verifier_score: float,
        faults: int,
        start: float,
        budget: RuntimeBudget,
        state: RuntimeState,
        trace: list[dict[str, Any]],
        hard_invalid: bool,
        invalid_reason: str | None,
    ) -> RunResult:
=======
    def _build_run_result(
        self,
        task_id: str,
        *,
        seed: int,
        request_id: str,
        plan_id: str,
        artifact: Any,
        verifier_score: float,
        faults: int,
        start: float,
        budget: RuntimeBudget,
        state: RuntimeState,
        trace: list[dict[str, Any]],
        hard_invalid: bool,
        invalid_reason: str | None,
        failure_class: str | None,
    ) -> RunResult:
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
        return RunResult(
            task_id=task.task_id,
            seed=seed,
            artifact=artifact,
            verifier_score=verifier_score,
            cost=budget.cost,
            latency=time.perf_counter() - start,
            faults=faults,
            trace=[dict(row) for row in trace],
            trace_path=str(trace_path) if trace_path is not None else None,
            checkpoint_ref=checkpoint_ref.ref if checkpoint_ref is not None else None,
            hard_invalid=hard_invalid,
            invalid_reason=invalid_reason,
            mode=state.mode,
            created_tools=state.created_tools,
            promoted_nodes=state.promoted_nodes,
            checks_used=state.checks_used,
            model_calls=budget.calls,
            tokens_used=budget.tokens,
            input_tokens=budget.input_tokens,
            output_tokens=budget.output_tokens,
        )
=======
        return RunResult(
            task_id=task_id,
            seed=seed,
            request_id=request_id,
            plan_id=plan_id,
            artifact=artifact,
            verifier_score=verifier_score,
            cost=budget.cost,
            latency=time.perf_counter() - start,
            faults=faults,
            trace=[dict(row) for row in trace],
            trace_path=str(trace_path) if trace_path is not None else None,
            checkpoint_ref=checkpoint_ref.ref if checkpoint_ref is not None else None,
            hard_invalid=hard_invalid,
            invalid_reason=invalid_reason,
            mode=state.mode,
            created_tools=state.created_tools,
            promoted_nodes=state.promoted_nodes,
            checks_used=state.checks_used,
            model_calls=budget.calls,
            tokens_used=budget.tokens,
            input_tokens=budget.input_tokens,
            output_tokens=budget.output_tokens,
            runtime_state=state.lifecycle_state,
            failure_class=failure_class,
            accepted_publication_ids=sorted(state.accepted_publications),
        )
>>>>>>> REPLACE
```
