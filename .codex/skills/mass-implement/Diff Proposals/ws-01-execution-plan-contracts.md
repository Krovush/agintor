# Workstream 2 Runtime Contracts and Entrypoint Consolidation

## Contract Decisions

- `OpenAITraceContext` stays the single provider-agnostic correlation envelope from `TRACE_AND_PLANNING_IMPROVEMENTS_PLAN.md`. Runtime code should enrich it by copy-on-write builders, not by mutating ad hoc metadata dictionaries.
- `ExecutionPlan` becomes the runtime-owned execution unit for both benchmark tasks and user requests. `BenchmarkTask` remains a host/planning provenance contract and compiles 1:1 into plan nodes unless the compiler inserts explicit `verify`, `checkpoint`, or `merge` nodes.
- `RuntimeTaskInvocation.request_id` is mandatory. Benchmark invocations normalize to `benchmark.<task_id>.seed_<seed>`.
- `RuntimeBatchRequest` stays transport-only. Independent invocations do not share `TaskRuntime` state just because they share a seed. Shared state is allowed only for an explicitly transfer-scored episode.
- `resume` becomes a first-class runtime entry command returning the same terminal `RuntimeSolveResponse` surface as `solve`.
- ABI and storage advance together here: `agintor-runtime-abi-v4` and `agintor-storage-v2`.
- The isolation contract is explicit and fail-closed. `local` can advertise only the guarantees it truly enforces; `docker` must be required whenever the deployment contract demands stronger guarantees.

## `agintor/schemas.py`

Notes:
- Add runtime-native plan, branch, checkpoint, side-effect, trace, and isolation contracts here rather than scattering dict-shaped payloads across `runtime_api`, `runtime_host`, and `runner`.
- Keep `SolveRequest` and `RuntimeSolveRequest` as transport envelopes, but extend them with typed trace context.
- Add one extra `ExecutionPlan.plan_constants` field. `InputBinding.source_kind == "plan_constant"` is underspecified without an explicit constant store.

```text
<<<<<<< SEARCH
class DeploymentContract(BaseModel):
    entry_command: str
    runtime_abi: str
    kernel_version: str = ""
    storage_schema_version: str = ""
    python_version: str
    supported_backends: List[str] = Field(default_factory=list)
    required_env_names: List[str] = Field(default_factory=list)
=======
class RuntimeIsolationPolicy(BaseModel):
    timeout_envelope: Dict[str, Any] = Field(default_factory=dict)
    workspace_root: str = ""
    environment_allowlist: List[str] = Field(default_factory=list)
    network_policy: str = "none"
    filesystem_policy: str = "workspace_only"
    required_guarantees: List[
        Literal[
            "timeout_enforcement",
            "workspace_isolation",
            "environment_filtering",
            "process_cleanup",
            "network_disablement",
        ]
    ] = Field(default_factory=list)
    desired_guarantees: List[
        Literal[
            "timeout_enforcement",
            "workspace_isolation",
            "environment_filtering",
            "process_cleanup",
            "network_disablement",
        ]
    ] = Field(default_factory=list)


class RuntimeIsolationBackendCapabilities(BaseModel):
    backend: str
    effective_guarantees: List[str] = Field(default_factory=list)
    unsupported_guarantees: List[str] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)


class DeploymentContract(BaseModel):
    entry_command: str
    runtime_abi: str
    kernel_version: str = ""
    storage_schema_version: str = ""
    python_version: str
    supported_backends: List[str] = Field(default_factory=list)
    required_env_names: List[str] = Field(default_factory=list)
    runtime_isolation_policy: Optional[RuntimeIsolationPolicy] = None
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
class OpenAITraceContext(BaseModel):
    session_id: Optional[str] = None
    provider_role: Optional[Literal["factory", "runtime"]] = None
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


class ExecutionFlags(BaseModel):
    allow_best_effort: bool = False
    allow_resume: bool = True
    allow_branching: bool = True
    allow_tool_synthesis: bool = True
    allow_async_handles: bool = True
    requires_terminal_verification: bool = False


class VerificationPlan(BaseModel):
    mode: str
    required: bool = True
    checker_ladder: List[str] = Field(default_factory=list)
    exact_verifier_required: bool = False
    artifact_contract: Dict[str, Any] = Field(default_factory=dict)
    terminal_nodes: List[str] = Field(default_factory=list)


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
    output_key: Optional[str] = None
    dependencies: List[str] = Field(default_factory=list)
    tool_hint: Optional[str] = None
    allowed_tool_categories: List[str] = Field(default_factory=list)
    static_args: Dict[str, Any] = Field(default_factory=dict)
    input_bindings: List[InputBinding] = Field(default_factory=list)
    verification_required: bool = False
    externally_visible: bool = False
    frame_role: str = "root"
    branch_group_id: Optional[str] = None
    consumes_branch_group_id: Optional[str] = None
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
    plan_constants: Dict[str, Any] = Field(default_factory=dict)
    nodes: List[PlanNode] = Field(default_factory=list)
    root_node_ids: List[str] = Field(default_factory=list)
    terminal_output_keys: List[str] = Field(default_factory=list)
    verification_plan: VerificationPlan
    execution_flags: ExecutionFlags
    allowed_tool_categories: List[str] = Field(default_factory=list)
    budget_overrides: Dict[str, Any] = Field(default_factory=dict)
    externally_visible: bool = False
    trace_context: Optional[OpenAITraceContext] = None

    @root_validator(pre=False, allow_reuse=True)
    def validate_plan_graph(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        nodes = list(values.get("nodes") or [])
        node_map = {node.node_id: node for node in nodes}
        if len(node_map) != len(nodes):
            raise ValueError("every node_id must be unique")
        for root_id in values.get("root_node_ids") or []:
            if root_id not in node_map:
                raise ValueError("every root_node_id must exist in nodes")
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visited:
                return
            if node_id in visiting:
                raise ValueError("execution plan graph must be acyclic")
            visiting.add(node_id)
            node = node_map[node_id]
            for dep_id in node.dependencies:
                if dep_id not in node_map:
                    raise ValueError(f"unknown dependency: {dep_id}")
                visit(dep_id)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in node_map:
            visit(node_id)
        produced_keys = {node.output_key for node in nodes if node.output_key}
        for output_key in values.get("terminal_output_keys") or []:
            if output_key not in produced_keys:
                raise ValueError("every terminal_output_key must be produced by a reachable node")
        terminal_nodes = set((values.get("verification_plan") or VerificationPlan(mode="none")).terminal_nodes)
        for terminal_node in terminal_nodes:
            if terminal_node not in node_map:
                raise ValueError("VerificationPlan.terminal_nodes must reference existing node_id values")
        branch_groups: Dict[str, List[str]] = {}
        for node in nodes:
            if node.branch_group_id:
                branch_groups.setdefault(node.branch_group_id, []).append(node.node_id)
        for node in nodes:
            if node.node_kind == "merge":
                if not node.consumes_branch_group_id:
                    raise ValueError("every merge node must consume exactly one declared branch group")
                if node.consumes_branch_group_id not in branch_groups:
                    raise ValueError("merge nodes must consume an existing branch group")
        return values
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
class BranchBudget(BaseModel):
    model_call_budget: int = 0
    checker_budget: int = 0
    latency_budget_s: float = 0.0
    tool_synthesis_allowed: bool = False


class BranchPlan(BaseModel):
    branch_id: str
    parent_frame_id: str
    request_id: str
    trace_context: Optional[OpenAITraceContext] = None
    assigned_node_ids: List[str] = Field(default_factory=list)
    merge_priority: int = 0
    reserved_budget: BranchBudget = Field(default_factory=BranchBudget)
    cancel_on_parent_stop: bool = True


class BranchPublication(BaseModel):
    publication_id: str
    publication_kind: Literal[
        "candidate_artifact",
        "verifier_evidence",
        "trace_rows",
        "budget_usage",
        "handle_refs",
        "cleanup_record",
    ]
    logical_key: str
    sequence_no: int
    accepted: bool = False
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
    summary: str = ""
    created_at: float = 0.0


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


class CheckpointEnvelope(BaseModel):
    checkpoint_id: str
    runtime_abi: str
    storage_schema_version: str
    runtime_hash: str
    request_id: str
    plan_id: str
    task_id: Optional[str] = None
    seed: int = 0
    queued_frames: List[Dict[str, Any]] = Field(default_factory=list)
    branch_state: Dict[str, BranchState] = Field(default_factory=dict)
    branch_publications: List[BranchPublication] = Field(default_factory=list)
    unresolved_goals: List[str] = Field(default_factory=list)
    artifact_refs: List[str] = Field(default_factory=list)
    handle_refs: List[str] = Field(default_factory=list)
    budget_state: Dict[str, Any] = Field(default_factory=dict)
    verifier_state: Dict[str, Any] = Field(default_factory=dict)
    working_state_summary: Dict[str, Any] = Field(default_factory=dict)
    trace_cursor: Dict[str, Any] = Field(default_factory=dict)
    side_effect_receipts: List[SideEffectReceipt] = Field(default_factory=list)


class RuntimeSolveRequest(BaseModel):
    request_id: str
    runtime_backend: str
    mode: Literal["benchmark", "user_request"]
    seed: int = 0
    task: Optional["BenchmarkTask"] = None
    solve_request: Optional[SolveRequest] = None
    trace_context: Optional[OpenAITraceContext] = None
    budget_overrides: Dict[str, Any] = Field(default_factory=dict)
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
class RunResult(BaseModel):
    task_id: str
    seed: int
    artifact: Any
    verifier_score: float
=======
class RunResult(BaseModel):
    request_id: str
    task_id: str
    seed: int
    plan_id: str = ""
    artifact: Any
    verifier_score: float
    trace_context: Optional[OpenAITraceContext] = None
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
class CheckpointReference(BaseModel):
    ref: str
    task_id: str
    seed: int
    checkpoint_count: int = 0
=======
class CheckpointReference(BaseModel):
    ref: str
    checkpoint_id: str
    request_id: str
    plan_id: str
    task_id: str
    seed: int
    runtime_hash: Optional[str] = None
    checkpoint_count: int = 0
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
    trace_context: Optional[OpenAITraceContext] = None
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
=======
class CapabilityExchange(BaseModel):
    runtime_abi: str
    kernel_version: str
    storage_schema_version: str
    supported_backends: List[str] = Field(default_factory=list)
    tool_runtimes: List[str] = Field(default_factory=list)
    checkpoint_support: bool = True
    resume_support: bool = True
    trace_context_support: bool = True
    execution_plan_schema_versions: List[str] = Field(default_factory=lambda: ["agintor.execution-plan.v1"])
    runtime_entry_commands: List[str] = Field(default_factory=lambda: ["inspect", "solve", "run-batch", "resume"])
    supported_reconciliation_policies: List[str] = Field(default_factory=lambda: ["strict", "best_effort"])
    runtime_asset_capabilities: Dict[str, bool] = Field(default_factory=dict)
    side_effect_receipts: bool = True
    isolation_capabilities: List[RuntimeIsolationBackendCapabilities] = Field(default_factory=list)
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
    trace_context: Optional[OpenAITraceContext] = None


class RuntimeBatchRequest(BaseModel):
    request_id: str
    runtime_backend: str
    trace_context: Optional[OpenAITraceContext] = None
    budget_overrides: Dict[str, Any] = Field(default_factory=dict)
    invocations: List[RuntimeTaskInvocation] = Field(default_factory=list)
>>>>>>> REPLACE
```

## `agintor/runtime_api.py`

Notes:
- Stop treating user solve requests as synthetic benchmark tasks. Keep the pattern heuristics, but turn them into deterministic plan templates.
- Move request normalization and trace-context derivation here so host, entrypoint, and runner all call the same builders.
- `PolicyContext` and `AgentFrame` should carry plan identity and typed trace context directly.

```text
<<<<<<< SEARCH
from .schemas import (
    AgentTemplate,
    BenchmarkTask,
    CapabilityExchange,
    Checkpoint,
    InspectRequest,
    ModelResponse,
    OperationSpec,
    RunResult,
    RuntimeBatchRequest,
    RuntimeSolveResponse,
    RuntimeSolveRequest,
    RuntimeTaskInvocation,
    SolveRequest,
    SolveResult,
)
=======
from .schemas import (
    AgentTemplate,
    BenchmarkTask,
    BranchPlan,
    CapabilityExchange,
    Checkpoint,
    CheckpointReference,
    ExecutionFlags,
    ExecutionPlan,
    InputBinding,
    InspectRequest,
    ModelResponse,
    OpenAITraceContext,
    OperationSpec,
    PlanNode,
    PlanOrigin,
    ResumeRequest,
    RunResult,
    RuntimeBatchRequest,
    RuntimeSolveResponse,
    RuntimeSolveRequest,
    RuntimeTaskInvocation,
    SolveRequest,
    SolveResult,
    VerificationPlan,
)
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
class AgentFrame:
    agent: AgentTemplate
    objective: str
    operation_ids: list[str]
    depth: int
    checkpoint: Checkpoint | None = None
    parent_id: str | None = None
    worker_id: str | None = None
    role: str = "root"
=======
class AgentFrame:
    frame_id: str
    plan_id: str
    agent: AgentTemplate
    objective: str
    node_ids: list[str]
    depth: int
    checkpoint: Checkpoint | None = None
    parent_id: str | None = None
    worker_id: str | None = None
    branch_id: str | None = None
    role: str = "root"
    trace_context: OpenAITraceContext = field(default_factory=OpenAITraceContext)
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
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
class PolicyContext:
    runtime_dir: Path
    shell: Any
    task: BenchmarkTask
    plan: ExecutionPlan
    provider: ModelProvider
    seed: int
    state: RuntimeState
    budget: RuntimeBudget
    trace: list[dict[str, Any]]
    objective: str
    trace_context: OpenAITraceContext
    profile: RuntimeProfile | None = None

    def derive_trace_context(self, **updates: Any) -> OpenAITraceContext:
        payload = self.trace_context.dict(exclude_none=True)
        payload.update({key: value for key, value in updates.items() if value is not None})
        return OpenAITraceContext(**payload)
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
def normalize_benchmark_request_id(task_id: str, seed: int) -> str:
    return f"benchmark.{task_id}.seed_{int(seed)}"


def build_runtime_trace_context(
    *,
    request_id: str,
    provider_role: str = "runtime",
    runtime_dir: str | None = None,
    runtime_hash: str | None = None,
    task_id: str | None = None,
    seed: int | None = None,
    objective: str | None = None,
    session_id: str | None = None,
) -> OpenAITraceContext:
    return OpenAITraceContext(
        session_id=session_id,
        provider_role=provider_role,
        runtime_dir=runtime_dir,
        runtime_hash=runtime_hash,
        task_id=task_id,
        seed=seed,
        request_id=request_id,
        objective=objective,
    )


def derive_trace_context(parent: OpenAITraceContext | None, **updates: Any) -> OpenAITraceContext:
    payload = {} if parent is None else parent.dict(exclude_none=True)
    payload.update({key: value for key, value in updates.items() if value is not None})
    return OpenAITraceContext(**payload)


def _execution_plan(
    *,
    request_id: str,
    origin: PlanOrigin,
    objective: str,
    nodes: list[PlanNode],
    terminal_output_keys: list[str],
    verification_plan: VerificationPlan,
    execution_flags: ExecutionFlags,
    allowed_tool_categories: list[str],
    budget_overrides: dict[str, Any],
    context_refs: list[str],
    file_refs: list[str],
    plan_constants: dict[str, Any],
    trace_context: OpenAITraceContext,
) -> ExecutionPlan:
    payload = {
        "request_id": request_id,
        "origin": origin.dict(),
        "objective": objective,
        "nodes": [node.dict() for node in nodes],
        "root_node_ids": [node.node_id for node in nodes if not node.dependencies],
        "terminal_output_keys": terminal_output_keys,
        "verification_plan": verification_plan.dict(),
        "execution_flags": execution_flags.dict(),
        "allowed_tool_categories": allowed_tool_categories,
        "budget_overrides": budget_overrides,
        "context_refs": context_refs,
        "file_refs": file_refs,
        "plan_constants": plan_constants,
        "externally_visible": any(node.externally_visible for node in nodes),
        "trace_context": trace_context.dict(exclude_none=True),
    }
    digest = stable_hash(payload)
    return ExecutionPlan(
        plan_digest=digest,
        plan_id=f"plan.{digest[:12]}",
        **payload,
    )


def compile_execution_plan_from_task_invocation(
    invocation: RuntimeTaskInvocation,
    *,
    runtime_dir: str,
    runtime_hash: str,
) -> ExecutionPlan:
    task = invocation.task
    trace_context = derive_trace_context(
        invocation.trace_context,
        provider_role="runtime",
        runtime_dir=runtime_dir,
        runtime_hash=runtime_hash,
        task_id=task.task_id,
        seed=invocation.seed,
        request_id=invocation.request_id,
        objective=task.prompt,
    )
    nodes = [
        PlanNode(
            node_id=operation.op_id,
            node_kind="builtin_op" if operation.kind == "builtin" else operation.kind,
            instruction=operation.description,
            output_key=operation.output_key,
            dependencies=list(operation.dependencies),
            tool_hint=operation.tool_hint,
            allowed_tool_categories=list(task.allowed_tool_categories),
            static_args=dict(operation.args),
            verification_required=task.verification_required,
            externally_visible=operation.externally_visible or task.externally_visible,
            frame_role="root",
            metadata={"source_op_id": operation.op_id},
        )
        for operation in task.operations
    ]
    return _execution_plan(
        request_id=invocation.request_id,
        origin=PlanOrigin(
            origin_kind="benchmark",
            source_task_id=task.task_id,
            source_request_id=invocation.request_id,
            source_suite=str(task.metadata.get("suite", "")),
            adapter_kind="benchmark_task.operations.v1",
            adaptation_assumptions=[],
        ),
        objective=task.prompt,
        nodes=nodes,
        terminal_output_keys=[operation.output_key for operation in task.operations],
        verification_plan=VerificationPlan(
            mode=task.verifier_type,
            required=task.verification_required,
            checker_ladder=["local", "subtree", "repo", "benchmark"],
            exact_verifier_required=task.verifier_type not in {"", "none"},
            artifact_contract={"expected": task.expected},
            terminal_nodes=[operation.op_id for operation in task.operations],
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
        context_refs=[stable_hash(item)[:12] for item in task.context_items],
        file_refs=list(task.file_paths),
        plan_constants={},
        trace_context=trace_context,
    )
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
    request_id: str | None,
    runtime_backend: str,
    seed: int,
    task: BenchmarkTask,
    runtime_dir: str | None = None,
    session_id: str | None = None,
    budget_overrides: dict[str, Any] | None = None,
) -> RuntimeSolveRequest:
    normalized_request_id = request_id or normalize_benchmark_request_id(task.task_id, seed)
    return RuntimeSolveRequest(
        request_id=normalized_request_id,
        runtime_backend=runtime_backend,
        mode="benchmark",
        seed=int(seed),
        task=task,
        trace_context=build_runtime_trace_context(
            request_id=normalized_request_id,
            runtime_dir=runtime_dir,
            task_id=task.task_id,
            seed=seed,
            objective=task.prompt,
            session_id=session_id,
        ),
        budget_overrides=dict(budget_overrides or {}),
    )
>>>>>>> REPLACE
```

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
    runtime_dir: str | None = None,
    session_id: str | None = None,
) -> RuntimeSolveRequest:
    trace_context = derive_trace_context(
        solve_request.trace_context,
        provider_role="runtime",
        runtime_dir=runtime_dir,
        request_id=solve_request.request_id,
        seed=seed,
        objective=solve_request.prompt,
        session_id=session_id,
    )
    solve_request = SolveRequest(**{**solve_request.dict(), "trace_context": trace_context})
    return RuntimeSolveRequest(
        request_id=solve_request.request_id,
        runtime_backend=runtime_backend,
        mode="user_request",
        seed=int(seed),
        solve_request=solve_request,
        trace_context=trace_context,
        budget_overrides=dict(solve_request.budget_overrides),
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
    runtime_dir: str | None = None,
    session_id: str | None = None,
    budget_overrides: dict[str, Any] | None = None,
) -> RuntimeBatchRequest:
    parent_trace_context = build_runtime_trace_context(
        request_id=request_id,
        runtime_dir=runtime_dir,
        session_id=session_id,
        objective="batch",
    )
    invocations: list[RuntimeTaskInvocation] = []
    for task, seed in task_runs:
        invocation_request_id = normalize_benchmark_request_id(task.task_id, seed)
        invocations.append(
            RuntimeTaskInvocation(
                request_id=invocation_request_id,
                seed=int(seed),
                task=task,
                trace_context=derive_trace_context(
                    parent_trace_context,
                    request_id=invocation_request_id,
                    task_id=task.task_id,
                    seed=seed,
                    objective=task.prompt,
                ),
            )
        )
    return RuntimeBatchRequest(
        request_id=request_id,
        runtime_backend=runtime_backend,
        trace_context=parent_trace_context,
        budget_overrides=dict(budget_overrides or {}),
        invocations=invocations,
    )


def runtime_resume_request(
    *,
    checkpoint_ref: CheckpointReference,
    request_id: str | None = None,
    session_id: str | None = None,
    reconciliation_policy: str = "strict",
) -> ResumeRequest:
    normalized_request_id = request_id or checkpoint_ref.request_id
    return ResumeRequest(
        request_id=normalized_request_id,
        checkpoint_ref=checkpoint_ref,
        trace_context=build_runtime_trace_context(
            request_id=normalized_request_id,
            provider_role="runtime",
            runtime_hash=checkpoint_ref.runtime_hash,
            task_id=checkpoint_ref.task_id,
            seed=checkpoint_ref.seed,
            session_id=session_id,
        ),
        reconciliation_policy=reconciliation_policy,
    )
>>>>>>> REPLACE
```

## `agintor/runtime_host.py`

Notes:
- Host stays responsible for CLI parsing, benchmark lookup, prompt-file loading, request-envelope construction, and transport.
- Host must no longer infer runtime behavior by synthesizing benchmark tasks locally. It should use runtime-api preview helpers that return runtime-native plans or provider-need summaries.
- Add `resume()` beside `solve()` and `run_batch()` instead of inventing a side restore path.

```text
<<<<<<< SEARCH
from .runtime_api import inspect_request_for_runtime, runtime_batch_request_for_tasks, solve_request_to_task
=======
from .runtime_api import (
    inspect_request_for_runtime,
    runtime_batch_request_for_tasks,
    runtime_resume_request,
)
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
from .schemas import BenchmarkTask, CapabilityExchange, RuntimeBatchResponse, RuntimeSolveRequest, RuntimeSolveResponse
=======
from .schemas import (
    BenchmarkTask,
    CapabilityExchange,
    CheckpointReference,
    ResumeRequest,
    RuntimeBatchResponse,
    RuntimeSolveRequest,
    RuntimeSolveResponse,
)
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
    def run_batch(
        self,
        runtime_dir: str | Path,
        task_runs: list[tuple[object, int]],
        *,
        provider: ModelProvider,
        runtime_profile: object | None = None,
        budget_overrides: Mapping[str, Any] | None = None,
    ) -> RuntimeBatchResponse:
        capability_exchange = self.inspect(runtime_dir)
        request = runtime_batch_request_for_tasks(
            request_id=f"run.{stable_hash(runtime_dir, len(task_runs), self.runtime_backend)[:12]}",
            runtime_backend=self.runtime_backend,
            task_runs=task_runs,
            budget_overrides=dict(budget_overrides or {}),
        )
=======
    def run_batch(
        self,
        runtime_dir: str | Path,
        task_runs: list[tuple[object, int]],
        *,
        provider: ModelProvider,
        runtime_profile: object | None = None,
        budget_overrides: Mapping[str, Any] | None = None,
        session_id: str | None = None,
    ) -> RuntimeBatchResponse:
        capability_exchange = self.inspect(runtime_dir)
        request = runtime_batch_request_for_tasks(
            request_id=f"batch.{stable_hash(runtime_dir, len(task_runs), self.runtime_backend)[:12]}",
            runtime_backend=self.runtime_backend,
            task_runs=task_runs,
            runtime_dir=str(Path(runtime_dir).resolve()),
            session_id=session_id,
            budget_overrides=dict(budget_overrides or {}),
        )
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
        failed = response.solve_result.status in {"failed", "controlled_failure"} or bool(response.solve_result.faults.get("hard_invalid"))
        self._prune_solve_result_artifacts(response, failed=failed)
        return response
=======
        failed = response.solve_result.status in {"failed", "controlled_failure"} or bool(response.solve_result.faults.get("hard_invalid"))
        self._prune_solve_result_artifacts(response, failed=failed)
        return response

    def resume(
        self,
        runtime_dir: str | Path,
        checkpoint_ref: CheckpointReference,
        *,
        provider: ModelProvider,
        runtime_profile: object | None = None,
        request_id: str | None = None,
        session_id: str | None = None,
        reconciliation_policy: str = "strict",
    ) -> RuntimeSolveResponse:
        capability_exchange = self.inspect(runtime_dir)
        if not capability_exchange.resume_support:
            raise RuntimeLoadError(f"runtime {runtime_dir} does not advertise resume support")
        request = runtime_resume_request(
            checkpoint_ref=checkpoint_ref,
            request_id=request_id,
            session_id=session_id,
            reconciliation_policy=reconciliation_policy,
        )
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
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
    @classmethod
    def _request_requires_default_provider(cls, request: RuntimeSolveRequest) -> bool:
        if request.mode == "benchmark":
            return bool(request.task) and cls._task_requires_default_provider(request.task)
        if request.solve_request is None:
            return False
        if cls._task_requires_default_provider(solve_request_to_task(request.solve_request)):
            return True
        return cls._request_may_trigger_default_provider_side_paths(request)
=======
    @classmethod
    def _request_requires_default_provider(cls, request: RuntimeSolveRequest) -> bool:
        if request.mode == "benchmark":
            return bool(request.task) and cls._task_requires_default_provider(request.task)
        if request.solve_request is None:
            return False
        prompt_lower = request.solve_request.prompt.lower()
        if any(token in prompt_lower for token in ("direct answer", "respond", "reply", "summarize", "explain")):
            return True
        return cls._request_may_trigger_default_provider_side_paths(request)
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
        response = model_validate(RuntimeSolveResponse, json.loads(output_json.read_text(encoding="utf-8")))
        failed = response.solve_result.status in {"failed", "controlled_failure"} or bool(response.solve_result.faults.get("hard_invalid"))
        self._cleanup_run_dir(run_dir, failed=failed)
        return response
=======
        response = model_validate(RuntimeSolveResponse, json.loads(output_json.read_text(encoding="utf-8")))
        failed = response.solve_result.status in {"failed", "controlled_failure"} or bool(response.solve_result.faults.get("hard_invalid"))
        self._cleanup_run_dir(run_dir, failed=failed)
        return response

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
>>>>>>> REPLACE
```

## `agintor/runtime_sdk/runtime_entry.py`

Notes:
- Keep `inspect`, `solve`, and `run-batch` surface names.
- Add `resume`.
- Compile plans inside the runtime entrypoint after the runtime is loaded so runtime identity can be stamped into trace context before any provider call.
- Stop grouping batch runners only by seed. Group only explicit transfer-scored episodes; otherwise each invocation gets its own shell and runtime state.

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
    compile_execution_plan_from_solve_request,
    compile_execution_plan_from_task_invocation,
    runtime_solve_failure_response,
    solve_result_from_run_result_with_context,
)
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
from .schemas import (
    BenchmarkTask,
    CapabilityExchange,
    InspectRequest,
    RunResult,
    RuntimeBatchRequest,
    RuntimeBatchResponse,
    RuntimeSolveRequest,
    RuntimeSolveResponse,
    RuntimeTaskInvocation,
    SolveRequest,
)
=======
from .schemas import (
    BenchmarkTask,
    CapabilityExchange,
    InspectRequest,
    ResumeRequest,
    RunResult,
    RuntimeBatchRequest,
    RuntimeBatchResponse,
    RuntimeSolveRequest,
    RuntimeSolveResponse,
    RuntimeTaskInvocation,
    SolveRequest,
)
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
def _run_batch(args: argparse.Namespace) -> int:
    request = model_validate(
        RuntimeBatchRequest,
        json.loads(Path(args.input_json).read_text(encoding="utf-8")),
    )
    runtime_profile = load_runtime_profile(args.runtime_dir, profile_path=args.profile_json)
    provider_payload_data = json.loads(Path(args.provider_json).read_text(encoding="utf-8"))
    provider = build_provider_from_payload(provider_payload_data, provider_profile=runtime_profile.runtime_provider)
    runtime = load_runtime(
        args.runtime_dir,
        runtime_profile=runtime_profile,
        runtime_backend=request.runtime_backend,
    )
    results: list[RunResult] = []
    runners_by_seed: dict[int, TaskRuntime] = {}
    for invocation_payload in request.invocations:
        invocation = model_validate(RuntimeTaskInvocation, model_dump(invocation_payload))
        runner = runners_by_seed.get(invocation.seed)
        if runner is None:
            shell = FixedShell(
                Path(args.workspace) / f"seed_{invocation.seed}",
                artifact_mode=ArtifactMode(args.artifact_mode),
            )
            runner = TaskRuntime(
                runtime,
                shell,
                provider,
                budget_overrides=request.budget_overrides,
                runtime_profile=runtime_profile,
            )
            runners_by_seed[invocation.seed] = runner
        results.append(runner.run_task(model_validate(BenchmarkTask, model_dump(invocation.task)), invocation.seed))
=======
def _run_batch(args: argparse.Namespace) -> int:
    request = model_validate(
        RuntimeBatchRequest,
        json.loads(Path(args.input_json).read_text(encoding="utf-8")),
    )
    runtime_profile = load_runtime_profile(args.runtime_dir, profile_path=args.profile_json)
    provider_payload_data = json.loads(Path(args.provider_json).read_text(encoding="utf-8"))
    provider = build_provider_from_payload(provider_payload_data, provider_profile=runtime_profile.runtime_provider)
    runtime = load_runtime(
        args.runtime_dir,
        runtime_profile=runtime_profile,
        runtime_backend=request.runtime_backend,
    )
    results: list[RunResult] = []
    grouped_invocations: dict[str, list[RuntimeTaskInvocation]] = {}
    for invocation_payload in request.invocations:
        invocation = model_validate(RuntimeTaskInvocation, model_dump(invocation_payload))
        group_key = invocation.task.episode_id if invocation.task.transfer_scored and invocation.task.episode_id else invocation.request_id
        grouped_invocations.setdefault(group_key, []).append(invocation)
    for group_key, invocations in grouped_invocations.items():
        shell = FixedShell(
            Path(args.workspace) / group_key.replace("/", "_"),
            artifact_mode=ArtifactMode(args.artifact_mode),
        )
        runner = TaskRuntime(
            runtime,
            shell,
            provider,
            budget_overrides=request.budget_overrides,
            runtime_profile=runtime_profile,
        )
        for invocation in invocations:
            plan = compile_execution_plan_from_task_invocation(
                invocation,
                runtime_dir=str(Path(args.runtime_dir).resolve()),
                runtime_hash=runtime.runtime_hash,
            )
            results.append(runner.run_plan(plan, invocation.seed))
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
def _solve(args: argparse.Namespace) -> int:
    request = model_validate(
        RuntimeSolveRequest,
        json.loads(Path(args.input_json).read_text(encoding="utf-8")),
    )
    runtime_profile = load_runtime_profile(args.runtime_dir, profile_path=args.profile_json)
    runtime = load_runtime(
        args.runtime_dir,
        runtime_profile=runtime_profile,
        runtime_backend=request.runtime_backend,
    )
    capability_exchange = CapabilityExchange(**model_dump(runtime.capability_exchange))
    if request.mode == "benchmark":
        if request.task is None:
            raise ValueError("benchmark solve requires a task payload")
        task = model_validate(BenchmarkTask, model_dump(request.task))
        solve_request = benchmark_task_to_solve_request(task, request_id=request.request_id)
    else:
        if request.solve_request is None:
            raise ValueError("user_request solve requires a solve_request payload")
        solve_request = model_validate(SolveRequest, model_dump(request.solve_request))
        task = solve_request_to_task(solve_request)
=======
def _solve(args: argparse.Namespace) -> int:
    request = model_validate(
        RuntimeSolveRequest,
        json.loads(Path(args.input_json).read_text(encoding="utf-8")),
    )
    runtime_profile = load_runtime_profile(args.runtime_dir, profile_path=args.profile_json)
    runtime = load_runtime(
        args.runtime_dir,
        runtime_profile=runtime_profile,
        runtime_backend=request.runtime_backend,
    )
    capability_exchange = CapabilityExchange(**model_dump(runtime.capability_exchange))
    if request.mode == "benchmark":
        if request.task is None:
            raise ValueError("benchmark solve requires a task payload")
        task = model_validate(BenchmarkTask, model_dump(request.task))
        solve_request = benchmark_task_to_solve_request(task, request_id=request.request_id)
        plan = compile_execution_plan_from_task_invocation(
            RuntimeTaskInvocation(
                request_id=request.request_id,
                seed=request.seed,
                task=task,
                trace_context=request.trace_context,
            ),
            runtime_dir=str(Path(args.runtime_dir).resolve()),
            runtime_hash=runtime.runtime_hash,
        )
    else:
        if request.solve_request is None:
            raise ValueError("user_request solve requires a solve_request payload")
        solve_request = model_validate(SolveRequest, model_dump(request.solve_request))
        plan = compile_execution_plan_from_solve_request(
            solve_request,
            runtime_dir=str(Path(args.runtime_dir).resolve()),
            runtime_hash=runtime.runtime_hash,
        )
        task = model_validate(BenchmarkTask, model_dump(request.task)) if request.task is not None else BenchmarkTask(
            task_id=f"user-plan.{request.request_id}",
            family="e2e",
            prompt=solve_request.prompt,
            task_type="user_request",
            operations=[],
            expected=None,
            verifier_type=plan.verification_plan.mode,
            verification_required=plan.verification_plan.required,
            allow_best_effort=plan.execution_flags.allow_best_effort,
        )
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
        run_result = runner.run_task(task, request.seed)
=======
        run_result = runner.run_plan(plan, request.seed)
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
    Path(args.output_json).write_text(json.dumps(model_dump(response), indent=2, sort_keys=True), encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
=======
    Path(args.output_json).write_text(json.dumps(model_dump(response), indent=2, sort_keys=True), encoding="utf-8")
    return 0


def _resume(args: argparse.Namespace) -> int:
    request = model_validate(
        ResumeRequest,
        json.loads(Path(args.input_json).read_text(encoding="utf-8")),
    )
    runtime_profile = load_runtime_profile(args.runtime_dir, profile_path=args.profile_json)
    runtime = load_runtime(
        args.runtime_dir,
        runtime_profile=runtime_profile,
        runtime_backend="local" if args.runtime_dir else "local",
    )
    capability_exchange = CapabilityExchange(**model_dump(runtime.capability_exchange))
    provider_payload_data = json.loads(Path(args.provider_json).read_text(encoding="utf-8"))
    provider = build_provider_from_payload(provider_payload_data, provider_profile=runtime_profile.runtime_provider)
    shell = FixedShell(
        Path(args.workspace) / f"resume_{request.request_id}",
        artifact_mode=ArtifactMode(args.artifact_mode),
    )
    runner = TaskRuntime(
        runtime,
        shell,
        provider,
        runtime_profile=runtime_profile,
    )
    response = RuntimeSolveResponse(
        request_id=request.request_id,
        capability_exchange=capability_exchange,
        solve_result=solve_result_from_run_result_with_context(
            benchmark_task_to_solve_request(
                BenchmarkTask(
                    task_id=request.checkpoint_ref.task_id,
                    family="e2e",
                    prompt=request.request_id,
                    task_type="resume",
                    operations=[],
                    expected=None,
                    verifier_type="none",
                ),
                request_id=request.request_id,
            ),
            runner.resume_from_checkpoint(request),
            runtime.runtime_hash,
            mode="user_request",
            provider_usage=provider.usage_summary(),
        ),
    )
    Path(args.output_json).write_text(json.dumps(model_dump(response), indent=2, sort_keys=True), encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
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

```text
<<<<<<< SEARCH
    if args.command == "solve":
        return _solve(args)
    raise ValueError(args.command)
=======
    if args.command == "solve":
        return _solve(args)
    if args.command == "resume":
        return _resume(args)
    raise ValueError(args.command)
>>>>>>> REPLACE
```

## `agintor/runtime_loader.py` and `agintor/runtime_sdk/bundle.py`

Notes:
- Bump ABI/storage here so the rest of the codebase imports one authoritative version line.
- Capability exchange should advertise resume, execution-plan, trace-context, and isolation capabilities.
- Deployment-contract validation should reject unsupported required guarantees before execution starts.

```text
<<<<<<< SEARCH
RUNTIME_ABI_VERSION = "agintor-runtime-abi-v3"
=======
RUNTIME_ABI_VERSION = "agintor-runtime-abi-v4"
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
from .schemas import CapabilityExchange, DeploymentContract, KernelManifest, RuntimeManifest
=======
from .schemas import (
    CapabilityExchange,
    DeploymentContract,
    KernelManifest,
    RuntimeIsolationBackendCapabilities,
    RuntimeIsolationPolicy,
    RuntimeManifest,
)
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
def _validate_deployment_contract(
    runtime_path: Path,
    contract: DeploymentContract,
    *,
    runtime_backend: str | None = None,
    require_env_names: bool = False,
) -> None:
=======
def _validate_deployment_contract(
    runtime_path: Path,
    contract: DeploymentContract,
    *,
    runtime_backend: str | None = None,
    require_env_names: bool = False,
) -> None:
    isolation_policy = contract.runtime_isolation_policy or RuntimeIsolationPolicy(
        workspace_root=".",
        environment_allowlist=list(contract.environment_allowlist),
        network_policy=contract.network_policy,
        filesystem_policy=contract.filesystem_policy,
    )
    backend_guarantees = {
        "local": {
            "timeout_enforcement",
            "workspace_isolation",
            "environment_filtering",
            "process_cleanup",
        },
        "docker": {
            "timeout_enforcement",
            "workspace_isolation",
            "environment_filtering",
            "process_cleanup",
            "network_disablement",
        },
    }
    if runtime_backend is not None:
        missing_required = sorted(set(isolation_policy.required_guarantees) - backend_guarantees.get(str(runtime_backend).strip().lower(), set()))
        if missing_required:
            raise RuntimeLoadError(
                f"runtime backend {runtime_backend!r} cannot satisfy required isolation guarantees for {runtime_path}: {', '.join(missing_required)}"
            )
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
    isolation_capabilities = [
        RuntimeIsolationBackendCapabilities(
            backend="local",
            effective_guarantees=[
                "timeout_enforcement",
                "workspace_isolation",
                "environment_filtering",
                "process_cleanup",
            ],
            unsupported_guarantees=["network_disablement"],
            notes=["development backend", "best-effort process cleanup only"],
        ),
        RuntimeIsolationBackendCapabilities(
            backend="docker",
            effective_guarantees=[
                "timeout_enforcement",
                "workspace_isolation",
                "environment_filtering",
                "process_cleanup",
                "network_disablement",
            ],
            unsupported_guarantees=[],
            notes=["required when deployment contract demands network disablement"],
        ),
    ]
    capability_exchange = CapabilityExchange(
        runtime_abi=RUNTIME_ABI_VERSION,
        kernel_version=kernel_manifest.kernel_version,
        storage_schema_version=kernel_manifest.storage_schema_version,
        supported_backends=list(deployment_contract.supported_backends),
        tool_runtimes=["python"],
        checkpoint_support=True,
        resume_support=True,
        trace_context_support=True,
        execution_plan_schema_versions=["agintor.execution-plan.v1"],
        runtime_entry_commands=["inspect", "solve", "run-batch", "resume"],
        supported_reconciliation_policies=["strict", "best_effort"],
        runtime_asset_capabilities={
            "traces": True,
            "checkpoints": True,
            "checkpoint_envelopes": True,
            "runtime_sdk": True,
        },
        side_effect_receipts=True,
        isolation_capabilities=isolation_capabilities,
        required_env_names=list(deployment_contract.required_env_names),
        required_env_any_of=[list(group) for group in deployment_contract.required_env_any_of],
        capability_flags=list(deployment_contract.capability_flags or kernel_manifest.capability_flags),
    )
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
STORAGE_SCHEMA_VERSION = "agintor-storage-v1"
KERNEL_CAPABILITY_FLAGS = [
    "inspect",
    "run_batch",
    "checkpoint_refs",
    "provider_usage",
    "trace_refs",
]
=======
STORAGE_SCHEMA_VERSION = "agintor-storage-v2"
KERNEL_CAPABILITY_FLAGS = [
    "inspect",
    "run_batch",
    "resume",
    "execution_plan_v1",
    "checkpoint_refs",
    "checkpoint_envelopes",
    "provider_usage",
    "trace_refs",
    "side_effect_receipts",
    "runtime_isolation",
]
>>>>>>> REPLACE
```

## `agintor/container_runtime.py`

Notes:
- Docker transport should mirror local transport exactly: inspect, solve, run-batch, resume.
- Keep the existing packaging path, but make runtime-wide isolation enforcement explicit. When required guarantees exceed `local`, do not silently fall back.

```text
<<<<<<< SEARCH
    def solve_protocol(
        self,
        runtime_dir: str | Path,
        request: RuntimeSolveRequest,
        *,
        provider: ModelProvider,
        runtime_profile: RuntimeProfile | None = None,
    ) -> RuntimeSolveResponse:
=======
    def solve_protocol(
        self,
        runtime_dir: str | Path,
        request: RuntimeSolveRequest,
        *,
        provider: ModelProvider,
        runtime_profile: RuntimeProfile | None = None,
    ) -> RuntimeSolveResponse:
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
from .schemas import (
    BenchmarkTask,
    CapabilityExchange,
    InspectRequest,
    RunResult,
    RuntimeBatchRequest,
    RuntimeBatchResponse,
    RuntimeSolveRequest,
    RuntimeSolveResponse,
    RuntimeTaskInvocation,
)
=======
from .schemas import (
    BenchmarkTask,
    CapabilityExchange,
    InspectRequest,
    ResumeRequest,
    RunResult,
    RuntimeBatchRequest,
    RuntimeBatchResponse,
    RuntimeSolveRequest,
    RuntimeSolveResponse,
    RuntimeTaskInvocation,
)
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
        response.solve_result.checkpoint_ref = self._host_workspace_path(response.solve_result.checkpoint_ref, workspace_dir)
=======
        response.solve_result.checkpoint_ref = self._host_workspace_path(response.solve_result.checkpoint_ref, workspace_dir)

    def resume_protocol(
        self,
        runtime_dir: str | Path,
        request: ResumeRequest,
        *,
        provider: ModelProvider,
        runtime_profile: RuntimeProfile | None = None,
    ) -> RuntimeSolveResponse:
        self.ensure_image()
        runtime_path = Path(runtime_dir).resolve()
        profile_payload = model_dump(runtime_profile) if runtime_profile is not None else None
        provider_config = provider_payload(provider)
        run_dir = ensure_directory(
            self.workspace
            / stable_hash("resume", runtime_path, model_dump(request), provider_config, profile_payload, self.base_image)[:12]
        )
        request_json = run_dir / "resume_request.json"
        profile_json = run_dir / "profile.json"
        provider_json = run_dir / "provider.json"
        output_json = run_dir / "resume_result.json"
        workspace_dir = ensure_directory(run_dir / "workspace")
        request_json.write_text(json.dumps(model_dump(request), indent=2, sort_keys=True), encoding="utf-8")
        if profile_payload is not None:
            profile_json.write_text(json.dumps(profile_payload, indent=2, sort_keys=True), encoding="utf-8")
        mounts = [
            f"{runtime_path}:/mnt/runtime:ro",
            f"{request_json.resolve()}:/mnt/resume_request.json:ro",
            f"{workspace_dir.resolve()}:/mnt/workspace",
            f"{output_json.parent.resolve()}:/mnt/output",
        ]
        if profile_payload is not None:
            mounts.append(f"{profile_json.resolve()}:/mnt/profile.json:ro")
        provider_file_map: dict[str, str] = {}
        for index, host_path_text in enumerate(provider_payload_file_paths(provider_config)):
            host_path = Path(host_path_text).resolve()
            container_path = f"/mnt/provider_files/{index}_{host_path.name}"
            mounts.append(f"{host_path}:{container_path}:ro")
            provider_file_map[host_path_text] = container_path
        provider_json.write_text(
            json.dumps(rewrite_provider_payload_file_paths(provider_config, provider_file_map), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        mounts.append(f"{provider_json.resolve()}:/mnt/provider.json:ro")
        command = ["docker", "run", "--rm", "-e", f"PYTHONPATH=/mnt/runtime/{KERNEL_BUNDLE_DIR}"]
        for env_name in provider_environment_names_for_instance(provider):
            env_value = os.environ.get(env_name)
            if env_value:
                command.extend(["-e", f"{env_name}={env_value}"])
        for mount in mounts:
            command.extend(["-v", mount])
        command.extend(
            [
                self.image_tag,
                "python",
                "-m",
                "agintor_runtime.runtime_entry",
                "resume",
                "--runtime-dir",
                "/mnt/runtime",
                "--input-json",
                "/mnt/resume_request.json",
                "--provider-json",
                "/mnt/provider.json",
                "--output-json",
                "/mnt/output/resume_result.json",
                "--workspace",
                "/mnt/workspace",
                "--artifact-mode",
                self.artifact_policy.mode.value,
            ]
        )
        if profile_payload is not None:
            command.extend(["--profile-json", "/mnt/profile.json"])
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "docker resume failed")
        response = model_validate(RuntimeSolveResponse, json.loads(output_json.read_text(encoding="utf-8")))
        self._rewrite_solve_response_paths(response, workspace_dir)
        failed = response.solve_result.status in {"failed", "controlled_failure"} or bool(response.solve_result.faults.get("hard_invalid"))
        self._cleanup_run_dir(run_dir, failed=failed)
        return response
>>>>>>> REPLACE
```

## Cross-File Implementation Notes

- `TaskRuntime` should gain `run_plan(plan, seed)` and `resume_from_checkpoint(resume_request)` rather than extending `run_task()` into another transport layer. `run_task()` can remain as the benchmark-only compatibility shim during the migration, but the runtime entrypoint and host should stop calling it directly.
- Batch execution should stop reusing one `TaskRuntime` per seed. Reuse is allowed only for invocations that share an explicit transfer-scored episode.
- `RunResult.request_id`, `RunResult.plan_id`, `CheckpointReference.request_id`, and `CheckpointReference.plan_id` are necessary for stable trace grouping and unambiguous resume.
- The plan compiler should stamp `runtime_hash` into `trace_context` only after `load_runtime()` succeeds. The host knows `runtime_dir`; the runtime entrypoint knows both `runtime_dir` and `runtime_hash`.
- `ExecutionPlan.trace_context`, `PolicyContext.trace_context`, `AgentFrame.trace_context`, `BranchPlan.trace_context`, and `BranchPublication` together give one typed correlation chain without reintroducing provider-specific dict payloads.
- The isolation model should be declarative in `DeploymentContract` and observed in `CapabilityExchange`. Backend enforcement belongs in `runtime_loader.py` and `container_runtime.py`, not in mutable runtime policy code.
