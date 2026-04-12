# Workstream 2 Runtime Execution and Orchestration

## Design Notes

- Replace `BenchmarkTask`-centric runtime execution with `ExecutionPlan`-centric runtime execution. `BenchmarkTask` stays a host-side and planning-side input contract. The runtime executes plans only.
- Keep the bundled runtime entrypoint and `TaskRuntime`, but route `solve`, `run-batch`, and `resume` through one state machine and one checkpoint/receipt model.
- Make `OpenAITraceContext` a real typed schema and stamp it onto request transport, execution plans, policy context, frames, branches, and runtime events. Runtime-side model/tool/provider calls derive child contexts from parents instead of assembling ad hoc metadata.
- Treat provider launches, provider completions, async tool launches, async tool completions, service actions, and filesystem writes as side effects with idempotency and reconciliation semantics. Resume never blindly reissues a side effect.
- Keep `local` as a development backend with best-effort guarantees only. Only `docker` may claim guaranteed network disablement. Host preflight and runtime recheck must both fail closed when required guarantees are missing.
- Remove the sequential fake-horizontal loop. Branch groups become structured concurrent work with reservation-based budgets, typed publications, explicit sibling cancellation, deterministic merge, and mandatory cleanup.

## agintor/schemas.py

### Notes

- `Checkpoint` is too small for WS2. It is a per-frame summary, not a restartable runtime artifact.
- Runtime transport objects are still task-shaped and missing request-level identity, trace context, invocation-level IDs, resume policy, and isolation guarantees.
- `CapabilityExchange` should report effective guarantees, supported commands, and receipt support so the host can preflight backend requirements before launch.

### Block 1

```text
<<<<<<< SEARCH
class Checkpoint(BaseModel):
    summary: SummaryRecord
    artifact_refs: List[str]
    open_handles: List[str]
    unresolved_goals: List[str]
    budget_state: Dict[str, Any]
    verifier_state: Dict[str, Any]
    resume_constraints: Dict[str, Any]


class AsyncHandle(BaseModel):
=======
class Checkpoint(BaseModel):
    summary: SummaryRecord
    artifact_refs: List[str]
    open_handles: List[str]
    unresolved_goals: List[str]
    budget_state: Dict[str, Any]
    verifier_state: Dict[str, Any]
    resume_constraints: Dict[str, Any]


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
    branch_id: Optional[str] = None
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
    requires_terminal_verification: bool = False


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


class ExecutionPlan(BaseModel):
    plan_schema_version: str = "agintor.execution-plan.v1"
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
    lifecycle_state: Literal[
        "compiled",
        "validated",
        "loaded",
        "running",
        "completed",
        "cancelled",
        "failed",
    ] = "compiled"


class BranchBudget(BaseModel):
    model_calls_max: int
    checks_max: int
    latency_max: float
    allow_tool_synthesis: bool = False


class BranchPlan(BaseModel):
    branch_id: str
    parent_frame_id: str
    request_id: str
    trace_context: OpenAITraceContext = Field(default_factory=OpenAITraceContext)
    assigned_node_ids: List[str] = Field(default_factory=list)
    merge_priority: int = 0
    reserved_budget: BranchBudget
    cancel_on_parent_stop: bool = True


class BranchPublication(BaseModel):
    publication_id: str
    publication_kind: Literal[
        "candidate_artifact",
        "verifier_evidence",
        "trace_rows",
        "budget_usage",
        "handle_refs",
        "cleanup",
        "receipt_reconciliation",
    ]
    logical_key: str
    sequence_no: int
    accepted: bool = False
    branch_id: str
    payload: Dict[str, Any] = Field(default_factory=dict)


class CancellationRecord(BaseModel):
    reason: Literal[
        "fatal_branch_fault",
        "budget_exhaustion",
        "superior_branch_dominance",
        "verification_failure",
        "parent_stop_policy",
        "external_interrupt",
    ]
    detail: str = ""
    timestamp: float = 0.0


class BranchState(BaseModel):
    branch_id: str
    status: Literal["pending", "running", "completed", "cancelled", "failed"] = "pending"
    assigned_node_ids: List[str] = Field(default_factory=list)
    publications: List[BranchPublication] = Field(default_factory=list)
    budget_consumed: Dict[str, Any] = Field(default_factory=dict)
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


class RuntimeIsolationPolicy(BaseModel):
    timeout_envelope: Dict[str, Any] = Field(default_factory=dict)
    workspace_root: str = ""
    environment_allowlist: List[str] = Field(default_factory=list)
    network_policy: str = "none"
    filesystem_policy: str = "workspace_only"
    required_guarantees: List[str] = Field(default_factory=list)
    desired_guarantees: List[str] = Field(default_factory=list)


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
    branch_state: List[BranchState] = Field(default_factory=list)
    branch_publications: List[BranchPublication] = Field(default_factory=list)
    unresolved_goals: List[str] = Field(default_factory=list)
    artifact_refs: Dict[str, Any] = Field(default_factory=dict)
    handle_refs: List[str] = Field(default_factory=list)
    budget_state: Dict[str, Any] = Field(default_factory=dict)
    verifier_state: Dict[str, Any] = Field(default_factory=dict)
    working_state_summary: Dict[str, Any] = Field(default_factory=dict)
    trace_cursor: Dict[str, Any] = Field(default_factory=dict)
    side_effect_receipts: List[SideEffectReceipt] = Field(default_factory=list)


class AsyncHandle(BaseModel):
>>>>>>> REPLACE
```

### Block 2

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
>>>>>>> REPLACE
```

### Block 3

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
    request_id: str
    plan_id: str
    task_id: Optional[str] = None
    seed: int
    artifact: Any
    verifier_score: float
    cost: float
    latency: float
    faults: int
    trace: List[Dict[str, Any]] = Field(default_factory=list)
    trace_context: OpenAITraceContext = Field(default_factory=OpenAITraceContext)
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
>>>>>>> REPLACE
```

### Block 4

```text
<<<<<<< SEARCH
class CheckpointReference(BaseModel):
    ref: str
    task_id: str
    seed: int
    checkpoint_count: int = 0


class InspectRequest(BaseModel):
=======
class CheckpointReference(BaseModel):
    ref: str
    request_id: str
    plan_id: str
    runtime_hash: str
    checkpoint_id: str
    task_id: Optional[str] = None
    seed: Optional[int] = None
    sequence_no: int = 0
    checkpoint_count: int = 0


class InspectRequest(BaseModel):
>>>>>>> REPLACE
```

### Block 5

```text
<<<<<<< SEARCH
class ResumeRequest(BaseModel):
    request_id: str
    checkpoint_ref: CheckpointReference
    prompt: Optional[str] = None


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
class ResumeRequest(BaseModel):
    request_id: str
    checkpoint_ref: Optional[CheckpointReference] = None
    trace_context: OpenAITraceContext = Field(default_factory=OpenAITraceContext)
    reconciliation_policy: Literal["strict", "best_effort"] = "strict"
    prompt: Optional[str] = None


class CapabilityExchange(BaseModel):
    runtime_abi: str
    kernel_version: str
    storage_schema_version: str
    supported_backends: List[str] = Field(default_factory=list)
    tool_runtimes: List[str] = Field(default_factory=list)
    checkpoint_support: bool = True
    runtime_asset_capabilities: Dict[str, bool] = Field(default_factory=dict)
    side_effect_receipts: bool = True
    required_env_names: List[str] = Field(default_factory=list)
    required_env_any_of: List[List[str]] = Field(default_factory=list)
    capability_flags: List[str] = Field(default_factory=list)
    supported_runtime_commands: List[str] = Field(default_factory=lambda: ["inspect", "solve", "run-batch", "resume"])
    supported_guarantees: Dict[str, bool] = Field(default_factory=dict)
    effective_guarantees: Dict[str, bool] = Field(default_factory=dict)
    runtime_isolation_policy: Optional[RuntimeIsolationPolicy] = None
>>>>>>> REPLACE
```

### Block 6

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
    transfer_episode_id: Optional[str] = None


class RuntimeBatchRequest(BaseModel):
    request_id: str
    runtime_backend: str
    budget_overrides: Dict[str, Any] = Field(default_factory=dict)
    trace_context: OpenAITraceContext = Field(default_factory=OpenAITraceContext)
    invocations: List[RuntimeTaskInvocation] = Field(default_factory=list)
>>>>>>> REPLACE
```

## agintor/runtime_api.py

### Notes

- `AgentFrame`, `RuntimeState`, and `PolicyContext` are still operation-list and `BenchmarkTask` shaped.
- Runtime-side request compilation should be deterministic and bounded. The current user-request heuristics can survive, but only as plan-template selection helpers.
- `runtime_solve_request_for_task()` should own the normalized benchmark request ID and trace-context defaults. CLI should stop hardcoding a second pattern.

### Block 7

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
    plan_node_ids: list[str]
    depth: int
    checkpoint: Checkpoint | None = None
    parent_frame_id: str | None = None
    worker_id: str | None = None
    branch_id: str | None = None
    branch_group_id: str | None = None
    role: str = "root"
    tool_scope: list[str] = field(default_factory=list)
    model_class: str = "small"
    trace_context: OpenAITraceContext = field(default_factory=OpenAITraceContext)
    metadata: dict[str, Any] = field(default_factory=dict)
>>>>>>> REPLACE
```

### Block 8

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
    lifecycle_state: str = "idle"
    created_tools: int = 0
    promoted_nodes: int = 0
    checks_used: int = 0
    interface_usage: dict[str, float] = field(default_factory=lambda: {"top": 0.0, "mem": 0.0, "tool": 0.0, "ctl": 0.0})
    artifacts: dict[str, Any] = field(default_factory=dict)
    checkpoints: dict[str, CheckpointEnvelope] = field(default_factory=dict)
    plan_node_status: dict[str, str] = field(default_factory=dict)
    branch_states: dict[str, BranchState] = field(default_factory=dict)
    branch_publications: dict[str, list[BranchPublication]] = field(default_factory=dict)
    side_effect_receipts: dict[str, SideEffectReceipt] = field(default_factory=dict)
    worker_plans: dict[str, dict[str, Any]] = field(default_factory=dict)
    open_handle_ids: list[str] = field(default_factory=list)
    subgoal_negative_steps: dict[str, int] = field(default_factory=dict)
    subgoal_last_model: dict[str, str] = field(default_factory=dict)
    last_unresolved_goal: str | None = None
>>>>>>> REPLACE
```

### Block 9

```text
<<<<<<< SEARCH
@dataclass
class PolicyContext:
    runtime_dir: Path
    shell: Any
    task: BenchmarkTask
    provider: ModelProvider
    seed: int
    state: RuntimeState
    budget: RuntimeBudget
    trace: list[dict[str, Any]]
    objective: str
    profile: RuntimeProfile | None = None
=======
@dataclass
class PolicyContext:
    runtime_dir: Path
    runtime_hash: str
    shell: Any
    task: BenchmarkTask | None
    plan: ExecutionPlan
    provider: ModelProvider
    seed: int
    request_id: str
    trace_context: OpenAITraceContext
    state: RuntimeState
    budget: RuntimeBudget
    trace: list[dict[str, Any]]
    objective: str
    profile: RuntimeProfile | None = None
>>>>>>> REPLACE
```

### Block 10

```text
<<<<<<< SEARCH
def _find_symbol_value(request: SolveRequest, symbol: str) -> str | None:
=======
def normalize_benchmark_request_id(task_id: str, seed: int) -> str:
    return f"benchmark.{task_id}.seed_{seed}"


def build_runtime_trace_context(
    *,
    request_id: str,
    provider_role: str,
    runtime_hash: str | None = None,
    runtime_dir: str | None = None,
    task_id: str | None = None,
    seed: int | None = None,
    objective: str | None = None,
    agent_id: str | None = None,
    frame_role: str | None = None,
    worker_id: str | None = None,
    branch_id: str | None = None,
    op_id: str | None = None,
    run_node_id: str | None = None,
) -> OpenAITraceContext:
    return OpenAITraceContext(
        provider_role=provider_role,
        runtime_hash=runtime_hash,
        runtime_dir=runtime_dir,
        task_id=task_id,
        seed=seed,
        request_id=request_id,
        objective=objective,
        agent_id=agent_id,
        frame_role=frame_role,
        worker_id=worker_id,
        branch_id=branch_id,
        op_id=op_id,
        run_node_id=run_node_id,
    )


def derive_child_trace_context(parent: OpenAITraceContext, **overrides: Any) -> OpenAITraceContext:
    payload = parent.model_dump()
    payload.update({key: value for key, value in overrides.items() if value is not None})
    return OpenAITraceContext(**payload)


def _find_symbol_value(request: SolveRequest, symbol: str) -> str | None:
>>>>>>> REPLACE
```

### Block 11

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
    request_id: str | None,
    runtime_backend: str,
    seed: int,
    task: BenchmarkTask,
    budget_overrides: dict[str, Any] | None = None,
    runtime_hash: str | None = None,
    runtime_dir: str | None = None,
) -> RuntimeSolveRequest:
    normalized_request_id = str(request_id or "").strip() or normalize_benchmark_request_id(task.task_id, int(seed))
    trace_context = build_runtime_trace_context(
        request_id=normalized_request_id,
        provider_role="runtime",
        runtime_hash=runtime_hash,
        runtime_dir=runtime_dir,
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
>>>>>>> REPLACE
```

### Block 12

```text
<<<<<<< SEARCH
def runtime_solve_request_for_user_request(
    *,
    runtime_backend: str,
    seed: int,
    solve_request: SolveRequest,
) -> RuntimeSolveRequest:
    return RuntimeSolveRequest(
        request_id=solve_request.request_id,
        runtime_backend=runtime_backend,
        mode="user_request",
        seed=int(seed),
        solve_request=solve_request,
        budget_overrides=dict(solve_request.budget_overrides),
    )
=======
def runtime_solve_request_for_user_request(
    *,
    runtime_backend: str,
    seed: int,
    solve_request: SolveRequest,
    runtime_hash: str | None = None,
    runtime_dir: str | None = None,
) -> RuntimeSolveRequest:
    trace_context = build_runtime_trace_context(
        request_id=solve_request.request_id,
        provider_role="runtime",
        runtime_hash=runtime_hash,
        runtime_dir=runtime_dir,
        seed=int(seed),
        objective=solve_request.prompt,
    )
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

### Block 13

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
    runtime_hash: str | None = None,
    runtime_dir: str | None = None,
) -> RuntimeBatchRequest:
    batch_trace_context = build_runtime_trace_context(
        request_id=request_id,
        provider_role="runtime",
        runtime_hash=runtime_hash,
        runtime_dir=runtime_dir,
    )
    invocations = []
    for task, seed in task_runs:
        invocation_request_id = normalize_benchmark_request_id(task.task_id, int(seed))
        invocations.append(
            RuntimeTaskInvocation(
                request_id=invocation_request_id,
                seed=int(seed),
                task=task,
                trace_context=build_runtime_trace_context(
                    request_id=invocation_request_id,
                    provider_role="runtime",
                    runtime_hash=runtime_hash,
                    runtime_dir=runtime_dir,
                    task_id=task.task_id,
                    seed=int(seed),
                    objective=task.prompt,
                ),
            )
        )
    return RuntimeBatchRequest(
        request_id=request_id,
        runtime_backend=runtime_backend,
        budget_overrides=dict(budget_overrides or {}),
        trace_context=batch_trace_context,
        invocations=invocations,
    )
>>>>>>> REPLACE
```

### Block 14

```text
<<<<<<< SEARCH
def solve_result_from_run_result(request: SolveRequest, run: RunResult, runtime_hash: str) -> SolveResult:
=======
class PlanCompileError(ValueError):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def compile_execution_plan_for_benchmark(
    request: RuntimeSolveRequest,
    *,
    runtime_hash: str,
    runtime_dir: str,
) -> ExecutionPlan:
    task = request.task
    if task is None:
        raise PlanCompileError("template_mismatch", "benchmark solve requires a task payload")
    nodes = []
    for operation in task.operations:
        node_kind = {
            "builtin": "builtin_op",
            "generated_expression": "tool_synthesis" if not str(operation.expression or "").strip() else "tool_call",
            "memory_lookup": "memory_lookup",
            "direct_response": "direct_response",
        }.get(operation.kind)
        if node_kind is None:
            raise PlanCompileError("unsupported_operation", f"unsupported operation kind: {operation.kind}")
        nodes.append(
            PlanNode(
                node_id=operation.op_id,
                node_kind=node_kind,
                instruction=operation.description,
                output_key=operation.output_key,
                dependencies=list(operation.dependencies),
                tool_hint=operation.tool_hint,
                allowed_tool_categories=list(task.allowed_tool_categories),
                static_args=dict(operation.args),
                input_bindings=[],
                verification_required=task.verification_required or operation.externally_visible,
                externally_visible=operation.externally_visible,
                frame_role="root" if not operation.dependencies else "worker",
                branch_group_id=None,
                metadata={"operation_kind": operation.kind},
            )
        )
    verification_plan = VerificationPlan(
        mode=task.verifier_type,
        required=task.verification_required,
        checker_ladder=["local", "subtree", "repo", "benchmark"] if task.verification_required else [],
        exact_verifier_required=str(task.verifier_type).strip().lower() not in {"", "none", "best_effort"},
        artifact_contract={"expected": task.expected, "verifier_type": task.verifier_type},
        terminal_nodes=[node.node_id for node in nodes if node.output_key],
    )
    origin = PlanOrigin(
        origin_kind="benchmark",
        source_task_id=task.task_id,
        source_request_id=request.request_id,
        adapter_kind="benchmark_task_operations_v1",
        adaptation_assumptions=[],
    )
    plan_id = f"plan.{stable_hash(request.request_id, task.task_id, request.seed)[:16]}"
    plan = ExecutionPlan(
        plan_digest="",
        plan_id=plan_id,
        request_id=request.request_id,
        origin=origin,
        objective=task.prompt,
        context_refs=[item.get("symbol", item.get("file_path", "")) for item in task.context_items if isinstance(item, dict)],
        file_refs=list(task.file_paths),
        nodes=nodes,
        root_node_ids=[node.node_id for node in nodes if not node.dependencies],
        terminal_output_keys=[node.output_key for node in nodes if node.output_key],
        verification_plan=verification_plan,
        execution_flags=ExecutionFlags(
            allow_best_effort=task.allow_best_effort,
            allow_resume=True,
            allow_branching=True,
            allow_tool_synthesis=True,
            allow_async_handles=True,
            requires_terminal_verification=task.verification_required,
        ),
        allowed_tool_categories=list(task.allowed_tool_categories),
        budget_overrides=dict(request.budget_overrides),
        externally_visible=task.externally_visible,
        trace_context=request.trace_context,
    )
    plan.plan_digest = stable_hash(plan.model_dump(exclude={"plan_digest"}))
    return plan


def compile_execution_plan_for_user_request(
    request: RuntimeSolveRequest,
    *,
    runtime_hash: str,
    runtime_dir: str,
) -> ExecutionPlan:
    solve_request = request.solve_request
    if solve_request is None:
        raise PlanCompileError("template_mismatch", "user_request solve requires a solve_request payload")
    prompt = solve_request.prompt
    template_kind = "direct_answer"
    if solve_request.file_paths:
        template_kind = "file_inspection"
    elif solve_request.output_schema:
        template_kind = "structured_computation"
    allowed_node_kind = {
        "direct_answer": "direct_response",
        "structured_computation": "direct_response",
        "file_inspection": "memory_lookup",
    }[template_kind]
    terminal_node = PlanNode(
        node_id="respond",
        node_kind=allowed_node_kind,
        instruction=prompt,
        output_key="response",
        dependencies=[],
        allowed_tool_categories=list(solve_request.allowed_tool_categories),
        static_args={"output_schema": dict(solve_request.output_schema)},
        input_bindings=[],
        verification_required=solve_request.verification_preference == "required",
        externally_visible=True,
        frame_role="root",
        metadata={"template_kind": template_kind},
    )
    origin = PlanOrigin(
        origin_kind="user_request",
        source_request_id=solve_request.request_id,
        adapter_kind=f"user_request_template.{template_kind}.v1",
        adaptation_assumptions=[],
    )
    plan = ExecutionPlan(
        plan_digest="",
        plan_id=f"plan.{stable_hash(solve_request.request_id, template_kind, request.seed)[:16]}",
        request_id=solve_request.request_id,
        origin=origin,
        objective=prompt,
        context_refs=[stable_hash(item)[:12] for item in solve_request.context_items],
        file_refs=list(solve_request.file_paths),
        nodes=[terminal_node],
        root_node_ids=["respond"],
        terminal_output_keys=["response"],
        verification_plan=VerificationPlan(
            mode="none" if solve_request.verification_preference == "best_effort" else "runtime_request",
            required=solve_request.verification_preference == "required",
            checker_ladder=["local", "subtree", "repo"],
            exact_verifier_required=False,
            artifact_contract=dict(solve_request.output_schema),
            terminal_nodes=["respond"],
        ),
        execution_flags=ExecutionFlags(
            allow_best_effort=solve_request.verification_preference != "required",
            allow_resume=True,
            allow_branching=True,
            allow_tool_synthesis=True,
            allow_async_handles=True,
            requires_terminal_verification=solve_request.verification_preference == "required",
        ),
        allowed_tool_categories=list(solve_request.allowed_tool_categories),
        budget_overrides=dict(request.budget_overrides),
        externally_visible=True,
        trace_context=request.trace_context,
    )
    plan.plan_digest = stable_hash(plan.model_dump(exclude={"plan_digest"}))
    return plan


def solve_result_from_run_result(request: SolveRequest, run: RunResult, runtime_hash: str) -> SolveResult:
>>>>>>> REPLACE
```

## agintor/runtime_host.py

### Notes

- Host stays transport-only, but it needs first-class `resume`, per-invocation request IDs, trace-context transport, and isolation preflight against reported guarantees.
- `run_batch()` must stop treating the batch request ID as the identity of each invocation.

### Block 15

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
>>>>>>> REPLACE
```

Add immediately below `solve()`:

```text
<<<<<<< SEARCH
    def _preflight_solve_contract(
=======
    def resume(
        self,
        runtime_dir: str | Path,
        request: ResumeRequest,
        *,
        provider: ModelProvider,
        runtime_profile: object | None = None,
    ) -> RuntimeSolveResponse:
        capability_exchange = self.inspect(runtime_dir)
        self._preflight_runtime_guarantees(runtime_dir, capability_exchange)
        if self.runtime_backend == "docker" and self.container_executor is not None:
            response = self.container_executor.resume_protocol(
                runtime_dir,
                request,
                provider=provider,
                runtime_profile=runtime_profile,
            )
        else:
            response = self._run_local_resume(
                Path(runtime_dir),
                request,
                provider=provider,
                runtime_profile=runtime_profile,
            )
        if response.capability_exchange != capability_exchange:
            raise RuntimeLoadError("runtime capability exchange changed between inspect and resume")
        failed = response.solve_result.status in {"failed", "controlled_failure"} or bool(response.solve_result.faults.get("hard_invalid"))
        self._prune_solve_result_artifacts(response, failed=failed)
        return response

    def _preflight_solve_contract(
>>>>>>> REPLACE
```

### Block 16

```text
<<<<<<< SEARCH
    def _preflight_solve_contract(
        self,
        runtime_dir: str | Path,
        capability_exchange: CapabilityExchange,
        request: RuntimeSolveRequest,
        *,
        provider: ModelProvider,
        runtime_profile: object | None,
    ) -> None:
        if not self._provider_matches_runtime_profile(provider, runtime_profile):
            return
        if not self._request_requires_default_provider(request):
            return
        missing = [
            name
            for name in capability_exchange.required_env_names
            if str(name).strip() and not self._runtime_requirement_available(provider, str(name))
        ]
=======
    def _preflight_solve_contract(
        self,
        runtime_dir: str | Path,
        capability_exchange: CapabilityExchange,
        request: RuntimeSolveRequest,
        *,
        provider: ModelProvider,
        runtime_profile: object | None,
    ) -> None:
        self._preflight_runtime_guarantees(runtime_dir, capability_exchange)
        if not self._provider_matches_runtime_profile(provider, runtime_profile):
            return
        if not self._request_requires_default_provider(request):
            return
        missing = [
            name
            for name in capability_exchange.required_env_names
            if str(name).strip() and not self._runtime_requirement_available(provider, str(name))
        ]
>>>>>>> REPLACE
```

Add immediately below that function:

```text
<<<<<<< SEARCH
    def _run_local_inspect(self, runtime_dir: Path, request) -> CapabilityExchange:
=======
    def _preflight_runtime_guarantees(self, runtime_dir: str | Path, capability_exchange: CapabilityExchange) -> None:
        policy = capability_exchange.runtime_isolation_policy
        if policy is None:
            return
        missing = [
            guarantee
            for guarantee in policy.required_guarantees
            if not capability_exchange.effective_guarantees.get(guarantee, False)
        ]
        if missing:
            raise RuntimeLoadError(
                f"runtime backend {self.runtime_backend!r} cannot satisfy required guarantees for {runtime_dir}: {', '.join(sorted(missing))}"
            )

    def _run_local_inspect(self, runtime_dir: Path, request) -> CapabilityExchange:
>>>>>>> REPLACE
```

### Block 17

```text
<<<<<<< SEARCH
    def _run_local_solve(
        self,
        runtime_dir: Path,
        request: RuntimeSolveRequest,
        *,
        provider: ModelProvider,
        runtime_profile: object | None,
    ) -> RuntimeSolveResponse:
=======
    def _run_local_solve(
        self,
        runtime_dir: Path,
        request: RuntimeSolveRequest,
        *,
        provider: ModelProvider,
        runtime_profile: object | None,
    ) -> RuntimeSolveResponse:
>>>>>>> REPLACE
```

Add immediately below `_run_local_solve()`:

```text
<<<<<<< SEARCH
    def _cleanup_run_dir(self, run_dir: Path, *, failed: bool) -> None:
=======
    def _run_local_resume(
        self,
        runtime_dir: Path,
        request: ResumeRequest,
        *,
        provider: ModelProvider,
        runtime_profile: object | None,
    ) -> RuntimeSolveResponse:
        run_dir = ensure_directory(self.workspace / f"resume_{stable_hash(runtime_dir, model_dump(request))[:12]}")
        input_json = run_dir / "resume_request.json"
        output_json = run_dir / "resume_response.json"
        provider_json = run_dir / "provider.json"
        profile_json = run_dir / "runtime_profile.json"
        workspace_dir = ensure_directory(run_dir / "workspace")
        input_json.write_text(json.dumps(model_dump(request), indent=2, sort_keys=True), encoding="utf-8")
        provider_json.write_text(json.dumps(self._local_provider_payload(provider), indent=2, sort_keys=True), encoding="utf-8")
        command = self._runtime_command(
            runtime_dir,
            "resume",
            input_json=input_json,
            output_json=output_json,
            provider_json=provider_json,
            workspace=workspace_dir,
            profile_json=profile_json if runtime_profile is not None else None,
        )
        if runtime_profile is not None:
            profile_json.write_text(json.dumps(model_dump(runtime_profile), indent=2, sort_keys=True), encoding="utf-8")
        completed = subprocess.run(
            command,
            env=self._runtime_env(runtime_dir),
            cwd=str(run_dir.resolve()),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeLoadError(completed.stderr.strip() or completed.stdout.strip() or "runtime resume failed")
        response = model_validate(RuntimeSolveResponse, json.loads(output_json.read_text(encoding="utf-8")))
        failed = response.solve_result.status in {"failed", "controlled_failure"} or bool(response.solve_result.faults.get("hard_invalid"))
        self._cleanup_run_dir(run_dir, failed=failed)
        return response

    def _cleanup_run_dir(self, run_dir: Path, *, failed: bool) -> None:
>>>>>>> REPLACE
```

## agintor/runtime_sdk/runtime_entry.py

### Notes

- The bundled runtime entrypoint should own `resume`. There should not be a host-only restore path.
- `solve` and `run-batch` should compile plans and execute through the same runtime engine.

### Block 18

```text
<<<<<<< SEARCH
def _solve(args: argparse.Namespace) -> int:
=======
def _solve(args: argparse.Namespace) -> int:
>>>>>>> REPLACE
```

Replace the body of `_solve()` so it:

- loads `RuntimeSolveRequest`
- loads runtime and provider
- constructs one `TaskRuntime`
- calls `runner.execute_request(request)` instead of `solve_request_to_task()` and `runner.run_task()`
- shapes failures using the existing failure wrapper

Add a new `_resume()` sibling:

```text
<<<<<<< SEARCH
def main(argv: list[str] | None = None) -> int:
=======
def _resume(args: argparse.Namespace) -> int:
    request = model_validate(
        ResumeRequest,
        json.loads(Path(args.input_json).read_text(encoding="utf-8")),
    )
    runtime_profile = load_runtime_profile(args.runtime_dir, profile_path=args.profile_json)
    runtime = load_runtime(
        args.runtime_dir,
        runtime_profile=runtime_profile,
        runtime_backend=args.runtime_backend if hasattr(args, "runtime_backend") else "local",
    )
    capability_exchange = CapabilityExchange(**model_dump(runtime.capability_exchange))
    provider_payload_data = json.loads(Path(args.provider_json).read_text(encoding="utf-8"))
    provider = build_provider_from_payload(provider_payload_data, provider_profile=runtime_profile.runtime_provider)
    shell = FixedShell(
        Path(args.workspace) / "resume",
        artifact_mode=ArtifactMode(args.artifact_mode),
    )
    runner = TaskRuntime(
        runtime,
        shell,
        provider,
        runtime_profile=runtime_profile,
    )
    response = runner.execute_resume(request, capability_exchange=capability_exchange)
    Path(args.output_json).write_text(json.dumps(model_dump(response), indent=2, sort_keys=True), encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
>>>>>>> REPLACE
```

### Block 19

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

And dispatch it in `main()`:

```text
<<<<<<< SEARCH
    if args.command == "solve":
        return _solve(args)
=======
    if args.command == "solve":
        return _solve(args)
    if args.command == "resume":
        return _resume(args)
>>>>>>> REPLACE
```

## agintor/runner.py

### Notes

- Keep `TaskRuntime`, but replace the raw task loop with a plan executor.
- `run_task()` becomes a benchmark compatibility wrapper around `execute_request()` and `compile_execution_plan_for_benchmark()`.
- The runtime should emit state transitions and event rows as it moves through `idle -> compiling -> validating -> running -> branching -> merging -> completing -> completed`.
- Branch groups should use `ThreadPoolExecutor` with `cancel_futures=True` on shutdown and explicit cancellation records.

### Block 20

```text
<<<<<<< SEARCH
class TaskRuntime:
    def __init__(
        self,
        runtime: LoadedRuntime,
        shell: FixedShell,
        provider: ModelProvider,
        budget_overrides: Mapping[str, Any] | None = None,
        runtime_profile: RuntimeProfile | None = None,
    ) -> None:
=======
class TaskRuntime:
    def __init__(
        self,
        runtime: LoadedRuntime,
        shell: FixedShell,
        provider: ModelProvider,
        budget_overrides: Mapping[str, Any] | None = None,
        runtime_profile: RuntimeProfile | None = None,
    ) -> None:
>>>>>>> REPLACE
```

Add immediately below `_isolated_provider_environment()`:

```text
<<<<<<< SEARCH
    def run_task(self, task: BenchmarkTask, seed: int) -> RunResult:
=======
    def execute_request(
        self,
        request: RuntimeSolveRequest,
        *,
        capability_exchange: CapabilityExchange | None = None,
    ) -> RuntimeSolveResponse:
        with self._isolated_provider_environment():
            if request.mode == "benchmark":
                plan = compile_execution_plan_for_benchmark(
                    request,
                    runtime_hash=self.runtime.runtime_hash,
                    runtime_dir=str(self.runtime.runtime_dir),
                )
                solve_request = benchmark_task_to_solve_request(request.task, request_id=request.request_id)
            else:
                plan = compile_execution_plan_for_user_request(
                    request,
                    runtime_hash=self.runtime.runtime_hash,
                    runtime_dir=str(self.runtime.runtime_dir),
                )
                solve_request = model_validate(SolveRequest, model_dump(request.solve_request))
            run_result = self._execute_plan(plan, request=request)
            solve_result = solve_result_from_run_result_with_context(
                solve_request,
                run_result,
                self.runtime.runtime_hash,
                mode=request.mode,
                provider_usage=self.provider.usage_summary(),
            )
            return RuntimeSolveResponse(
                request_id=request.request_id,
                capability_exchange=CapabilityExchange(**model_dump(capability_exchange or self.runtime.capability_exchange)),
                solve_result=solve_result,
            )

    def execute_resume(
        self,
        request: ResumeRequest,
        *,
        capability_exchange: CapabilityExchange | None = None,
    ) -> RuntimeSolveResponse:
        envelope = self.shell.load_checkpoint_envelope(request)
        run_result = self._resume_plan(envelope, reconciliation_policy=request.reconciliation_policy)
        solve_request = SolveRequest(
            request_id=request.request_id,
            prompt=request.prompt or "",
            context_items=[],
            file_paths=[],
            output_schema={},
            allowed_tool_categories=[],
            verification_preference="verified_if_available",
            budget_overrides={},
        )
        solve_result = solve_result_from_run_result_with_context(
            solve_request,
            run_result,
            self.runtime.runtime_hash,
            mode="user_request",
            provider_usage=self.provider.usage_summary(),
        )
        return RuntimeSolveResponse(
            request_id=request.request_id,
            capability_exchange=CapabilityExchange(**model_dump(capability_exchange or self.runtime.capability_exchange)),
            solve_result=solve_result,
        )

    def run_task(self, task: BenchmarkTask, seed: int) -> RunResult:
>>>>>>> REPLACE
```

Follow-through note for the block above:

- After `execute_request()` and `execute_resume()` land, remove the task-centric body of `run_task()` and replace it with a thin benchmark wrapper that synthesizes `RuntimeSolveRequest` and delegates into `_execute_plan()`.

### Block 21

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
            output, local_faults, checkpoint = self._execute_isolated_frame(
                context,
                worker_frame,
                [self._operation_by_id(task, op_id) for op_id in op_order],
                isolate_runtime_state=True,
            )
            worker_outputs.append(
                {
                    "worker_id": worker_frame.worker_id,
                    "artifact": output,
                    "verifier_support": self._worker_support(task, output),
                    "predicted_solve": worker.get("predicted_solve", 0.5),
                    "unresolved_critical": 0 if output else 1,
                    "summary": model_dump(checkpoint.summary),
                }
            )
            faults += local_faults
            self.shell.message_board.append(worker_frame.worker_id or "worker", {"artifact": output, "summary": model_dump(checkpoint.summary)})
        context.state.queue.append(
            AgentFrame(
                agent=self.shell.agent_pool.clone("root"),
                objective="merge",
                operation_ids=[],
                depth=frame.depth,
                role="merge_horizontal",
                metadata={"worker_outputs": worker_outputs, "parent_run_node_id": frame.metadata.get("run_node_id")},
            )
        )
=======
        branch_plans = self.runtime.topology.select_workers(context, frame, task.operations)
        branch_results = self._run_branch_group(
            context,
            parent_frame=frame,
            branch_plans=branch_plans,
            operations_by_id={operation.op_id: operation for operation in task.operations},
        )
        worker_outputs = [result["merged_candidate"] for result in branch_results]
        context.state.queue.append(
            AgentFrame(
                frame_id=stable_hash("merge", frame.frame_id, task.task_id)[:16],
                agent=self.shell.agent_pool.clone("root"),
                objective="merge",
                plan_node_ids=[],
                depth=frame.depth,
                role="merge_horizontal",
                trace_context=derive_child_trace_context(
                    frame.trace_context,
                    frame_role="merge_horizontal",
                    op_id="merge_horizontal",
                ),
                metadata={"worker_outputs": worker_outputs, "parent_run_node_id": frame.metadata.get("run_node_id")},
            )
        )
>>>>>>> REPLACE
```

### Block 22

```text
<<<<<<< SEARCH
        if dispatch_meta.get("async"):
            handle = self.shell.tool_executor.launch_async(
                tool_name,
                args,
                self.shell.workspace / "handles",
                context.task.task_id,
            )
            self.shell.open_handles.add(handle)
            context.state.open_handle_ids.append(handle.handle_id)
            handle_node_id = self.shell.short_term.add_node("OpenHandle", handle.tool_name, model_dump(handle))
            if run_node_id and run_node_id in self.shell.short_term.nodes:
                self.shell.short_term.add_edge(run_node_id, handle_node_id, "WAITS_ON")
            if hasattr(self.shell.tool_executor, "await_handle"):
                finished = self.shell.tool_executor.await_handle(handle.handle_id, self.shell.open_handles)
                context.budget.consume_tool_latency(float(finished.get("latency_s", 0.0)))
                if finished.get("state") != "completed":
                    faults += 1
                    stderr = str(finished.get("stderr", "async execution failed"))
                    context.record("tool_fault", tool=tool_name, stderr=stderr)
                    self._record_tool_failure(context, operation, tool_name, stderr)
                    raise HardInvalidation(f"tool execution failed for {tool_name}: {stderr}")
                output = finished.get("output")
            elif hasattr(self.shell.tool_executor, "wait_async"):
                result = self.shell.tool_executor.wait_async(handle)
                context.budget.consume_tool_latency(result.latency_s)
                if not result.success:
                    faults += 1
                    context.record("tool_fault", tool=tool_name, stderr=result.stderr)
                    self._record_tool_failure(context, operation, tool_name, result.stderr)
                    raise HardInvalidation(f"tool execution failed for {tool_name}: {result.stderr}")
                output = result.output
                self.shell.open_handles.update_state(handle.handle_id, "completed")
            else:
                self.shell.open_handles.update_state(handle.handle_id, "completed")
                output = None
=======
        if dispatch_meta.get("async"):
            handle = self.shell.tool_executor.launch_async(
                tool_name,
                args,
                self.shell.workspace / "handles",
                context.plan.request_id,
            )
            receipt = self.shell.record_side_effect_receipt(
                action_kind="tool_launch",
                request_id=context.request_id,
                branch_id=frame.branch_id,
                backend="runtime",
                fingerprint=stable_hash(tool_name, args, handle.handle_id),
                result_ref=handle.handle_id,
                reconciliation_policy="strict",
            )
            self.shell.open_handles.add(handle)
            context.state.open_handle_ids.append(handle.handle_id)
            self._emit_runtime_event(context, "side_effect_recorded", handle_id=handle.handle_id, side_effect_id=receipt.side_effect_id)
            self._publish_checkpoint_envelope(context, boundary="after_tool_launch")
            output = {"async_handle_id": handle.handle_id}
>>>>>>> REPLACE
```

Follow-through note for the block above:

- Add `_reconcile_async_handles()` during merge, cancellation, and resume. `tool_completion` receipts should be written when a handle becomes terminal, not at launch time.

### Block 23

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
=======
        request_trace_context = derive_child_trace_context(
            context.trace_context,
            frame_role=context.state.mode or "single",
            op_id=operation.op_id,
            run_node_id=context.trace_context.run_node_id,
        )
        receipt = self.shell.record_side_effect_receipt(
            action_kind="provider_request",
            request_id=context.request_id,
            branch_id=context.trace_context.branch_id,
            backend=self.runtime_profile.runtime_provider.name,
            fingerprint=stable_hash(model_class, prompt_lines, output_schema),
            result_ref=None,
            reconciliation_policy="strict",
        )
        response = context.provider.generate(
            ModelRequest(
                instructions="Return the strongest bounded answer you can for the request. Use JSON only when an output schema is provided.",
                prompt="\n".join(prompt_lines),
                model_class=model_class,
                seed=context.seed,
                metadata={
                    "mode": "user_request",
                    "payload": {
                        "prompt": context.task.prompt if context.task is not None else context.objective,
                        "output_schema": output_schema,
                    },
                    "trace_context": request_trace_context.model_dump(exclude_none=True),
                    "receipt_id": receipt.side_effect_id,
                },
            )
        )
        self.shell.record_side_effect_receipt(
            action_kind="provider_completion",
            request_id=context.request_id,
            branch_id=context.trace_context.branch_id,
            backend=self.runtime_profile.runtime_provider.name,
            fingerprint=stable_hash(receipt.side_effect_id, response.model_name, response.text),
            result_ref=receipt.side_effect_id,
            reconciliation_policy="strict",
        )
>>>>>>> REPLACE
```

## agintor/shell.py

### Notes

- `save_checkpoints()` currently writes one end-of-run summary blob. WS2 needs deterministic boundary publication and resume loading.
- Shell should own receipt storage, checkpoint-envelope storage, publication acceptance, and invariant checks over publication visibility.

### Block 24

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
    def publish_checkpoint_envelope(self, envelope: CheckpointEnvelope) -> CheckpointReference:
        ensure_directory(self.checkpoint_dir / envelope.request_id)
        path = self.checkpoint_dir / envelope.request_id / f"{envelope.checkpoint_id}.json"
        path.write_text(json.dumps(model_dump(envelope), indent=2, sort_keys=True), encoding="utf-8")
        return CheckpointReference(
            ref=str(path),
            request_id=envelope.request_id,
            plan_id=envelope.plan_id,
            runtime_hash=envelope.runtime_hash,
            checkpoint_id=envelope.checkpoint_id,
            task_id=envelope.task_id,
            seed=envelope.seed,
            sequence_no=envelope.trace_cursor.get("sequence_no", 0),
            checkpoint_count=1,
        )

    def load_checkpoint_envelope(self, request: ResumeRequest) -> CheckpointEnvelope:
        if request.checkpoint_ref is not None:
            path = Path(request.checkpoint_ref.ref)
        else:
            request_dir = self.checkpoint_dir / request.request_id
            candidates = sorted(request_dir.glob("*.json"))
            if not candidates:
                raise HardInvalidation("no checkpoint available for resume request")
            path = candidates[-1]
        payload = json.loads(path.read_text(encoding="utf-8"))
        envelope = CheckpointEnvelope(**payload)
        return envelope

    def save_checkpoints(self, task_id: str, seed: int, checkpoints: Mapping[str, Any]) -> CheckpointReference | None:
        return None
>>>>>>> REPLACE
```

### Block 25

```text
<<<<<<< SEARCH
    def validate_invariants(self, transfer_scored: bool = False) -> None:
=======
    def record_side_effect_receipt(
        self,
        *,
        action_kind: str,
        request_id: str,
        branch_id: str | None,
        backend: str,
        fingerprint: str,
        result_ref: str | None,
        reconciliation_policy: str,
    ) -> SideEffectReceipt:
        receipt = SideEffectReceipt(
            side_effect_id=stable_hash(action_kind, request_id, branch_id, fingerprint, now_ts())[:16],
            action_fingerprint=fingerprint,
            idempotency_key=stable_hash(request_id, action_kind, fingerprint),
            action_kind=action_kind,
            branch_id=branch_id,
            request_digest=stable_hash(request_id),
            backend=backend,
            status="recorded",
            result_ref=result_ref,
            replay_policy="reuse_or_reconcile",
            reconciliation_policy=reconciliation_policy,
            created_at=now_ts(),
        )
        if not hasattr(self, "side_effect_receipts"):
            self.side_effect_receipts = {}
        self.side_effect_receipts[receipt.side_effect_id] = receipt
        return receipt

    def validate_invariants(self, transfer_scored: bool = False) -> None:
>>>>>>> REPLACE
```

## agintor/runtime_loader.py

### Notes

- WS2 should bump `runtime_abi` to `agintor-runtime-abi-v4` and `storage_schema_version` to `agintor-storage-v2`.
- Loader-side capability exchange must expose effective guarantees and the runtime-wide isolation policy.

### Block 26

```text
<<<<<<< SEARCH
RUNTIME_ABI_VERSION = "agintor-runtime-abi-v3"
=======
RUNTIME_ABI_VERSION = "agintor-runtime-abi-v4"
>>>>>>> REPLACE
```

### Block 27

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
    runtime_isolation_policy = RuntimeIsolationPolicy(
        timeout_envelope={"seconds": getattr(deployment_contract, "timeout_seconds", None)},
        workspace_root=str(runtime_path),
        environment_allowlist=list(deployment_contract.environment_allowlist),
        network_policy=deployment_contract.network_policy,
        filesystem_policy=deployment_contract.filesystem_policy,
        required_guarantees=list(getattr(deployment_contract, "required_guarantees", [])),
        desired_guarantees=list(getattr(deployment_contract, "desired_guarantees", [])),
    )
    effective_guarantees = {
        "timeout_enforcement": True,
        "workspace_isolation": bool(runtime_backend in {"local", "docker"}),
        "environment_filtering": True,
        "process_cleanup": True,
        "network_disablement": runtime_backend == "docker" and deployment_contract.network_policy == "none",
    }
    capability_exchange = CapabilityExchange(
        runtime_abi=RUNTIME_ABI_VERSION,
        kernel_version=kernel_manifest.kernel_version,
        storage_schema_version=kernel_manifest.storage_schema_version,
        supported_backends=list(deployment_contract.supported_backends),
        tool_runtimes=["python"],
        checkpoint_support=True,
        runtime_asset_capabilities={"traces": True, "checkpoints": True, "runtime_sdk": True},
        side_effect_receipts=True,
        required_env_names=list(deployment_contract.required_env_names),
        required_env_any_of=[list(group) for group in deployment_contract.required_env_any_of],
        capability_flags=list(deployment_contract.capability_flags or kernel_manifest.capability_flags),
        supported_guarantees={
            "timeout_enforcement": True,
            "workspace_isolation": True,
            "environment_filtering": True,
            "process_cleanup": True,
            "network_disablement": runtime_backend == "docker",
        },
        effective_guarantees=effective_guarantees,
        runtime_isolation_policy=runtime_isolation_policy,
    )
>>>>>>> REPLACE
```

## agintor/container_runtime.py

### Notes

- Docker commands should be assembled from the declared `RuntimeIsolationPolicy`, not from an unconditional mount bundle.
- `docker` should enforce `--network none` when the policy requires it and reject impossible guarantee combinations before launch.

### Block 28

```text
<<<<<<< SEARCH
        command = [
            "docker",
            "run",
            "--rm",
            "-e",
            f"PYTHONPATH=/mnt/runtime/{KERNEL_BUNDLE_DIR}",
        ]
=======
        command = [
            "docker",
            "run",
            "--rm",
            "--init",
            "-e",
            f"PYTHONPATH=/mnt/runtime/{KERNEL_BUNDLE_DIR}",
        ]
        policy = resolved_isolation_policy
        if policy and policy.get("network_policy") == "none":
            command.extend(["--network", "none"])
        command.extend(["--mount", f"type=bind,src={runtime_path},dst=/mnt/runtime,readonly"])
>>>>>>> REPLACE
```

Follow-through note for the block above:

- Replace the remaining `-v` mount assembly in `inspect()`, `run_batch_protocol()`, and `solve_protocol()` with helper-built `--mount` entries so read-only versus read-write intent is explicit and auditable.

## agintor/templates/baseline_runtime/topology_policy.py

### Notes

- `select_workers()` should emit branch plans, not free-form dicts.
- `merge_ensemble()` should rank branch publications, not raw worker output dicts, and the sort key should match the workstream contract.

### Block 29

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
            {
                "worker_id": "w1",
                "instruction": "Reverse order plan",
                "op_ids": list(reversed(op_ids)),
                "predicted_solve": 0.58,
                "tool_scope": ctx.state.visible_tool_names,
                "agent_id": "root",
            },
            {
                "worker_id": "w2",
                "instruction": "Dependency-first plan",
                "op_ids": sorted(
                    op_ids,
                    key=lambda op_id: 0 if any(op.op_id == op_id and op.dependencies for op in operations) else 1,
                ),
                "predicted_solve": 0.55,
                "tool_scope": ctx.state.visible_tool_names,
                "agent_id": "root",
            },
        ]
        selected = []
        selected_ids = set()
        while len(selected) < min(config.k_max, len(candidates)):
            best = None
            best_score = -1e9
            for worker in candidates:
                if worker["worker_id"] in selected_ids:
                    continue
                solve_term = 1.0 - (1.0 - worker["predicted_solve"])
                diversity_penalty = 0.0
                for existing in selected:
                    diversity_penalty += jaccard(worker["op_ids"], existing["op_ids"])
                score = solve_term - 0.12 * diversity_penalty - 0.06 * (len(selected) + 1)
                if score > best_score:
                    best_score = score
                    best = worker
            if best is None or best_score < 0.35:
                break
            selected.append(best)
            selected_ids.add(best["worker_id"])
        return selected or [candidates[0]]
=======
    def select_workers(self, ctx, frame, operations: Sequence[Any]) -> list[dict[str, Any]]:
        config = ctx.profile.topology
        op_ids = [op.op_id for op in operations]
        candidates = [
            ("w0", "Sequential canonical plan", op_ids, 0.62),
            ("w1", "Reverse order plan", list(reversed(op_ids)), 0.58),
            (
                "w2",
                "Dependency-first plan",
                sorted(op_ids, key=lambda op_id: 0 if any(op.op_id == op_id and op.dependencies for op in operations) else 1),
                0.55,
            ),
        ]
        selected = []
        selected_ids = set()
        while len(selected) < min(config.k_max, len(candidates)):
            best = None
            best_score = -1e9
            for worker_id, instruction, candidate_op_ids, predicted_solve in candidates:
                if worker_id in selected_ids:
                    continue
                diversity_penalty = 0.0
                for existing in selected:
                    diversity_penalty += jaccard(candidate_op_ids, existing["assigned_node_ids"])
                score = predicted_solve - 0.12 * diversity_penalty - 0.06 * (len(selected) + 1)
                if score > best_score:
                    best_score = score
                    best = {
                        "branch_id": worker_id,
                        "instruction": instruction,
                        "assigned_node_ids": candidate_op_ids,
                        "merge_priority": len(selected),
                        "predicted_solve": predicted_solve,
                        "tool_scope": ctx.state.visible_tool_names,
                        "agent_id": "root",
                    }
            if best is None or best_score < 0.35:
                break
            selected.append(best)
            selected_ids.add(best["branch_id"])
        return selected or [
            {
                "branch_id": "w0",
                "instruction": "Sequential canonical plan",
                "assigned_node_ids": op_ids,
                "merge_priority": 0,
                "predicted_solve": 0.62,
                "tool_scope": ctx.state.visible_tool_names,
                "agent_id": "root",
            }
        ]
>>>>>>> REPLACE
```

### Block 30

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
    def merge_ensemble(self, ctx, worker_outputs: Sequence[dict[str, Any]]) -> Any:
        ordered = sorted(
            worker_outputs,
            key=lambda item: (
                item.get("merge_priority", 0),
                -item.get("verifier_support", 0.0),
                item.get("unresolved_critical", 0),
                item.get("branch_rank", 0),
                item.get("branch_id", ""),
            ),
        )
        return ordered[0]["artifact"] if ordered else {}
>>>>>>> REPLACE
```

## Required Follow-Through Outside the WS2 Ownership List

- `agintor/runtime_sdk/bundle.py` and the bundled kernel manifest path need the version bump to `agintor-runtime-abi-v4` and `agintor-storage-v2` so exported runtimes and the loader stay aligned.
- `agintor/templates/baseline_runtime/deployment_contract.json`, `runtime_manifest.json`, and the runtime-plan/export stamping path need the new isolation-policy and guarantee fields, otherwise the loader and host changes cannot be exercised.
- `agintor/cli.py` should stop hardcoding `solve.<task_id>.<seed>` and defer to `runtime_api.normalize_benchmark_request_id()` for benchmark-mode request IDs.

## Regression Coverage

- Extend `tests/test_runtime_host.py` for normalized benchmark request IDs, invocation-level trace context, `resume` transport, and guarantee-preflight failures.
- Add focused runtime tests for plan validation, branch concurrency, deterministic merge across completion-order variation, sibling cancellation, checkpoint restore, receipt-backed resume, and docker guarantee mismatches.
- Add one runtime-entry test that proves `solve`, `run-batch`, and `resume` all flow through the bundled runtime entry module rather than bypassing it.
