# Workstream 2 - Checkpoint Envelope, Receipt, and Resume Semantics

## Pre-implementation notes

- Keep `Checkpoint` as the mutable-policy-produced summary fragment. Persist restartable runtime state in a shell-owned `CheckpointEnvelope`.
- Treat checkpoints as append-only runtime artifacts emitted at deterministic boundaries, not as a single end-of-run summary dump.
- Record every replay-sensitive action with a stable `side_effect_id`, `action_fingerprint`, and `idempotency_key`. Resume reuses completed receipts, reconciles launched-but-incomplete work, and fails closed when strict reconciliation cannot prove safety.
- Reconstruct runtime state from queued frames, per-node status, branch reservations, accepted publications, artifacts, budgets, verifier state, message-board state, receipts, and trace cursor. Do not rebuild from summaries alone.
- Keep copy-in, publication-out branch isolation. Branches publish typed outputs only; parent state changes only through accepted publications and deterministic merge.

## Diff proposals

### File: `agintor/schemas.py`

<<<<<<< SEARCH
class AsyncHandle(BaseModel):
    handle_id: str
    tool_name: str
    sandbox_hash: str
    working_directory: str
    launch_time: float
    timeout: float
    stdout_path: Optional[str] = None
    stderr_path: Optional[str] = None
=======
class AsyncHandle(BaseModel):
    handle_id: str
    tool_name: str
    sandbox_hash: str
    working_directory: str
    launch_time: float
    timeout: float
    request_id: str = ""
    plan_id: str = ""
    node_id: str = ""
    branch_id: str = ""
    idempotency_key: str = ""
    stdout_path: Optional[str] = None
    stderr_path: Optional[str] = None
    result_ref: Optional[str] = None
    receipt_ids: List[str] = Field(default_factory=list)
>>>>>>> REPLACE

<<<<<<< SEARCH
class CheckpointReference(BaseModel):
    ref: str
    task_id: str = ""
    seed: int = 0
    request_id: str = ""
    plan_id: str = ""
    checkpoint_id: str = ""
    checkpoint_count: int = 0
    latest: bool = False
=======
class CheckpointBoundary(str, Enum):
    BEFORE_BRANCH_FANOUT = "before_branch_fanout"
    AFTER_BRANCH_PLAN_CREATION = "after_branch_plan_creation"
    AFTER_TOOL_OR_PROVIDER_LAUNCH = "after_tool_or_provider_launch"
    AFTER_TOOL_OR_PROVIDER_COMPLETION = "after_tool_or_provider_completion"
    BEFORE_MERGE = "before_merge"
    AFTER_MERGE = "after_merge"
    BEFORE_TERMINAL_RESULT = "before_terminal_result"


class ReceiptStatus(str, Enum):
    LAUNCHED = "launched"
    COMPLETED = "completed"
    FAILED = "failed"
    RECONCILED = "reconciled"
    CANCELLED = "cancelled"


class ReceiptReplayPolicy(str, Enum):
    REUSE_COMPLETED = "reuse_completed"
    RECONCILE_BEFORE_REISSUE = "reconcile_before_reissue"
    NEVER_REISSUE = "never_reissue"


class ReceiptReconciliationPolicy(str, Enum):
    NONE = "none"
    HANDLE_STATUS = "handle_status"
    FILESYSTEM_STATE = "filesystem_state"
    PROVIDER_IDEMPOTENCY = "provider_idempotency"
    SERVICE_STATUS = "service_status"


class BranchPublicationKind(str, Enum):
    CANDIDATE_ARTIFACT = "candidate_artifact"
    VERIFIER_EVIDENCE = "verifier_evidence"
    TRACE_ROWS = "trace_rows"
    BUDGET_USAGE = "budget_usage"
    HANDLE_REFERENCE = "handle_reference"
    CLEANUP_RECORD = "cleanup_record"
    RECEIPT_RECONCILIATION = "receipt_reconciliation"


class RecoveryFailureKind(str, Enum):
    CHECKPOINT_NOT_FOUND = "checkpoint_not_found"
    CHECKPOINT_CORRUPT = "checkpoint_corrupt"
    REQUEST_MISMATCH = "request_mismatch"
    RUNTIME_ABI_MISMATCH = "runtime_abi_mismatch"
    STORAGE_SCHEMA_MISMATCH = "storage_schema_mismatch"
    RUNTIME_HASH_MISMATCH = "runtime_hash_mismatch"
    PLAN_DIGEST_MISMATCH = "plan_digest_mismatch"
    BRANCH_STATE_INCOMPLETE = "branch_state_incomplete"
    RECEIPT_COMPLETION_MISSING = "receipt_completion_missing"
    RECEIPT_RECONCILIATION_FAILED = "receipt_reconciliation_failed"
    HANDLE_RECONCILIATION_FAILED = "handle_reconciliation_failed"
    ARTIFACT_RECONCILIATION_FAILED = "artifact_reconciliation_failed"
    TRACE_CURSOR_INVALID = "trace_cursor_invalid"


class CheckpointReference(BaseModel):
    ref: str
    task_id: str = ""
    seed: int = 0
    request_id: str = ""
    plan_id: str = ""
    checkpoint_id: str = ""
    sequence_no: int = 0
    boundary: CheckpointBoundary = CheckpointBoundary.BEFORE_TERMINAL_RESULT
    created_at: float = 0.0
    checkpoint_count: int = 0
    latest: bool = False
>>>>>>> REPLACE

<<<<<<< SEARCH
class BranchPublication(BaseModel):
    publication_id: str
    publication_kind: str
    logical_key: str
    sequence_no: int
    accepted: bool = False
    branch_id: str = ""
    trace_context: Optional[OpenAITraceContext] = None
    payload: Dict[str, Any] = Field(default_factory=dict)
=======
class BranchPublication(BaseModel):
    publication_id: str
    publication_kind: BranchPublicationKind
    logical_key: str
    sequence_no: int
    accepted: bool = False
    branch_id: str = ""
    trace_context: Optional[OpenAITraceContext] = None
    verifier_support: float = 0.0
    unresolved_critical: int = 0
    branch_rank: int = 0
    published_at: float = 0.0
    payload: Dict[str, Any] = Field(default_factory=dict)
>>>>>>> REPLACE

<<<<<<< SEARCH
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
    result_ref: Any = None
    replay_policy: str = "reuse_if_completed"
    reconciliation_policy: str = "strict"
    created_at: float = 0.0
=======
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
    node_id: str = ""
    request_digest: str
    backend: str
    status: ReceiptStatus
    result_ref: Any = None
    handle_id: str = ""
    replay_policy: ReceiptReplayPolicy = ReceiptReplayPolicy.REUSE_COMPLETED
    reconciliation_policy: ReceiptReconciliationPolicy = ReceiptReconciliationPolicy.NONE
    created_at: float = 0.0
    reconciled_at: float = 0.0
>>>>>>> REPLACE

<<<<<<< SEARCH
class CheckpointEnvelope(BaseModel):
    checkpoint_id: str
    runtime_abi: str
    storage_schema_version: str
    runtime_hash: str
    request_id: str
    plan_id: str
    task_id: str
    seed: int
    queued_frames: List[Dict[str, Any]] = Field(default_factory=list)
    plan_node_status: Dict[str, str] = Field(default_factory=dict)
    branch_state: List[BranchState] = Field(default_factory=list)
    branch_publications: List[BranchPublication] = Field(default_factory=list)
    unresolved_goals: List[str] = Field(default_factory=list)
    artifact_refs: Dict[str, Any] = Field(default_factory=dict)
    handle_or_job_refs: List[str] = Field(default_factory=list)
    budget_state: Dict[str, Any] = Field(default_factory=dict)
    verifier_state: Dict[str, Any] = Field(default_factory=dict)
    working_state_summary: Dict[str, Any] = Field(default_factory=dict)
    trace_cursor: Dict[str, Any] = Field(default_factory=dict)
    side_effect_receipts: List[SideEffectReceipt] = Field(default_factory=list)
=======
class CheckpointIdentity(BaseModel):
    checkpoint_id: str
    request_id: str
    plan_id: str
    task_id: str
    seed: int
    runtime_abi: str
    storage_schema_version: str
    runtime_hash: str
    sequence_no: int
    boundary: CheckpointBoundary
    created_at: float


class CheckpointStateSnapshot(BaseModel):
    queued_frames: List[Dict[str, Any]] = Field(default_factory=list)
    plan_node_status: Dict[str, str] = Field(default_factory=dict)
    branch_state: Dict[str, BranchState] = Field(default_factory=dict)
    accepted_publications: Dict[str, BranchPublication] = Field(default_factory=dict)
    branch_publications: List[BranchPublication] = Field(default_factory=list)
    unresolved_goals: List[str] = Field(default_factory=list)
    artifacts: Dict[str, Any] = Field(default_factory=dict)
    artifact_refs: Dict[str, Any] = Field(default_factory=dict)
    handle_or_job_refs: List[str] = Field(default_factory=list)
    budget_state: Dict[str, Any] = Field(default_factory=dict)
    verifier_state: Dict[str, Any] = Field(default_factory=dict)
    working_state_summary: Dict[str, Any] = Field(default_factory=dict)
    message_board_entries: List[Dict[str, Any]] = Field(default_factory=list)
    policy_checkpoint: Optional[Checkpoint] = None


class CheckpointTraceCursor(BaseModel):
    event_index: int = 0
    checkpoint_index: int = 0
    publication_sequence_by_branch: Dict[str, int] = Field(default_factory=dict)
    last_run_node_id: str = ""


class CheckpointEnvelope(BaseModel):
    identity: CheckpointIdentity
    state: CheckpointStateSnapshot
    receipts: List[SideEffectReceipt] = Field(default_factory=list)
    trace_cursor: CheckpointTraceCursor = Field(default_factory=CheckpointTraceCursor)
>>>>>>> REPLACE

<<<<<<< SEARCH
class ResumeRequest(BaseModel):
    request_id: str
    checkpoint_ref: Optional[str] = None
    trace_context: Optional[OpenAITraceContext] = None
    reconciliation_policy: Literal["strict", "best_effort"] = "strict"
=======
class ResumeRequest(BaseModel):
    request_id: str
    checkpoint_ref: Optional[CheckpointReference] = None
    trace_context: Optional[OpenAITraceContext] = None
    reconciliation_policy: Literal["strict", "best_effort"] = "strict"
>>>>>>> REPLACE

### File: `agintor/runtime_api.py`

<<<<<<< SEARCH
from .schemas import (
    AgentTemplate,
    BenchmarkTask,
    CapabilityExchange,
    ExecutionFlags,
    ExecutionPlan,
    InputBinding,
    Checkpoint,
    OpenAITraceContext,
    PlanNode,
    PlanOrigin,
    InspectRequest,
    ModelResponse,
    OperationSpec,
    RunResult,
    RuntimeBatchRequest,
    RuntimeSolveResponse,
    RuntimeSolveRequest,
    RuntimeTaskInvocation,
    SideEffectReceipt,
    SolveRequest,
    SolveResult,
    VerificationPlan,
)
=======
from .schemas import (
    AgentTemplate,
    BenchmarkTask,
    BranchPlan,
    BranchPublication,
    BranchPublicationKind,
    BranchState,
    CapabilityExchange,
    Checkpoint,
    CheckpointEnvelope,
    CheckpointReference,
    ExecutionFlags,
    ExecutionPlan,
    InputBinding,
    InspectRequest,
    ModelRequest,
    ModelResponse,
    OpenAITraceContext,
    OperationSpec,
    PlanNode,
    PlanOrigin,
    RecoveryFailureKind,
    ResumeRequest,
    RunResult,
    RuntimeBatchRequest,
    RuntimeSolveResponse,
    RuntimeSolveRequest,
    RuntimeTaskInvocation,
    SideEffectReceipt,
    SolveRequest,
    SolveResult,
    VerificationPlan,
)
>>>>>>> REPLACE

<<<<<<< SEARCH
class RuntimeState:
    request_id: str = ""
    plan_id: str = ""
    execution_state: str = "idle"
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
    plan_node_status: dict[str, str] = field(default_factory=dict)
    branch_states: dict[str, dict[str, Any]] = field(default_factory=dict)
    branch_publications: list[dict[str, Any]] = field(default_factory=list)
    side_effect_receipts: list[dict[str, Any]] = field(default_factory=list)
    subgoal_negative_steps: dict[str, int] = field(default_factory=dict)
    subgoal_last_model: dict[str, str] = field(default_factory=dict)
    last_unresolved_goal: str | None = None
=======
class RuntimeState:
    request_id: str = ""
    plan_id: str = ""
    execution_state: str = "idle"
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
    worker_plans: dict[str, BranchPlan] = field(default_factory=dict)
    open_handle_ids: list[str] = field(default_factory=list)
    plan_node_status: dict[str, str] = field(default_factory=dict)
    branch_states: dict[str, BranchState] = field(default_factory=dict)
    accepted_publications: dict[str, BranchPublication] = field(default_factory=dict)
    branch_publications: list[BranchPublication] = field(default_factory=list)
    side_effect_receipts: list[SideEffectReceipt] = field(default_factory=list)
    verifier_state: dict[str, Any] = field(default_factory=dict)
    message_board_entries: list[dict[str, Any]] = field(default_factory=list)
    trace_cursor: dict[str, Any] = field(default_factory=lambda: {"event_index": 0, "checkpoint_index": 0, "publication_sequence_by_branch": {}})
    last_checkpoint_ref: CheckpointReference | None = None
    subgoal_negative_steps: dict[str, int] = field(default_factory=dict)
    subgoal_last_model: dict[str, str] = field(default_factory=dict)
    last_unresolved_goal: str | None = None
>>>>>>> REPLACE

<<<<<<< SEARCH
    def record(self, event: str, **payload: Any) -> None:
        self.trace.append(
            {
                "event": event,
                "plan_id": self.plan.plan_id,
                "request_id": self.request_id,
                "trace_context": model_dump(self.trace_context),
                **payload,
            }
        )
=======
    def record(self, event: str, **payload: Any) -> None:
        next_index = int(self.state.trace_cursor.get("event_index", 0)) + 1
        self.state.trace_cursor["event_index"] = next_index
        self.trace.append(
            {
                "event_index": next_index,
                "event": event,
                "plan_id": self.plan.plan_id,
                "request_id": self.request_id,
                "execution_state": self.state.execution_state,
                "trace_context": model_dump(self.trace_context),
                **payload,
            }
        )
>>>>>>> REPLACE

<<<<<<< SEARCH
    def build_model_request(
        self,
        *,
        instructions: str,
        prompt: str,
        model_class: str,
        purpose: str,
        payload: Optional[dict[str, Any]] = None,
        trace_context: OpenAITraceContext | None = None,
    ):
        effective_trace_context = trace_context or self.trace_context
        return type(
            "Req",
            (),
            {
                "instructions": instructions,
                "prompt": prompt,
                "model_class": model_class,
                "seed": self.seed,
                "metadata": {
                    "mode": purpose,
                    "payload": dict(payload or {}),
                    "trace_context": model_dump(effective_trace_context),
                },
            },
        )
=======
    def build_model_request(
        self,
        *,
        instructions: str,
        prompt: str,
        model_class: str,
        purpose: str,
        payload: Optional[dict[str, Any]] = None,
        trace_context: OpenAITraceContext | None = None,
    ) -> ModelRequest:
        effective_trace_context = trace_context or self.trace_context
        return ModelRequest(
            instructions=instructions,
            prompt=prompt,
            model_class=model_class,
            seed=self.seed,
            metadata={
                "mode": purpose,
                "payload": dict(payload or {}),
                "trace_context": model_dump(effective_trace_context),
            },
        )
>>>>>>> REPLACE

<<<<<<< SEARCH
    def record_side_effect(self, receipt: SideEffectReceipt) -> None:
        self.state.side_effect_receipts.append(model_dump(receipt))
        self.record(
            "side_effect_recorded",
            side_effect_id=receipt.side_effect_id,
            action_kind=receipt.action_kind,
            status=receipt.status,
            branch_id=receipt.branch_id,
        )
=======
    def record_side_effect(self, receipt: SideEffectReceipt) -> None:
        self.state.side_effect_receipts.append(receipt)
        self.record(
            "side_effect_recorded",
            side_effect_id=receipt.side_effect_id,
            action_kind=receipt.action_kind,
            status=str(receipt.status),
            branch_id=receipt.branch_id,
            node_id=receipt.node_id,
            handle_id=receipt.handle_id,
        )
>>>>>>> REPLACE

<<<<<<< SEARCH
def _compile_plan_nodes(task: BenchmarkTask) -> list[PlanNode]:
    dependency_to_output = {operation.op_id: operation.output_key for operation in task.operations}
    root_ops = [operation.op_id for operation in task.operations if not operation.dependencies]
    root_branch_group = "root-frontier" if len(root_ops) > 1 else None
    nodes: list[PlanNode] = []
=======
def _compile_plan_nodes(task: BenchmarkTask) -> list[PlanNode]:
    dependency_to_output = {operation.op_id: operation.output_key for operation in task.operations}
    root_ops = [operation.op_id for operation in task.operations if not operation.dependencies]
    branchable_root_group = "root-frontier" if len(root_ops) > 1 else None
    nodes: list[PlanNode] = []
>>>>>>> REPLACE

<<<<<<< SEARCH
                branch_group_id=root_branch_group if operation.op_id in root_ops else None,
                metadata={
                    "operation_kind": operation.kind,
                    "task_type": task.task_type,
                    "family": task.family,
                },
            )
        )
    return nodes
=======
                branch_group_id=branchable_root_group if operation.op_id in root_ops else None,
                metadata={
                    "operation_kind": operation.kind,
                    "task_type": task.task_type,
                    "family": task.family,
                },
            )
        )
    if branchable_root_group is not None:
        nodes.append(
            PlanNode(
                node_id=f"merge.{branchable_root_group}",
                node_kind="merge",
                instruction="Merge the published outputs for the branchable frontier in deterministic order.",
                output_key=f"merge.{branchable_root_group}",
                dependencies=list(root_ops),
                frame_role="merge_horizontal",
                metadata={"consumes_branch_group": branchable_root_group},
            )
        )
    if task.verification_required:
        verification_dependencies = [nodes[-1].node_id] if nodes and nodes[-1].node_kind == "merge" else [node.node_id for node in nodes if node.output_key]
        nodes.append(
            PlanNode(
                node_id="verify.terminal",
                node_kind="verify",
                instruction="Run the frozen checker ladder and exact verifier policy for the terminal artifact.",
                output_key="verification.terminal",
                dependencies=verification_dependencies,
                verification_required=True,
                externally_visible=bool(task.externally_visible),
                frame_role="verifier",
            )
        )
    return nodes
>>>>>>> REPLACE

<<<<<<< SEARCH
def runtime_batch_request_for_tasks(
    *,
    request_id: str,
    runtime_backend: str,
    task_runs: list[tuple[BenchmarkTask, int]],
    budget_overrides: dict[str, Any] | None = None,
) -> RuntimeBatchRequest:
=======
def runtime_batch_request_for_tasks(
    *,
    request_id: str,
    runtime_backend: str,
    task_runs: list[tuple[BenchmarkTask, int]],
    budget_overrides: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> RuntimeBatchRequest:
>>>>>>> REPLACE

<<<<<<< SEARCH
                trace_context=build_trace_context(
                    provider_role="runtime",
                    request_id=normalize_benchmark_request_id(task.task_id, int(seed)),
                    task_id=task.task_id,
                    seed=int(seed),
                    objective=task.prompt,
                ),
=======
                trace_context=build_trace_context(
                    provider_role="runtime",
                    request_id=normalize_benchmark_request_id(task.task_id, int(seed)),
                    task_id=task.task_id,
                    seed=int(seed),
                    objective=task.prompt,
                    session_id=session_id,
                ),
>>>>>>> REPLACE

<<<<<<< SEARCH
        trace_context=build_trace_context(
            provider_role="runtime",
            request_id=request_id,
        ),
    )
=======
        trace_context=build_trace_context(
            provider_role="runtime",
            request_id=request_id,
            session_id=session_id,
        ),
    )


def resume_request_for_runtime(
    *,
    request_id: str,
    checkpoint_ref: CheckpointReference | None = None,
    trace_context: OpenAITraceContext | None = None,
    reconciliation_policy: str = "strict",
) -> ResumeRequest:
    return ResumeRequest(
        request_id=request_id,
        checkpoint_ref=checkpoint_ref,
        trace_context=trace_context or build_trace_context(provider_role="runtime", request_id=request_id),
        reconciliation_policy=reconciliation_policy,
    )
>>>>>>> REPLACE

### File: `agintor/shell.py`

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
    def publish_checkpoint_envelope(self, envelope) -> CheckpointReference:
        request_key = envelope.identity.request_id.replace("/", "_")
        request_dir = ensure_directory(self.checkpoint_dir / request_key)
        path = request_dir / f"{envelope.identity.sequence_no:04d}.{envelope.identity.boundary.value}.json"
        path.write_text(json.dumps(model_dump(envelope), indent=2, sort_keys=True), encoding="utf-8")

        index_path = request_dir / "index.json"
        history = []
        if index_path.exists():
            try:
                history = json.loads(index_path.read_text(encoding="utf-8")).get("checkpoints", [])
            except Exception:
                history = []
        history = [row for row in history if row.get("checkpoint_id") != envelope.identity.checkpoint_id]
        history.append(
            {
                "checkpoint_id": envelope.identity.checkpoint_id,
                "path": str(path),
                "sequence_no": envelope.identity.sequence_no,
                "boundary": envelope.identity.boundary.value,
                "created_at": envelope.identity.created_at,
            }
        )
        history.sort(key=lambda row: (row["sequence_no"], row["checkpoint_id"]))
        index_path.write_text(
            json.dumps({"request_id": envelope.identity.request_id, "checkpoints": history}, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return CheckpointReference(
            ref=str(path),
            task_id=envelope.identity.task_id,
            seed=envelope.identity.seed,
            request_id=envelope.identity.request_id,
            plan_id=envelope.identity.plan_id,
            checkpoint_id=envelope.identity.checkpoint_id,
            sequence_no=envelope.identity.sequence_no,
            boundary=envelope.identity.boundary,
            created_at=envelope.identity.created_at,
            checkpoint_count=len(history),
            latest=True,
        )

    def load_checkpoint_envelope(self, checkpoint_ref: CheckpointReference):
        return json.loads(Path(checkpoint_ref.ref).read_text(encoding="utf-8"))

    def select_checkpoint_for_resume(self, request_id: str, checkpoint_ref: CheckpointReference | None = None):
        if checkpoint_ref is not None:
            return self.load_checkpoint_envelope(checkpoint_ref)
        request_dir = self.checkpoint_dir / request_id.replace("/", "_")
        index_path = request_dir / "index.json"
        if not index_path.exists():
            raise HardInvalidation("checkpoint_not_found")
        payload = json.loads(index_path.read_text(encoding="utf-8"))
        rows = list(payload.get("checkpoints", []))
        if not rows:
            raise HardInvalidation("checkpoint_not_found")
        latest = max(rows, key=lambda row: (row.get("sequence_no", 0), row.get("checkpoint_id", "")))
        return json.loads(Path(latest["path"]).read_text(encoding="utf-8"))
>>>>>>> REPLACE

<<<<<<< SEARCH
class OpenHandleTable:
    def __init__(self) -> None:
        self.handles: dict[str, AsyncHandle] = {}
=======
class OpenHandleTable:
    def __init__(self) -> None:
        self.handles: dict[str, AsyncHandle] = {}

    def freeze_snapshot(self) -> dict[str, dict[str, Any]]:
        return {handle_id: model_dump(handle) for handle_id, handle in self.handles.items()}

    def restore_snapshot(self, snapshot: Mapping[str, Mapping[str, Any]]) -> None:
        self.handles = {handle_id: AsyncHandle(**dict(payload)) for handle_id, payload in snapshot.items()}

    def cancel(self, handle_id: str) -> None:
        handle = self.handles[handle_id]
        handle.state = "cancelled"
        self.handles[handle_id] = handle
>>>>>>> REPLACE

### File: `agintor/runner.py`

<<<<<<< SEARCH
    def run_task(self, task: BenchmarkTask, seed: int) -> RunResult:
        with self._isolated_provider_environment():
            task = model_copy(task, deep=True)
=======
    def run_task(
        self,
        task: BenchmarkTask,
        seed: int,
        *,
        request_id: str | None = None,
        trace_context: Any | None = None,
    ) -> RunResult:
        normalized_request_id = request_id or normalize_benchmark_request_id(task.task_id, seed)
        plan = compile_execution_plan_from_task(
            task,
            request_id=normalized_request_id,
            seed=seed,
            runtime_hash=self.runtime.runtime_hash,
            runtime_dir=str(self.runtime.runtime_dir),
            trace_context=trace_context,
        )
        return self._run_execution_plan(task, plan, seed=seed)

    def run_solve_request(self, request: RuntimeSolveRequest) -> RunResult:
        if request.mode == "benchmark":
            if request.task is None:
                raise HardInvalidation("benchmark solve requires a benchmark task")
            plan = compile_execution_plan_from_task(
                request.task,
                request_id=request.request_id,
                seed=request.seed,
                runtime_hash=self.runtime.runtime_hash,
                runtime_dir=str(self.runtime.runtime_dir),
                trace_context=request.trace_context,
            )
            task = model_copy(request.task, deep=True)
        else:
            if request.solve_request is None:
                raise HardInvalidation("user_request solve requires a solve request payload")
            task, plan = compile_execution_plan_from_solve_request(
                request.solve_request,
                seed=request.seed,
                runtime_hash=self.runtime.runtime_hash,
                runtime_dir=str(self.runtime.runtime_dir),
                trace_context=request.trace_context,
            )
        return self._run_execution_plan(task, plan, seed=request.seed)

    def resume_request(self, request: ResumeRequest) -> RunResult:
        envelope = self.shell.select_checkpoint_for_resume(request.request_id, request.checkpoint_ref)
        task, plan, state = self._restore_execution_state(envelope, request)
        return self._run_execution_plan(task, plan, seed=envelope["identity"]["seed"], restored_state=state)

    def _run_execution_plan(self, task: BenchmarkTask, plan: ExecutionPlan, *, seed: int, restored_state: RuntimeState | None = None) -> RunResult:
        with self._isolated_provider_environment():
            task = model_copy(task, deep=True)
>>>>>>> REPLACE

<<<<<<< SEARCH
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
            context = PolicyContext(
                runtime_dir=self.runtime.runtime_dir,
                shell=self.shell,
                task=task,
                request_id=plan.request_id,
                plan=plan,
                trace_context=plan.trace_context or build_trace_context(provider_role="runtime", request_id=plan.request_id, task_id=task.task_id, seed=seed, objective=plan.objective),
                provider=self.provider,
                profile=self.runtime_profile,
                seed=seed,
                state=restored_state or state,
                budget=budget,
                trace=trace,
                objective=plan.objective,
                runtime_backend=self.runtime_profile.runtime_backend,
                side_effect_callback=self._record_receipt_boundary,
                checkpoint_callback=lambda boundary: self._publish_checkpoint_envelope(context, boundary),
            )
            context.state.request_id = plan.request_id
            context.state.plan_id = plan.plan_id
            context.state.execution_state = "running"
>>>>>>> REPLACE

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
        workers = self.runtime.topology.select_workers(context, frame, task.operations)
        branch_results = self._run_branch_group(context, frame, task, workers)
        context.state.queue.append(
            AgentFrame(
                frame_id=f"merge.{context.plan.plan_id}.{frame.frame_id}",
                agent=self.shell.agent_pool.clone("root"),
                request_id=context.request_id,
                plan_id=context.plan.plan_id,
                objective="merge",
                operation_ids=[],
                depth=frame.depth,
                role="merge_horizontal",
                trace_context=context.derive_trace_context(frame_role="merge_horizontal", agent_id="root"),
                metadata={"branch_results": [model_dump(result) for result in branch_results], "parent_run_node_id": frame.metadata.get("run_node_id")},
            )
        )
>>>>>>> REPLACE

<<<<<<< SEARCH
    def _publish_checkpoint_summary(self, frame: AgentFrame, checkpoint: Checkpoint) -> None:
        summary_id = self.shell.short_term.add_node(
            "Summary",
            checkpoint.summary.objective,
            model_dump(checkpoint.summary),
            agent_id=frame.agent.agent_id,
            role=frame.role,
        )
        parent_run_node_id = frame.metadata.get("parent_run_node_id")
        if isinstance(parent_run_node_id, str) and parent_run_node_id in self.shell.short_term.nodes:
            self.shell.short_term.add_edge(parent_run_node_id, summary_id, "CALLS_AGENT")
        for artifact_ref in checkpoint.artifact_refs:
            artifact_id = self.shell.short_term.add_node("Artifact", artifact_ref, {"artifact_ref": artifact_ref})
            self.shell.short_term.add_edge(summary_id, artifact_id, "PRODUCES")
        for handle_id in checkpoint.open_handles:
            if handle_id in self.shell.open_handles.handles:
                handle = self.shell.open_handles.get(handle_id)
                handle_node_id = self.shell.short_term.add_node("OpenHandle", handle.tool_name, model_dump(handle))
                self.shell.short_term.add_edge(summary_id, handle_node_id, "WAITS_ON")
=======
    def _publish_checkpoint_summary(self, frame: AgentFrame, checkpoint: Checkpoint) -> None:
        summary_id = self.shell.short_term.add_node(
            "Summary",
            checkpoint.summary.objective,
            model_dump(checkpoint.summary),
            agent_id=frame.agent.agent_id,
            role=frame.role,
        )
        parent_run_node_id = frame.metadata.get("parent_run_node_id")
        if isinstance(parent_run_node_id, str) and parent_run_node_id in self.shell.short_term.nodes:
            self.shell.short_term.add_edge(parent_run_node_id, summary_id, "CALLS_AGENT")
        for artifact_ref in checkpoint.artifact_refs:
            artifact_id = self.shell.short_term.add_node("Artifact", artifact_ref, {"artifact_ref": artifact_ref})
            self.shell.short_term.add_edge(summary_id, artifact_id, "PRODUCES")
        for handle_id in checkpoint.open_handles:
            if handle_id in self.shell.open_handles.handles:
                handle = self.shell.open_handles.get(handle_id)
                handle_node_id = self.shell.short_term.add_node("OpenHandle", handle.tool_name, model_dump(handle))
                self.shell.short_term.add_edge(summary_id, handle_node_id, "WAITS_ON")

    def _publish_checkpoint_envelope(self, context: PolicyContext, boundary: str) -> None:
        checkpoint_index = int(context.state.trace_cursor.get("checkpoint_index", 0)) + 1
        context.state.trace_cursor["checkpoint_index"] = checkpoint_index
        envelope = self._build_checkpoint_envelope(context, boundary, checkpoint_index)
        checkpoint_ref = self.shell.publish_checkpoint_envelope(envelope)
        context.state.checkpoints[envelope.identity.checkpoint_id] = envelope
        context.state.last_checkpoint_ref = checkpoint_ref
        context.record("checkpoint_published", checkpoint_id=envelope.identity.checkpoint_id, boundary=boundary, checkpoint_ref=model_dump(checkpoint_ref))

    def _build_checkpoint_envelope(self, context: PolicyContext, boundary: str, checkpoint_index: int):
        checkpoint_id = f"checkpoint.{context.request_id}.{checkpoint_index:04d}"
        return {
            "identity": {
                "checkpoint_id": checkpoint_id,
                "request_id": context.request_id,
                "plan_id": context.plan.plan_id,
                "task_id": context.task.task_id,
                "seed": context.seed,
                "runtime_abi": self.runtime.manifest.metadata.get("runtime_abi", ""),
                "storage_schema_version": self.runtime.kernel_manifest.storage_schema_version,
                "runtime_hash": self.runtime.runtime_hash,
                "sequence_no": checkpoint_index,
                "boundary": boundary,
                "created_at": now_ts(),
            },
            "state": {
                "queued_frames": [model_dump(frame) for frame in context.state.queue],
                "plan_node_status": dict(context.state.plan_node_status),
                "branch_state": {branch_id: model_dump(branch_state) for branch_id, branch_state in context.state.branch_states.items()},
                "accepted_publications": {key: model_dump(publication) for key, publication in context.state.accepted_publications.items()},
                "branch_publications": [model_dump(publication) for publication in context.state.branch_publications],
                "unresolved_goals": list(context.state.unresolved_goals),
                "artifacts": dict(context.state.artifacts),
                "artifact_refs": dict(context.state.artifacts),
                "handle_or_job_refs": list(context.state.open_handle_ids),
                "budget_state": context.budget.normalized(),
                "verifier_state": dict(context.state.verifier_state),
                "working_state_summary": {"mode": context.state.mode, "confidence": context.state.confidence},
                "message_board_entries": list(self.shell.message_board.entries),
            },
            "receipts": [model_dump(receipt) for receipt in context.state.side_effect_receipts],
            "trace_cursor": dict(context.state.trace_cursor),
        }
>>>>>>> REPLACE

<<<<<<< SEARCH
        dispatch_meta = self.runtime.tooling.dispatch_tool(context, tool_name, args)
        if dispatch_meta.get("async"):
            handle = self.shell.tool_executor.launch_async(
                tool_name,
                args,
                self.shell.workspace / "handles",
                context.task.task_id,
            )
=======
        dispatch_meta = self.runtime.tooling.dispatch_tool(context, tool_name, args)
        side_effect_id = stable_hash(context.request_id, context.plan.plan_id, operation.op_id, frame.worker_id or "", tool_name, args)[:16]
        if dispatch_meta.get("async"):
            handle = self.shell.tool_executor.launch_async(
                tool_name,
                args,
                self.shell.workspace / "handles",
                context.task.task_id,
                request_id=context.request_id,
                plan_id=context.plan.plan_id,
                node_id=operation.op_id,
                branch_id=frame.worker_id or "",
                idempotency_key=side_effect_id,
            )
            context.record_side_effect(
                SideEffectReceipt(
                    side_effect_id=f"tool-launch.{side_effect_id}",
                    action_fingerprint=side_effect_id,
                    idempotency_key=side_effect_id,
                    action_kind="tool_launch",
                    branch_id=frame.worker_id,
                    node_id=operation.op_id,
                    request_digest=side_effect_id,
                    backend=context.runtime_backend,
                    status="launched",
                    handle_id=handle.handle_id,
                    replay_policy="reconcile_before_reissue",
                    reconciliation_policy="handle_status",
                    created_at=now_ts(),
                )
            )
            context.publish_checkpoint_boundary("after_tool_launch")
>>>>>>> REPLACE

<<<<<<< SEARCH
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
=======
            if dispatch_meta.get("await_immediately"):
                finished = self.shell.tool_executor.await_handle(handle.handle_id, self.shell.open_handles)
                context.budget.consume_tool_latency(float(finished.get("latency_s", 0.0)))
                if finished.get("state") != "completed":
                    faults += 1
                    stderr = str(finished.get("stderr", "async execution failed"))
                    context.record("tool_fault", tool=tool_name, stderr=stderr)
                    self._record_tool_failure(context, operation, tool_name, stderr)
                    raise HardInvalidation(f"tool execution failed for {tool_name}: {stderr}")
                context.record_side_effect(
                    SideEffectReceipt(
                        side_effect_id=f"tool-completion.{side_effect_id}",
                        action_fingerprint=side_effect_id,
                        idempotency_key=side_effect_id,
                        action_kind="tool_completion",
                        branch_id=frame.worker_id,
                        node_id=operation.op_id,
                        request_digest=side_effect_id,
                        backend=context.runtime_backend,
                        status="completed",
                        handle_id=handle.handle_id,
                        result_ref=finished,
                        replay_policy="reuse_completed",
                        reconciliation_policy="handle_status",
                        created_at=now_ts(),
                    )
                )
                context.publish_checkpoint_boundary("after_tool_completion")
                output = finished.get("output")
            else:
                output = {"handle_id": handle.handle_id, "deferred": True}
                context.state.plan_node_status[operation.op_id] = "waiting_on_handle"
>>>>>>> REPLACE

### File: `agintor/runtime_host.py`

<<<<<<< SEARCH
from .runtime_api import inspect_request_for_runtime, runtime_batch_request_for_tasks, solve_request_to_task
from .runtime_loader import RUNTIME_ABI_VERSION
from .runtime_sdk import KERNEL_BUNDLE_DIR, KERNEL_VERSION, STORAGE_SCHEMA_VERSION
from .schemas import BenchmarkTask, CapabilityExchange, RuntimeBatchResponse, RuntimeSolveRequest, RuntimeSolveResponse
=======
from .runtime_api import inspect_request_for_runtime, resume_request_for_runtime, runtime_batch_request_for_tasks, solve_request_to_task
from .runtime_loader import RUNTIME_ABI_VERSION
from .runtime_sdk import KERNEL_BUNDLE_DIR, KERNEL_VERSION, STORAGE_SCHEMA_VERSION
from .schemas import BenchmarkTask, CapabilityExchange, CheckpointReference, ResumeRequest, RuntimeBatchResponse, RuntimeSolveRequest, RuntimeSolveResponse
>>>>>>> REPLACE

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
        return self._execute_runtime_command("solve", runtime_dir, request, provider=provider, runtime_profile=runtime_profile)

    def resume(
        self,
        runtime_dir: str | Path,
        *,
        request_id: str,
        checkpoint_ref: CheckpointReference | None = None,
        provider: ModelProvider,
        runtime_profile: object | None = None,
        reconciliation_policy: str = "strict",
    ) -> RuntimeSolveResponse:
        capability_exchange = self.inspect(runtime_dir)
        if not capability_exchange.checkpoint_support or not capability_exchange.resume_support:
            raise RuntimeLoadError(f"runtime {runtime_dir} does not advertise checkpoint resume support")
        request = resume_request_for_runtime(
            request_id=request_id,
            checkpoint_ref=checkpoint_ref,
            reconciliation_policy=reconciliation_policy,
        )
        return self._execute_runtime_command("resume", runtime_dir, request, provider=provider, runtime_profile=runtime_profile, capability_exchange=capability_exchange)
>>>>>>> REPLACE

<<<<<<< SEARCH
            "solve",
            "--runtime-dir",
            str(runtime_dir.resolve()),
            "--input-json",
            str(input_json.resolve()),
            "--output-json",
            str(output_json.resolve()),
=======
            command,
            "--runtime-dir",
            str(runtime_dir.resolve()),
            "--input-json",
            str(input_json.resolve()),
            "--output-json",
            str(output_json.resolve()),
>>>>>>> REPLACE

### File: `agintor/runtime_sdk/runtime_entry.py`

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

<<<<<<< SEARCH
        runner = TaskRuntime(
            runtime,
            shell,
            provider,
            budget_overrides=request.budget_overrides,
            runtime_profile=runtime_profile,
        )
        run_result = runner.run_task(task, request.seed)
=======
        runner = TaskRuntime(
            runtime,
            shell,
            provider,
            budget_overrides=request.budget_overrides,
            runtime_profile=runtime_profile,
        )
        run_result = runner.run_solve_request(request)
>>>>>>> REPLACE

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

<<<<<<< SEARCH
    if args.command == "solve":
        return _solve(args)
=======
    if args.command == "solve":
        return _solve(args)
    if args.command == "resume":
        request = model_validate(
            ResumeRequest,
            json.loads(Path(args.input_json).read_text(encoding="utf-8")),
        )
        runtime_profile = load_runtime_profile(args.runtime_dir, profile_path=args.profile_json)
        runtime = load_runtime(
            args.runtime_dir,
            runtime_profile=runtime_profile,
            runtime_backend="local",
        )
        capability_exchange = CapabilityExchange(**model_dump(runtime.capability_exchange))
        provider_payload_data = json.loads(Path(args.provider_json).read_text(encoding="utf-8"))
        provider = build_provider_from_payload(provider_payload_data, provider_profile=runtime_profile.runtime_provider)
        shell = FixedShell(
            Path(args.workspace) / "resume",
            artifact_mode=ArtifactMode(args.artifact_mode),
        )
        runner = TaskRuntime(runtime, shell, provider, runtime_profile=runtime_profile)
        run_result = runner.resume_request(request)
        solve_request = benchmark_task_to_solve_request(model_validate(BenchmarkTask, {"task_id": run_result.task_id, "family": "e2e", "prompt": "", "task_type": "resume", "expected": None}))
        response = RuntimeSolveResponse(
            request_id=request.request_id,
            capability_exchange=capability_exchange,
            solve_result=solve_result_from_run_result_with_context(
                solve_request,
                run_result,
                runtime.runtime_hash,
                mode="benchmark",
                provider_usage=provider.usage_summary(),
            ),
        )
        Path(args.output_json).write_text(json.dumps(model_dump(response), indent=2, sort_keys=True), encoding="utf-8")
        return 0
>>>>>>> REPLACE

### File: `agintor/runtime_loader.py`

<<<<<<< SEARCH
RUNTIME_ABI_VERSION = "agintor-runtime-abi-v3"
=======
RUNTIME_ABI_VERSION = "agintor-runtime-abi-v4"
>>>>>>> REPLACE

<<<<<<< SEARCH
    KERNEL_VERSION = "agintor-kernel-v1"
    STORAGE_SCHEMA_VERSION = "agintor-storage-v1"
=======
    KERNEL_VERSION = "agintor-kernel-v1"
    STORAGE_SCHEMA_VERSION = "agintor-storage-v2"
>>>>>>> REPLACE

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
    effective_isolation_policy = deployment_contract.runtime_isolation_policy or RuntimeIsolationPolicy(
        workspace_root=str(runtime_path),
        environment_allowlist=list(deployment_contract.environment_allowlist),
        network_policy=deployment_contract.network_policy,
        filesystem_policy=deployment_contract.filesystem_policy,
    )
    capability_exchange = CapabilityExchange(
        runtime_abi=RUNTIME_ABI_VERSION,
        kernel_version=kernel_manifest.kernel_version,
        storage_schema_version=kernel_manifest.storage_schema_version,
        supported_backends=list(deployment_contract.supported_backends),
        tool_runtimes=["python"],
        checkpoint_support=True,
        runtime_asset_capabilities={"traces": True, "checkpoints": True, "runtime_sdk": True},
        side_effect_receipts=True,
        resume_support=True,
        runtime_isolation_policy=effective_isolation_policy,
        supported_guarantees=["timeout_enforcement", "workspace_isolation", "environment_filtering", "process_cleanup", "network_disablement"],
        effective_guarantees=[],
        required_env_names=list(deployment_contract.required_env_names),
        required_env_any_of=[list(group) for group in deployment_contract.required_env_any_of],
        capability_flags=list(deployment_contract.capability_flags or kernel_manifest.capability_flags),
    )
>>>>>>> REPLACE

## Notes

- `CheckpointEnvelope` should be the only restart surface. `Checkpoint` remains the policy summary fragment returned by `make_checkpoint`.
- `strict` reconciliation must never reissue `provider_request`, `provider_completion`, or `service_action` receipts without completion proof or backend-native reconciliation support. `best_effort` may relax only tool and filesystem reissue paths whose idempotency key and output location are stable.
- `FixedShell.publish_checkpoint_envelope()` should write append-only files under `workspace/checkpoints/<request_id>/` with an `index.json` pointer. Workstream 3 can replace the storage backend without changing the envelope or receipt meaning.
- `ToolExecutor.launch_async()` and container-backed execution need matching `request_id`, `plan_id`, `node_id`, `branch_id`, and `idempotency_key` plumbing so handle reconciliation is possible after interruption.
- `runtime_api.py` already contains partial plan and trace-context helpers; the implementation should tighten those helpers instead of adding a second request adaptation path.
- Merge must consume only accepted `BranchPublication` objects. Losing publications remain in trace history and checkpoint state; they never mutate the merged artifact map.
- Recovery failures should surface as structured `faults` fields and trace events with a stable `failure_kind` plus a concrete `recovery_reason` from `RecoveryFailureKind`.
