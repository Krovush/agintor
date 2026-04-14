# Worker 02 Proposal: Exact Snapshot / Resume / Side-Effect Ledger

Scope:
- Owns typed checkpoint snapshots, shell/runtime restore semantics, and fail-closed side-effect-ledger rules.
- Intentionally defers durable run-root creation, run-store indexing, manifest lifecycle wiring, and resume targeting to Worker 01.
- Intentionally defers branch-frontier selection, compiler/runtime arg separation, and batch transfer ordering to Worker 03.

Best-practice notes informing the proposal:
- [Temporal Python SDK](https://github.com/temporalio/sdk-python) requires deterministic replay, isolated workflow state, and cancellation-aware cleanup rather than ad hoc reconstruction of mutable state.
- [AWS Durable Execution steps docs](https://docs.aws.amazon.com/durable-functions/core/steps/) treat completed steps as instantly reusable on replay and explicitly recommend at-most-once semantics for side-effecting work.
- The practical implication for WS2 is: restore from typed state, never from summaries; treat side effects as receipts to reuse or reconcile, never as actions to blindly replay.

Assumptions:
- Worker 01 will wire durable run metadata into the runtime before solve/resume starts. The snapshot contract should freeze `AttemptSnapshot` now even if Worker 01 populates `run_id`, `run_root`, and `attempt_id` later.
- `workspace/side_effects/` is the required receipt sink from Worker 02's perspective. Worker 01 can move or index it under the durable run root without changing receipt semantics.
- `working_state_summary` remains in the envelope only for human diagnostics and must be safe to corrupt without changing restore behavior.

File: `agintor/schemas.py`
<<<<<<< SEARCH
class RuntimeStateSnapshot(BaseModel):
    queue_length: int
    budget_state: Dict[str, Any]
    unresolved_count: int
    visible_tool_count: int
    open_handle_count: int
    confidence: float
    active_mode: Optional[str] = None
    lifecycle_state: str = "idle"
    active_branch_count: int = 0
=======
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
    attempt_id: str = ""
    run_root: str = ""
    started_at: float = 0.0
    resumed_from_checkpoint_ref: str = ""
    published_boundary: str = ""
    published_at: float = 0.0


class RuntimeStateSnapshot(BaseModel):
    request_id: str = ""
    plan_id: str = ""
    execution_state: str = "idle"
    active_branch_count: int = 0
    checkpoint_sequence_no: int = 0
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
    latest_checkpoint_ref: Optional[str] = None
    subgoal_negative_steps: Dict[str, int] = Field(default_factory=dict)
    subgoal_last_model: Dict[str, str] = Field(default_factory=dict)
    last_unresolved_goal: Optional[str] = None
    budget_totals: RuntimeBudgetTotalsSnapshot = Field(default_factory=RuntimeBudgetTotalsSnapshot)
    verifier_state: VerifierStateSnapshot = Field(default_factory=VerifierStateSnapshot)
>>>>>>> REPLACE

File: `agintor/schemas.py`
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
    sequence_no: int = 0
    boundary: str = ""
    created_at: float = 0.0
    plan_snapshot: Dict[str, Any] = Field(default_factory=dict)
    task_payload: Dict[str, Any] = Field(default_factory=dict)
    queued_frames: List[QueuedFrameSnapshot] = Field(default_factory=list)
    plan_node_status: Dict[str, str] = Field(default_factory=dict)
    branch_state: List[BranchState] = Field(default_factory=list)
    branch_publications: List[BranchPublication] = Field(default_factory=list)
    unresolved_goals: List[str] = Field(default_factory=list)
    artifact_refs: Dict[str, Any] = Field(default_factory=dict)
    open_handle_snapshots: List[AsyncHandle] = Field(default_factory=list)
    handle_or_job_refs: List[str] = Field(default_factory=list)
    budget_state: Dict[str, Any] = Field(default_factory=dict)
    verifier_state: Dict[str, Any] = Field(default_factory=dict)
    working_state_summary: Dict[str, Any] = Field(default_factory=dict)
    trace_cursor: Dict[str, Any] = Field(default_factory=dict)
    side_effect_receipts: List[SideEffectReceipt] = Field(default_factory=list)
=======
class SideEffectLedgerSnapshot(BaseModel):
    receipts: List[SideEffectReceipt] = Field(default_factory=list)


class TraceCursorSnapshot(BaseModel):
    trace_length: int = 0
    latest_event: Optional[str] = None


class CheckpointEnvelope(BaseModel):
    checkpoint_schema_version: str = "agintor.checkpoint-envelope.v2"
    checkpoint_id: str
    runtime_abi: str
    storage_schema_version: str
    runtime_hash: str
    request_id: str
    plan_id: str
    task_id: str
    seed: int
    sequence_no: int = 0
    boundary: str = ""
    created_at: float = 0.0
    plan_snapshot: Dict[str, Any] = Field(default_factory=dict)
    task_payload: Dict[str, Any] = Field(default_factory=dict)
    runtime_state_snapshot: RuntimeStateSnapshot = Field(default_factory=RuntimeStateSnapshot)
    shell_state_snapshot: ShellStateSnapshot = Field(default_factory=ShellStateSnapshot)
    side_effect_ledger: SideEffectLedgerSnapshot = Field(default_factory=SideEffectLedgerSnapshot)
    attempt_snapshot: AttemptSnapshot = Field(default_factory=AttemptSnapshot)
    working_state_summary: Dict[str, Any] = Field(default_factory=dict)
    trace_cursor: TraceCursorSnapshot = Field(default_factory=TraceCursorSnapshot)
>>>>>>> REPLACE

File: `agintor/tool_runtime.py`
<<<<<<< SEARCH
from .schemas import AsyncHandle, ToolExecutionResult, ToolSpec
=======
from .schemas import (
    AsyncHandle,
    TaskLocalToolRegistrySnapshot,
    TaskLocalToolSnapshot,
    ToolExecutionResult,
    ToolSpec,
)
>>>>>>> REPLACE

File: `agintor/tool_runtime.py`
<<<<<<< SEARCH
    def reset_task_local(self) -> None:
        removable = [name for name, tool in self._tools.items() if tool.spec.category_path[:2] == ["generated", "local"]]
        for name in removable:
            self._tools.pop(name, None)
        removable_categories = [
            category_key
            for category_key in list(self._category_summaries)
            if category_key.startswith("generated/local")
            and not any(tool.category_key == category_key for tool in self._tools.values())
        ]
        for category_key in removable_categories:
            self._category_summaries.pop(category_key, None)

    def register_generated_tool(self, spec: ToolSpec, source: str, executor: Callable[..., Any] | None = None) -> RegisteredTool:
=======
    def reset_task_local(self) -> None:
        removable = [name for name, tool in self._tools.items() if tool.spec.category_path[:2] == ["generated", "local"]]
        for name in removable:
            self._tools.pop(name, None)
        removable_categories = [
            category_key
            for category_key in list(self._category_summaries)
            if category_key.startswith("generated/local")
            and not any(tool.category_key == category_key for tool in self._tools.values())
        ]
        for category_key in removable_categories:
            self._category_summaries.pop(category_key, None)

    def snapshot_task_local(self) -> TaskLocalToolRegistrySnapshot:
        tool_snapshots: list[TaskLocalToolSnapshot] = []
        category_summaries: dict[str, str] = {}
        for tool in self._tools.values():
            if tool.spec.category_path[:2] != ["generated", "local"]:
                continue
            sandbox_dir = self.sandbox_manager.ensure_environment(tool.spec)
            tool_file = sandbox_dir / _tool_filename(tool.spec)
            source = tool_file.read_text(encoding="utf-8") if tool_file.exists() else ""
            tool_snapshots.append(
                TaskLocalToolSnapshot(
                    spec=model_copy(tool.spec, deep=True),
                    source=source,
                    historical_passes=tool.historical_passes,
                    historical_runs=tool.historical_runs,
                    distinct_tasks=sorted(tool.distinct_tasks),
                    sandbox_hash=tool.sandbox_hash,
                    safety_validated=tool.safety_validated,
                )
            )
            category_summaries[tool.category_key] = self._category_summaries.get(tool.category_key, tool.spec.description)
        return TaskLocalToolRegistrySnapshot(tools=tool_snapshots, category_summaries=category_summaries)

    def restore_task_local(self, snapshot: TaskLocalToolRegistrySnapshot) -> None:
        self.reset_task_local()
        for tool_snapshot in snapshot.tools:
            registered = self.register_generated_tool(tool_snapshot.spec, tool_snapshot.source)
            registered.historical_passes = tool_snapshot.historical_passes
            registered.historical_runs = tool_snapshot.historical_runs
            registered.distinct_tasks = set(tool_snapshot.distinct_tasks)
            registered.sandbox_hash = tool_snapshot.sandbox_hash
            registered.safety_validated = tool_snapshot.safety_validated
            self._category_summaries[registered.category_key] = snapshot.category_summaries.get(
                registered.category_key,
                registered.spec.description,
            )

    def register_generated_tool(self, spec: ToolSpec, source: str, executor: Callable[..., Any] | None = None) -> RegisteredTool:
>>>>>>> REPLACE

File: `agintor/shell.py`
<<<<<<< SEARCH
from .memory_graph import LongTermGraph, ShortTermGraph
=======
from .memory_graph import GraphEdge, LongTermGraph, ShortTermGraph
>>>>>>> REPLACE

File: `agintor/shell.py`
<<<<<<< SEARCH
from .schemas import AgentTemplate, AsyncHandle, CheckpointEnvelope, CheckpointReference
=======
from .schemas import (
    AgentTemplate,
    AsyncHandle,
    AttemptSnapshot,
    CheckpointEnvelope,
    CheckpointReference,
    LongTermGraphSnapshot,
    MessageBoardSnapshot,
    ShellStateSnapshot,
    ShortTermGraphSnapshot,
    SideEffectReceipt,
)
>>>>>>> REPLACE

File: `agintor/shell.py`
<<<<<<< SEARCH
        self.trace_dir = self.workspace / "traces"
        self.checkpoint_dir = self.workspace / "checkpoints"
        self._resume_checkpoint_store_dir: Path | None = None
        self._current_task_id: str | None = None
        self._current_episode_id: str | None = None
        self._memory_scope_kind: str | None = None
        self._memory_scope_id: str | None = None
=======
        self.trace_dir = self.workspace / "traces"
        self.checkpoint_dir = self.workspace / "checkpoints"
        self.side_effect_dir = self.workspace / "side_effects"
        self._resume_checkpoint_store_dir: Path | None = None
        self._current_task_id: str | None = None
        self._current_episode_id: str | None = None
        self._memory_scope_kind: str | None = None
        self._memory_scope_id: str | None = None
        self._attempt_metadata: dict[str, Any] = {}
>>>>>>> REPLACE

File: `agintor/shell.py`
<<<<<<< SEARCH
    def restore_open_handles(self, handles: Iterable[AsyncHandle]) -> None:
        restored: dict[str, AsyncHandle] = {}
        for handle in handles:
            restored[handle.handle_id] = model_copy(handle, deep=True)
        self.open_handles.handles = restored

    def load_open_handle_output(self, handle: AsyncHandle) -> Any:
        if not handle.artifact_refs:
            return None
        result_path = Path(handle.artifact_refs[0])
        if not result_path.exists():
            return None
        try:
            return json.loads(result_path.read_text(encoding="utf-8"))
        except Exception:
            return None
=======
    def configure_attempt_metadata(self, **metadata: Any) -> None:
        self._attempt_metadata = {str(key): value for key, value in metadata.items() if value not in (None, "")}

    def snapshot_attempt_state(self, *, boundary: str, published_at: float) -> AttemptSnapshot:
        payload = dict(self._attempt_metadata)
        payload.update({"published_boundary": boundary, "published_at": published_at})
        return AttemptSnapshot(**payload)

    def save_side_effect_receipt(self, receipt: SideEffectReceipt) -> Path:
        ensure_directory(self.side_effect_dir)
        path = self.side_effect_dir / f"{receipt.side_effect_id}.json"
        path.write_text(json.dumps(model_dump(receipt), indent=2, sort_keys=True), encoding="utf-8")
        return path

    def snapshot_checkpoint_shell_state(self) -> ShellStateSnapshot:
        return ShellStateSnapshot(
            short_term_graph=ShortTermGraphSnapshot(**self.short_term.to_jsonable()),
            long_term_graph=LongTermGraphSnapshot(nodes=[model_copy(node, deep=True) for node in self.long_term.all_nodes()]),
            message_board=MessageBoardSnapshot(
                entries=[dict(item) for item in self.message_board.entries],
                cursors={str(key): int(value) for key, value in self.message_board.cursors.items()},
            ),
            open_handles=[model_copy(handle, deep=True) for handle in self.open_handles.handles.values()],
            task_local_tool_registry=self.tool_registry.snapshot_task_local(),
            current_task_id=self._current_task_id or "",
            current_episode_id=self._current_episode_id,
            memory_scope_kind=self._memory_scope_kind or "",
            memory_scope_id=self._memory_scope_id or "",
        )

    def restore_checkpoint_shell_state(self, snapshot: ShellStateSnapshot) -> None:
        self.short_term = ShortTermGraph()
        self.short_term.nodes = copy.deepcopy(snapshot.short_term_graph.nodes)
        self.short_term.edges = [GraphEdge(**dict(edge)) for edge in snapshot.short_term_graph.edges]
        self.short_term.hidden_nodes = set(snapshot.short_term_graph.hidden_nodes)

        self.long_term = LongTermGraph()
        for node in snapshot.long_term_graph.nodes:
            self.long_term.upsert(model_copy(node, deep=True))

        self.message_board = MessageBoard(
            entries=[dict(item) for item in snapshot.message_board.entries],
            cursors={str(key): int(value) for key, value in snapshot.message_board.cursors.items()},
        )
        self.restore_open_handles(snapshot.open_handles)
        self.tool_registry.restore_task_local(snapshot.task_local_tool_registry)
        self._current_task_id = snapshot.current_task_id or None
        self._current_episode_id = snapshot.current_episode_id
        self._memory_scope_kind = snapshot.memory_scope_kind or None
        self._memory_scope_id = snapshot.memory_scope_id or None

    def restore_open_handles(self, handles: Iterable[AsyncHandle]) -> None:
        restored: dict[str, AsyncHandle] = {}
        for handle in handles:
            restored[handle.handle_id] = model_copy(handle, deep=True)
        self.open_handles.handles = restored

    def load_open_handle_output(self, handle: AsyncHandle) -> Any:
        if not handle.artifact_refs:
            return None
        result_path = Path(handle.artifact_refs[0])
        if not result_path.exists():
            return None
        try:
            return json.loads(result_path.read_text(encoding="utf-8"))
        except Exception:
            return None
>>>>>>> REPLACE

File: `agintor/runner.py`
<<<<<<< SEARCH
from .schemas import (
    AgentTemplate,
    AsyncHandle,
    BenchmarkTask,
    BranchBudget,
    BranchPlan,
    BranchPublication,
    BranchResult,
    BranchState,
    CancellationRecord,
    Checkpoint,
    CheckpointEnvelope,
    ChildSpec,
    ExecutionPlan,
    MemoryNode,
    OpenAITraceContext,
    PlanNode,
    QueuedAgentSnapshot,
    QueuedFrameSnapshot,
    RecoveryFailureKind,
    ReceiptReconciliationRecord,
    RunResult,
    SideEffectReceipt,
)
=======
from .schemas import (
    AgentTemplate,
    AsyncHandle,
    BenchmarkTask,
    BranchBudget,
    BranchPlan,
    BranchPublication,
    BranchResult,
    BranchState,
    CancellationRecord,
    Checkpoint,
    CheckpointEnvelope,
    ChildSpec,
    ExecutionPlan,
    MemoryNode,
    OpenAITraceContext,
    PlanNode,
    QueuedAgentSnapshot,
    QueuedFrameSnapshot,
    RecoveryFailureKind,
    ReceiptReconciliationRecord,
    RunResult,
    RuntimeBudgetTotalsSnapshot,
    RuntimeStateSnapshot,
    SideEffectLedgerSnapshot,
    SideEffectReceipt,
    TraceCursorSnapshot,
    VerifierStateSnapshot,
)
>>>>>>> REPLACE

File: `agintor/runner.py`
<<<<<<< SEARCH
    def _restore_from_checkpoint(
        self,
        context: PolicyContext,
        checkpoint_envelope: CheckpointEnvelope,
        *,
        reconciliation_policy: str,
    ) -> None:
        if checkpoint_envelope.request_id != context.request_id:
            raise ResumeRecoveryError(
                RecoveryFailureKind.REQUEST_MISMATCH.value,
                f"checkpoint request mismatch: expected {context.request_id}, found {checkpoint_envelope.request_id}",
            )
        if checkpoint_envelope.plan_id != context.plan.plan_id:
            raise ResumeRecoveryError(
                RecoveryFailureKind.REQUEST_MISMATCH.value,
                f"checkpoint plan mismatch: expected {context.plan.plan_id}, found {checkpoint_envelope.plan_id}",
            )
        if checkpoint_envelope.runtime_abi != self.runtime.kernel_manifest.runtime_abi:
            raise ResumeRecoveryError(
                RecoveryFailureKind.RUNTIME_ABI_MISMATCH.value,
                f"checkpoint runtime ABI mismatch: expected {self.runtime.kernel_manifest.runtime_abi}, found {checkpoint_envelope.runtime_abi}",
            )
        if checkpoint_envelope.storage_schema_version != self.runtime.kernel_manifest.storage_schema_version:
            raise ResumeRecoveryError(
                RecoveryFailureKind.STORAGE_SCHEMA_MISMATCH.value,
                "checkpoint storage schema version does not match the loaded runtime",
            )
        if checkpoint_envelope.runtime_hash != self.runtime.runtime_hash:
            raise ResumeRecoveryError(
                RecoveryFailureKind.RUNTIME_HASH_MISMATCH.value,
                "checkpoint runtime hash does not match the loaded runtime",
            )
        plan_snapshot = model_validate(ExecutionPlan, checkpoint_envelope.plan_snapshot)
        if plan_snapshot.plan_digest != context.plan.plan_digest:
            raise ResumeRecoveryError(
                RecoveryFailureKind.PLAN_DIGEST_MISMATCH.value,
                "checkpoint plan digest does not match the compiled execution plan",
            )
        context.state.plan_node_status = dict(checkpoint_envelope.plan_node_status)
        context.state.unresolved_goals = list(checkpoint_envelope.unresolved_goals)
        context.state.artifacts = dict(checkpoint_envelope.artifact_refs)
        context.state.branch_publications = [model_dump(item) for item in checkpoint_envelope.branch_publications]
        context.state.branch_states = {
            branch_state.branch_id: model_dump(branch_state)
            for branch_state in checkpoint_envelope.branch_state
        }
        self.shell.restore_open_handles(checkpoint_envelope.open_handle_snapshots)
        context.state.open_handle_ids = [handle.handle_id for handle in checkpoint_envelope.open_handle_snapshots]
        if not context.state.open_handle_ids:
            context.state.open_handle_ids = list(checkpoint_envelope.handle_or_job_refs)
        receipts, blocked_node_ids = self._reconcile_side_effect_receipts(
            context,
            checkpoint_envelope.side_effect_receipts,
            reconciliation_policy=reconciliation_policy,
        )
        context.state.side_effect_receipts = [model_dump(receipt) for receipt in receipts]
        for node_id in blocked_node_ids:
            context.state.plan_node_status[node_id] = "recovery_blocked"
        context.state.latest_checkpoint_ref = self.shell.latest_checkpoint_ref(checkpoint_envelope.request_id)
        context.state.checkpoint_sequence_no = int(checkpoint_envelope.sequence_no or 0)
        budget_state = dict(checkpoint_envelope.budget_state)
        context.budget.cost = float(budget_state.get("cost", context.budget.cost) or 0.0)
        context.budget.latency = float(budget_state.get("latency", context.budget.latency) or 0.0)
        context.budget.calls = int(budget_state.get("calls", context.budget.calls) or 0)
        context.budget.checks = int(budget_state.get("checks", context.budget.checks) or 0)
        context.budget.tokens = int(budget_state.get("tokens", context.budget.tokens) or 0)
        context.budget.input_tokens = int(budget_state.get("input_tokens", context.budget.input_tokens) or 0)
        context.budget.output_tokens = int(budget_state.get("output_tokens", context.budget.output_tokens) or 0)
        working_state = dict(checkpoint_envelope.working_state_summary)
        context.state.mode = working_state.get("mode")
        context.state.confidence = float(working_state.get("confidence", context.state.confidence) or 0.0)
        context.state.created_tools = int(working_state.get("created_tools", context.state.created_tools) or 0)
        context.state.promoted_nodes = int(working_state.get("promoted_nodes", context.state.promoted_nodes) or 0)
        context.state.checks_used = int(working_state.get("checks_used", context.state.checks_used) or 0)
        context.state.interface_usage = dict(working_state.get("interface_usage", context.state.interface_usage))
        context.state.subgoal_negative_steps = dict(working_state.get("subgoal_negative_steps", context.state.subgoal_negative_steps))
        context.state.subgoal_last_model = dict(working_state.get("subgoal_last_model", context.state.subgoal_last_model))
        context.state.last_unresolved_goal = working_state.get("last_unresolved_goal")
        context.state.execution_state = str(working_state.get("execution_state", context.state.execution_state) or context.state.execution_state)
        message_entries = working_state.get("message_board_entries")
        if isinstance(message_entries, list):
            context.shell.message_board.entries = [dict(item) for item in message_entries if isinstance(item, dict)]
        message_cursors = working_state.get("message_board_cursors")
        if isinstance(message_cursors, dict):
            context.shell.message_board.cursors = {
                str(key): int(value)
                for key, value in message_cursors.items()
            }
        for frame_snapshot in checkpoint_envelope.queued_frames:
            context.state.queue.append(self._restore_frame_snapshot(context, frame_snapshot))
=======
    def _restore_from_checkpoint(
        self,
        context: PolicyContext,
        checkpoint_envelope: CheckpointEnvelope,
        *,
        reconciliation_policy: str,
    ) -> None:
        if checkpoint_envelope.request_id != context.request_id:
            raise ResumeRecoveryError(
                RecoveryFailureKind.REQUEST_MISMATCH.value,
                f"checkpoint request mismatch: expected {context.request_id}, found {checkpoint_envelope.request_id}",
            )
        if checkpoint_envelope.plan_id != context.plan.plan_id:
            raise ResumeRecoveryError(
                RecoveryFailureKind.REQUEST_MISMATCH.value,
                f"checkpoint plan mismatch: expected {context.plan.plan_id}, found {checkpoint_envelope.plan_id}",
            )
        if checkpoint_envelope.runtime_abi != self.runtime.kernel_manifest.runtime_abi:
            raise ResumeRecoveryError(
                RecoveryFailureKind.RUNTIME_ABI_MISMATCH.value,
                f"checkpoint runtime ABI mismatch: expected {self.runtime.kernel_manifest.runtime_abi}, found {checkpoint_envelope.runtime_abi}",
            )
        if checkpoint_envelope.storage_schema_version != self.runtime.kernel_manifest.storage_schema_version:
            raise ResumeRecoveryError(
                RecoveryFailureKind.STORAGE_SCHEMA_MISMATCH.value,
                "checkpoint storage schema version does not match the loaded runtime",
            )
        if checkpoint_envelope.runtime_hash != self.runtime.runtime_hash:
            raise ResumeRecoveryError(
                RecoveryFailureKind.RUNTIME_HASH_MISMATCH.value,
                "checkpoint runtime hash does not match the loaded runtime",
            )
        plan_snapshot = model_validate(ExecutionPlan, checkpoint_envelope.plan_snapshot)
        if plan_snapshot.plan_digest != context.plan.plan_digest:
            raise ResumeRecoveryError(
                RecoveryFailureKind.PLAN_DIGEST_MISMATCH.value,
                "checkpoint plan digest does not match the compiled execution plan",
            )

        self.shell.restore_checkpoint_shell_state(checkpoint_envelope.shell_state_snapshot)
        self._restore_runtime_state_snapshot(context, checkpoint_envelope.runtime_state_snapshot)

        receipts, blocked_node_ids = self._reconcile_side_effect_receipts(
            context,
            checkpoint_envelope.side_effect_ledger.receipts,
            reconciliation_policy=reconciliation_policy,
        )
        context.state.side_effect_receipts = [model_dump(receipt) for receipt in receipts]
        for node_id in blocked_node_ids:
            context.state.plan_node_status[node_id] = "recovery_blocked"
        context.state.latest_checkpoint_ref = self.shell.latest_checkpoint_ref(checkpoint_envelope.request_id)
        context.active_frame = None

    def _restore_runtime_state_snapshot(
        self,
        context: PolicyContext,
        snapshot: RuntimeStateSnapshot,
    ) -> None:
        context.state.request_id = snapshot.request_id or context.request_id
        context.state.plan_id = snapshot.plan_id or context.plan.plan_id
        context.state.execution_state = snapshot.execution_state
        context.state.active_branch_count = snapshot.active_branch_count
        context.state.checkpoint_sequence_no = snapshot.checkpoint_sequence_no
        context.state.visible_tool_names = list(snapshot.visible_tool_names)
        context.state.unresolved_goals = list(snapshot.unresolved_goals)
        context.state.confidence = snapshot.confidence
        context.state.mode = snapshot.mode
        context.state.created_tools = snapshot.created_tools
        context.state.promoted_nodes = snapshot.promoted_nodes
        context.state.checks_used = snapshot.checks_used
        context.state.interface_usage = dict(snapshot.interface_usage)
        context.state.artifacts = dict(snapshot.artifacts)
        context.state.checkpoints = {
            key: model_validate(Checkpoint, model_dump(value))
            for key, value in snapshot.checkpoints.items()
        }
        context.state.worker_plans = {str(key): dict(value) for key, value in snapshot.worker_plans.items()}
        context.state.open_handle_ids = list(snapshot.open_handle_ids)
        context.state.plan_node_status = dict(snapshot.plan_node_status)
        context.state.branch_states = {
            branch_id: model_dump(branch_state)
            for branch_id, branch_state in snapshot.branch_states.items()
        }
        context.state.branch_publications = [model_dump(item) for item in snapshot.branch_publications]
        context.state.latest_checkpoint_ref = snapshot.latest_checkpoint_ref
        context.state.subgoal_negative_steps = dict(snapshot.subgoal_negative_steps)
        context.state.subgoal_last_model = dict(snapshot.subgoal_last_model)
        context.state.last_unresolved_goal = snapshot.last_unresolved_goal

        context.budget.cost = snapshot.budget_totals.cost
        context.budget.latency = snapshot.budget_totals.latency
        context.budget.calls = snapshot.budget_totals.calls
        context.budget.checks = snapshot.budget_totals.checks
        context.budget.tokens = snapshot.budget_totals.tokens
        context.budget.input_tokens = snapshot.budget_totals.input_tokens
        context.budget.output_tokens = snapshot.budget_totals.output_tokens

        restored_queue: list[AgentFrame] = []
        if snapshot.active_frame is not None:
            restored_queue.append(self._restore_frame_snapshot(context, snapshot.active_frame))
        restored_queue.extend(self._restore_frame_snapshot(context, frame_snapshot) for frame_snapshot in snapshot.queued_frames)
        context.state.queue = restored_queue
>>>>>>> REPLACE

File: `agintor/runner.py`
<<<<<<< SEARCH
    def _record_side_effect_receipt(self, context: PolicyContext, receipt: SideEffectReceipt) -> None:
        normalized = model_validate(SideEffectReceipt, receipt)
        deduped: list[dict[str, Any]] = []
        for payload in context.state.side_effect_receipts[:-1]:
            same_idempotency = str(payload.get("idempotency_key", "")) == normalized.idempotency_key
            same_kind = str(payload.get("action_kind", "")) == normalized.action_kind
            if same_idempotency and same_kind and normalized.status in {"completed", "reconciled", "abandoned"}:
                continue
            deduped.append(payload)
        deduped.append(model_dump(normalized))
        context.state.side_effect_receipts = deduped

    def _publish_checkpoint_envelope(
        self,
        context: PolicyContext,
        task: BenchmarkTask,
        plan: ExecutionPlan,
        seed: int,
        boundary: str,
    ) -> None:
        context.state.checkpoint_sequence_no += 1
        created_at = now_ts()
        queued_frames = []
        if context.active_frame is not None:
            queued_frames.append(self._frame_payload(context.active_frame))
        queued_frames.extend(self._frame_payload(frame) for frame in context.state.queue)
        envelope = CheckpointEnvelope(
            checkpoint_id=f"checkpoint.{plan.request_id}.{context.state.checkpoint_sequence_no:04d}",
            runtime_abi=self.runtime.kernel_manifest.runtime_abi,
            storage_schema_version=self.runtime.kernel_manifest.storage_schema_version,
            runtime_hash=self.runtime.runtime_hash,
            request_id=plan.request_id,
            plan_id=plan.plan_id,
            task_id=task.task_id,
            seed=seed,
            sequence_no=context.state.checkpoint_sequence_no,
            boundary=boundary,
            created_at=created_at,
            plan_snapshot=model_dump(plan),
            task_payload=model_dump(task),
            queued_frames=queued_frames,
            plan_node_status=dict(context.state.plan_node_status),
            branch_state=[
                model_validate(BranchState, payload)
                for payload in context.state.branch_states.values()
            ],
            branch_publications=[
                model_validate(BranchPublication, payload)
                for payload in context.state.branch_publications
            ],
            unresolved_goals=list(context.state.unresolved_goals),
            artifact_refs=dict(context.state.artifacts),
            open_handle_snapshots=[
                model_validate(AsyncHandle, model_dump(handle))
                for handle in context.shell.open_handles.handles.values()
            ],
            handle_or_job_refs=list(context.state.open_handle_ids),
            budget_state={
                "normalized": context.budget.normalized(),
                "cost": context.budget.cost,
                "latency": context.budget.latency,
                "calls": context.budget.calls,
                "checks": context.budget.checks,
                "tokens": context.budget.tokens,
                "input_tokens": context.budget.input_tokens,
                "output_tokens": context.budget.output_tokens,
            },
            verifier_state={
                "checker_ladder": list(plan.verification_plan.checker_ladder),
                "required": plan.verification_plan.required,
            },
            working_state_summary={
                "boundary": boundary,
                "execution_state": context.state.execution_state,
                "mode": context.state.mode,
                "confidence": context.state.confidence,
                "created_tools": context.state.created_tools,
                "promoted_nodes": context.state.promoted_nodes,
                "checks_used": context.state.checks_used,
                "interface_usage": dict(context.state.interface_usage),
                "subgoal_negative_steps": dict(context.state.subgoal_negative_steps),
                "subgoal_last_model": dict(context.state.subgoal_last_model),
                "last_unresolved_goal": context.state.last_unresolved_goal,
                "message_board_entries": list(context.shell.message_board.entries),
                "message_board_cursors": dict(context.shell.message_board.cursors),
            },
            trace_cursor={
                "trace_length": len(context.trace),
                "latest_event": context.trace[-1]["event"] if context.trace else None,
            },
            side_effect_receipts=[
                model_validate(SideEffectReceipt, receipt)
                for receipt in context.state.side_effect_receipts
            ],
        )
        checkpoint_ref = self.shell.save_checkpoint_envelope(envelope)
        context.state.latest_checkpoint_ref = checkpoint_ref.ref
        context.record("checkpoint_published", checkpoint_id=envelope.checkpoint_id, checkpoint_ref=checkpoint_ref.ref, boundary=boundary)
=======
    def _record_side_effect_receipt(self, context: PolicyContext, receipt: SideEffectReceipt) -> None:
        normalized = model_validate(SideEffectReceipt, receipt)
        self.shell.save_side_effect_receipt(normalized)
        deduped: list[dict[str, Any]] = []
        terminal_statuses = {"completed", "reconciled", "abandoned"}
        for payload in context.state.side_effect_receipts:
            same_idempotency = str(payload.get("idempotency_key", "")) == normalized.idempotency_key
            same_kind = str(payload.get("action_kind", "")) == normalized.action_kind
            if same_idempotency and same_kind and normalized.status in terminal_statuses:
                continue
            deduped.append(payload)
        deduped.append(model_dump(normalized))
        context.state.side_effect_receipts = deduped

    def _publish_checkpoint_envelope(
        self,
        context: PolicyContext,
        task: BenchmarkTask,
        plan: ExecutionPlan,
        seed: int,
        boundary: str,
    ) -> None:
        context.state.checkpoint_sequence_no += 1
        created_at = now_ts()
        shell_snapshot = self.shell.snapshot_checkpoint_shell_state()
        envelope = CheckpointEnvelope(
            checkpoint_id=f"checkpoint.{plan.request_id}.{context.state.checkpoint_sequence_no:04d}",
            runtime_abi=self.runtime.kernel_manifest.runtime_abi,
            storage_schema_version=self.runtime.kernel_manifest.storage_schema_version,
            runtime_hash=self.runtime.runtime_hash,
            request_id=plan.request_id,
            plan_id=plan.plan_id,
            task_id=task.task_id,
            seed=seed,
            sequence_no=context.state.checkpoint_sequence_no,
            boundary=boundary,
            created_at=created_at,
            plan_snapshot=model_dump(plan),
            task_payload=model_dump(task),
            runtime_state_snapshot=RuntimeStateSnapshot(
                request_id=context.state.request_id,
                plan_id=context.state.plan_id,
                execution_state=context.state.execution_state,
                active_branch_count=context.state.active_branch_count,
                checkpoint_sequence_no=context.state.checkpoint_sequence_no,
                active_frame=self._frame_payload(context.active_frame) if context.active_frame is not None else None,
                queued_frames=[self._frame_payload(frame) for frame in context.state.queue],
                visible_tool_names=list(context.state.visible_tool_names),
                unresolved_goals=list(context.state.unresolved_goals),
                confidence=context.state.confidence,
                mode=context.state.mode,
                created_tools=context.state.created_tools,
                promoted_nodes=context.state.promoted_nodes,
                checks_used=context.state.checks_used,
                interface_usage=dict(context.state.interface_usage),
                artifacts=dict(context.state.artifacts),
                checkpoints={
                    key: model_validate(Checkpoint, model_dump(value))
                    for key, value in context.state.checkpoints.items()
                },
                worker_plans={str(key): dict(value) for key, value in context.state.worker_plans.items()},
                open_handle_ids=list(context.state.open_handle_ids),
                plan_node_status=dict(context.state.plan_node_status),
                branch_states={
                    branch_id: model_validate(BranchState, payload)
                    for branch_id, payload in context.state.branch_states.items()
                },
                branch_publications=[
                    model_validate(BranchPublication, payload)
                    for payload in context.state.branch_publications
                ],
                latest_checkpoint_ref=context.state.latest_checkpoint_ref,
                subgoal_negative_steps=dict(context.state.subgoal_negative_steps),
                subgoal_last_model=dict(context.state.subgoal_last_model),
                last_unresolved_goal=context.state.last_unresolved_goal,
                budget_totals=RuntimeBudgetTotalsSnapshot(
                    normalized=context.budget.normalized(),
                    cost=context.budget.cost,
                    latency=context.budget.latency,
                    calls=context.budget.calls,
                    checks=context.budget.checks,
                    tokens=context.budget.tokens,
                    input_tokens=context.budget.input_tokens,
                    output_tokens=context.budget.output_tokens,
                ),
                verifier_state=VerifierStateSnapshot(
                    checker_ladder=list(plan.verification_plan.checker_ladder),
                    required=plan.verification_plan.required,
                    exact_verifier_required=plan.verification_plan.exact_verifier_required,
                    verifier_type=plan.verification_plan.verifier_type,
                    terminal_nodes=list(plan.verification_plan.terminal_nodes),
                ),
            ),
            shell_state_snapshot=shell_snapshot,
            side_effect_ledger=SideEffectLedgerSnapshot(
                receipts=[model_validate(SideEffectReceipt, receipt) for receipt in context.state.side_effect_receipts]
            ),
            attempt_snapshot=self.shell.snapshot_attempt_state(boundary=boundary, published_at=created_at),
            working_state_summary={
                "boundary": boundary,
                "execution_state": context.state.execution_state,
                "mode": context.state.mode,
                "active_frame_id": getattr(context.active_frame, "frame_id", None),
                "queued_frame_count": len(context.state.queue),
                "artifact_keys": sorted(context.state.artifacts),
                "short_term_node_count": len(shell_snapshot.short_term_graph.nodes),
                "long_term_node_count": len(shell_snapshot.long_term_graph.nodes),
                "message_count": len(shell_snapshot.message_board.entries),
                "open_handle_count": len(shell_snapshot.open_handles),
                "task_local_tool_count": len(shell_snapshot.task_local_tool_registry.tools),
            },
            trace_cursor=TraceCursorSnapshot(
                trace_length=len(context.trace),
                latest_event=context.trace[-1]["event"] if context.trace else None,
            ),
        )
        checkpoint_ref = self.shell.save_checkpoint_envelope(envelope)
        context.state.latest_checkpoint_ref = checkpoint_ref.ref
        context.record("checkpoint_published", checkpoint_id=envelope.checkpoint_id, checkpoint_ref=checkpoint_ref.ref, boundary=boundary)
>>>>>>> REPLACE

File: `agintor/runner.py`
<<<<<<< SEARCH
    def _reconcile_side_effect_receipts(
        self,
        context: PolicyContext,
        receipts: Sequence[SideEffectReceipt],
        *,
        reconciliation_policy: str,
    ) -> tuple[list[SideEffectReceipt], set[str]]:
        resolved: list[SideEffectReceipt] = []
        blocked_node_ids: set[str] = set()
        completed_by_key = {
            receipt.idempotency_key: receipt
            for receipt in receipts
            if receipt.status == "completed"
        }
        for receipt in receipts:
            if receipt.status == "completed":
                resolved.append(receipt)
                continue
            completed_receipt = completed_by_key.get(receipt.idempotency_key)
            if completed_receipt is not None:
                resolved.append(completed_receipt)
                context.record(
                    "side_effect_reconciled",
                    side_effect_id=completed_receipt.side_effect_id,
                    action_kind=completed_receipt.action_kind,
                    reconciliation_status="reused_completed_receipt",
                )
                continue
            if receipt.action_kind == "tool_launch":
                handle_id = str((receipt.result_ref or {}).get("handle_id", "")).strip()
                handle = self.shell.open_handles.handles.get(handle_id) if handle_id else None
                if handle is not None:
                    output = None
                    if handle.state == "running" and hasattr(self.shell.tool_executor, "await_handle"):
                        finished = self.shell.tool_executor.await_handle(handle.handle_id, self.shell.open_handles)
                        output = finished.get("output")
                        handle = self.shell.open_handles.get(handle.handle_id)
                    elif handle.state == "completed":
                        output = self.shell.load_open_handle_output(handle)
                    if handle.state == "completed":
                        resolved.append(
                            receipt.copy(
                                update={
                                    "side_effect_id": f"tool-completion.{receipt.idempotency_key[:12]}",
                                    "action_kind": "tool_completion",
                                    "status": "reconciled",
                                    "result_ref": {"handle_id": handle.handle_id, "output": output},
                                    "reconciliation": ReceiptReconciliationRecord(
                                        status="completed_from_handle",
                                        details={"handle_id": handle.handle_id},
                                        reconciled_at=now_ts(),
                                    ),
                                }
                            )
                        )
                        context.record(
                            "side_effect_reconciled",
                            side_effect_id=receipt.side_effect_id,
                            action_kind=receipt.action_kind,
                            reconciliation_status="completed_from_handle",
                        )
                        continue
            if receipt.action_kind == "provider_request" and hasattr(self.provider, "reconcile_request"):
                reconciled = self.provider.reconcile_request(receipt.idempotency_key, receipt)
                if reconciled is not None:
                    resolved.append(
                        SideEffectReceipt(
                            side_effect_id=f"provider-completion.{receipt.idempotency_key[:12]}",
                            action_fingerprint=receipt.action_fingerprint,
                            idempotency_key=receipt.idempotency_key,
                            action_kind="provider_completion",
                            request_id=receipt.request_id,
                            plan_id=receipt.plan_id,
                            frame_id=receipt.frame_id,
                            node_id=receipt.node_id,
                            branch_id=receipt.branch_id,
                            trace_context=receipt.trace_context,
                            request_digest=receipt.request_digest,
                            backend=receipt.backend,
                            status="reconciled",
                            result_ref=model_dump(reconciled) if hasattr(reconciled, "model_dump") else dict(reconciled),
                            reconciliation=ReceiptReconciliationRecord(
                                status="reconciled_from_provider_hook",
                                details={"idempotency_key": receipt.idempotency_key},
                                reconciled_at=now_ts(),
                            ),
                            created_at=receipt.created_at,
                        )
                    )
                    context.record(
                        "side_effect_reconciled",
                        side_effect_id=receipt.side_effect_id,
                        action_kind=receipt.action_kind,
                        reconciliation_status="reconciled_from_provider_hook",
                    )
                    continue
            if reconciliation_policy == "strict":
                raise ResumeRecoveryError(
                    RecoveryFailureKind.RECEIPT_RECONCILIATION_FAILED.value,
                    f"strict resume requires reconciled side effects; unresolved receipt {receipt.side_effect_id}",
                )
            if receipt.node_id:
                blocked_node_ids.add(receipt.node_id)
            resolved.append(
                receipt.copy(
                    update={
                        "status": "abandoned",
                        "reconciliation": ReceiptReconciliationRecord(
                            status="blocked_best_effort",
                            details={"node_id": receipt.node_id, "action_kind": receipt.action_kind},
                            reconciled_at=now_ts(),
                        ),
                    }
                )
            )
            context.record(
                "side_effect_reconciled",
                side_effect_id=receipt.side_effect_id,
                action_kind=receipt.action_kind,
                reconciliation_status="blocked_best_effort",
            )
        return resolved, blocked_node_ids
=======
    def _reconcile_side_effect_receipts(
        self,
        context: PolicyContext,
        receipts: Sequence[SideEffectReceipt],
        *,
        reconciliation_policy: str,
    ) -> tuple[list[SideEffectReceipt], set[str]]:
        resolved: list[SideEffectReceipt] = []
        blocked_node_ids: set[str] = set()
        terminal_statuses = {"completed", "reconciled"}
        terminal_by_key = {
            receipt.idempotency_key: receipt
            for receipt in receipts
            if receipt.status in terminal_statuses
        }
        for receipt in receipts:
            if receipt.status in terminal_statuses:
                resolved.append(receipt)
                continue

            terminal_receipt = terminal_by_key.get(receipt.idempotency_key)
            if terminal_receipt is not None:
                resolved.append(terminal_receipt)
                context.record(
                    "side_effect_reconciled",
                    side_effect_id=terminal_receipt.side_effect_id,
                    action_kind=terminal_receipt.action_kind,
                    reconciliation_status="reused_terminal_receipt",
                )
                continue

            if receipt.action_kind == "tool_launch":
                handle_id = str((receipt.result_ref or {}).get("handle_id", "")).strip()
                handle = self.shell.open_handles.handles.get(handle_id) if handle_id else None
                if handle is not None:
                    output = None
                    if handle.state == "running" and hasattr(self.shell.tool_executor, "await_handle"):
                        finished = self.shell.tool_executor.await_handle(handle.handle_id, self.shell.open_handles)
                        output = finished.get("output")
                        handle = self.shell.open_handles.get(handle.handle_id)
                    elif handle.state == "completed":
                        output = self.shell.load_open_handle_output(handle)
                    if handle.state == "completed":
                        resolved.append(
                            receipt.copy(
                                update={
                                    "side_effect_id": f"tool-completion.{receipt.idempotency_key[:12]}",
                                    "action_kind": "tool_completion",
                                    "status": "reconciled",
                                    "result_ref": {"handle_id": handle.handle_id, "output": output},
                                    "reconciliation": ReceiptReconciliationRecord(
                                        status="completed_from_handle",
                                        details={"handle_id": handle.handle_id},
                                        reconciled_at=now_ts(),
                                    ),
                                }
                            )
                        )
                        context.record(
                            "side_effect_reconciled",
                            side_effect_id=receipt.side_effect_id,
                            action_kind=receipt.action_kind,
                            reconciliation_status="completed_from_handle",
                        )
                        continue

            if receipt.action_kind == "provider_request" and hasattr(self.provider, "reconcile_request"):
                reconciled = self.provider.reconcile_request(receipt.idempotency_key, receipt)
                if reconciled is not None:
                    resolved.append(
                        SideEffectReceipt(
                            side_effect_id=f"provider-completion.{receipt.idempotency_key[:12]}",
                            action_fingerprint=receipt.action_fingerprint,
                            idempotency_key=receipt.idempotency_key,
                            action_kind="provider_completion",
                            request_id=receipt.request_id,
                            plan_id=receipt.plan_id,
                            frame_id=receipt.frame_id,
                            node_id=receipt.node_id,
                            branch_id=receipt.branch_id,
                            trace_context=receipt.trace_context,
                            request_digest=receipt.request_digest,
                            backend=receipt.backend,
                            status="reconciled",
                            result_ref=model_dump(reconciled) if hasattr(reconciled, "model_dump") else dict(reconciled),
                            reconciliation=ReceiptReconciliationRecord(
                                status="reconciled_from_provider_hook",
                                details={"idempotency_key": receipt.idempotency_key},
                                reconciled_at=now_ts(),
                            ),
                            created_at=receipt.created_at,
                        )
                    )
                    context.record(
                        "side_effect_reconciled",
                        side_effect_id=receipt.side_effect_id,
                        action_kind=receipt.action_kind,
                        reconciliation_status="reconciled_from_provider_hook",
                    )
                    continue

            if reconciliation_policy == "strict":
                raise ResumeRecoveryError(
                    RecoveryFailureKind.RECEIPT_RECONCILIATION_FAILED.value,
                    f"strict resume requires reconciled side effects; unresolved receipt {receipt.side_effect_id}",
                )
            if receipt.node_id:
                blocked_node_ids.add(receipt.node_id)
            resolved.append(
                receipt.copy(
                    update={
                        "status": "abandoned",
                        "reconciliation": ReceiptReconciliationRecord(
                            status="blocked_best_effort",
                            details={"node_id": receipt.node_id, "action_kind": receipt.action_kind},
                            reconciled_at=now_ts(),
                        ),
                    }
                )
            )
            context.record(
                "side_effect_reconciled",
                side_effect_id=receipt.side_effect_id,
                action_kind=receipt.action_kind,
                reconciliation_status="blocked_best_effort",
            )
        return resolved, blocked_node_ids
>>>>>>> REPLACE

File: `agintor/runtime_api.py`
<<<<<<< SEARCH
        idempotency_trace_context = model_dump(effective_trace_context)
        idempotency_trace_context.pop("run_node_id", None)
        request_digest = stable_hash(instructions, prompt, model_class, payload or {}, idempotency_trace_context)
        for receipt_payload in self.state.side_effect_receipts:
            if str(receipt_payload.get("idempotency_key", "")) != request_digest:
                continue
            if str(receipt_payload.get("status", "")) != "completed":
                continue
            result_ref = receipt_payload.get("result_ref") or {}
            self.record(
                "side_effect_reconciled",
                side_effect_id=receipt_payload.get("side_effect_id"),
                action_kind="provider_completion",
            )
            return ModelResponse(
                text=str(result_ref.get("text", "")),
                raw={"replayed_from_receipt": receipt_payload.get("side_effect_id")},
                model_name=result_ref.get("model_name"),
                input_tokens=int(result_ref.get("input_tokens", 0) or 0),
                output_tokens=int(result_ref.get("output_tokens", 0) or 0),
                token_estimate=int(result_ref.get("input_tokens", 0) or 0) + int(result_ref.get("output_tokens", 0) or 0),
                latency_s=0.0,
                dollar_cost=0.0,
            )
=======
        idempotency_trace_context = model_dump(effective_trace_context)
        idempotency_trace_context.pop("run_node_id", None)
        request_digest = stable_hash(instructions, prompt, model_class, payload or {}, idempotency_trace_context)
        unresolved_launch = False
        for receipt_payload in self.state.side_effect_receipts:
            if str(receipt_payload.get("idempotency_key", "")) != request_digest:
                continue
            status = str(receipt_payload.get("status", ""))
            if status in {"completed", "reconciled"}:
                result_ref = receipt_payload.get("result_ref") or {}
                self.record(
                    "side_effect_reconciled",
                    side_effect_id=receipt_payload.get("side_effect_id"),
                    action_kind="provider_completion",
                )
                return ModelResponse(
                    text=str(result_ref.get("text", "")),
                    raw={"replayed_from_receipt": receipt_payload.get("side_effect_id")},
                    model_name=result_ref.get("model_name"),
                    input_tokens=int(result_ref.get("input_tokens", 0) or 0),
                    output_tokens=int(result_ref.get("output_tokens", 0) or 0),
                    token_estimate=int(result_ref.get("input_tokens", 0) or 0) + int(result_ref.get("output_tokens", 0) or 0),
                    latency_s=0.0,
                    dollar_cost=0.0,
                )
            if str(receipt_payload.get("action_kind", "")) == "provider_request" and status == "launched":
                unresolved_launch = True
        if unresolved_launch:
            raise HardInvalidation("provider request was already launched and must be reconciled before reissue")
>>>>>>> REPLACE

File: `agintor/runner.py`
<<<<<<< SEARCH
        side_effect_key = stable_hash(context.request_id, operation.node_id, tool_name, dict(args))
        for receipt_payload in context.state.side_effect_receipts:
            if str(receipt_payload.get("idempotency_key", "")) != side_effect_key:
                continue
            if str(receipt_payload.get("status", "")) != "completed":
                continue
            result_ref = receipt_payload.get("result_ref") or {}
            context.record(
                "side_effect_reconciled",
                side_effect_id=receipt_payload.get("side_effect_id"),
                action_kind="tool_completion",
            )
            if "output" in result_ref:
                return result_ref.get("output"), tool_name, created_tool, faults
=======
        side_effect_key = stable_hash(context.request_id, operation.node_id, tool_name, dict(args))
        unresolved_launch = False
        for receipt_payload in context.state.side_effect_receipts:
            if str(receipt_payload.get("idempotency_key", "")) != side_effect_key:
                continue
            status = str(receipt_payload.get("status", ""))
            if status in {"completed", "reconciled"}:
                result_ref = receipt_payload.get("result_ref") or {}
                context.record(
                    "side_effect_reconciled",
                    side_effect_id=receipt_payload.get("side_effect_id"),
                    action_kind="tool_completion",
                )
                if "output" in result_ref:
                    return result_ref.get("output"), tool_name, created_tool, faults
            if str(receipt_payload.get("action_kind", "")) == "tool_launch" and status == "launched":
                unresolved_launch = True
        if unresolved_launch:
            raise HardInvalidation("tool execution was already launched and must be reconciled before reissue")
>>>>>>> REPLACE

File: `tests/test_runtime_execution.py`
<<<<<<< SEARCH
    assert envelope.budget_state["calls"] == 1
=======
    assert envelope.runtime_state_snapshot.budget_totals.calls == 1
    assert envelope.runtime_state_snapshot.request_id == first_run.request_id
    assert envelope.shell_state_snapshot.message_board.entries == []
    assert envelope.side_effect_ledger.receipts
>>>>>>> REPLACE

File: `tests/test_runtime_execution.py`
<<<<<<< SEARCH
def test_resume_from_after_branch_completion_reuses_saved_branch_frontier(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    _force_horizontal(monkeypatch, runtime, ["w0", "w1"])
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_direct_response_task("horizontal.resume")
    first_runner = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([{"text": "same"}]),
        budget_overrides={"M_max": 4, "Q_max": 1},
    )
    first_run = first_runner.run_task(task, 0)
    envelope = _checkpoint_for_boundary(shell, first_run.request_id, "after_branch_completion")

    resume_runner = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([]),
        budget_overrides={"M_max": 4, "Q_max": 1},
    )
    resumed_run = resume_runner.resume_from_checkpoint(envelope)

    assert resumed_run.hard_invalid is False
    assert resumed_run.model_calls == first_run.model_calls
    assert resumed_run.artifact == first_run.artifact
=======
def test_resume_from_after_branch_completion_reuses_saved_branch_frontier(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    _force_horizontal(monkeypatch, runtime, ["w0", "w1"])
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_direct_response_task("horizontal.resume")
    first_runner = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([{"text": "same"}]),
        budget_overrides={"M_max": 4, "Q_max": 1},
    )
    first_run = first_runner.run_task(task, 0)
    envelope = _checkpoint_for_boundary(shell, first_run.request_id, "after_branch_completion")

    resume_runner = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([]),
        budget_overrides={"M_max": 4, "Q_max": 1},
    )
    resumed_run = resume_runner.resume_from_checkpoint(envelope)

    assert resumed_run.hard_invalid is False
    assert resumed_run.model_calls == first_run.model_calls
    assert resumed_run.artifact == first_run.artifact


def test_resume_reuses_reconciled_provider_completion_without_generate(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_direct_response_task("resume.reconciled-provider")
    plan = compile_execution_plan_from_task(
        task,
        request_id="resume.reconciled-provider",
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    envelope = CheckpointEnvelope(
        checkpoint_id="checkpoint.resume.reconciled-provider.0001",
        runtime_abi=runtime.kernel_manifest.runtime_abi,
        storage_schema_version=runtime.kernel_manifest.storage_schema_version,
        runtime_hash=runtime.runtime_hash,
        request_id=plan.request_id,
        plan_id=plan.plan_id,
        task_id=task.task_id,
        seed=0,
        sequence_no=1,
        boundary="after_provider_launch",
        created_at=now_ts(),
        plan_snapshot=model_dump(plan),
        task_payload=model_dump(task),
        runtime_state_snapshot={
            "request_id": plan.request_id,
            "plan_id": plan.plan_id,
            "execution_state": "running",
            "active_frame": {
                "frame_id": "frame-root",
                "request_id": plan.request_id,
                "plan_id": plan.plan_id,
                "objective": plan.objective,
                "operation_ids": ["respond"],
                "depth": 0,
                "role": "root",
                "trace_context": model_dump(plan.trace_context),
                "agent_snapshot": model_dump(_canonical_root_snapshot()),
            },
            "plan_node_status": {"respond": "running"},
        },
        side_effect_ledger={
            "receipts": [
                model_dump(
                    SideEffectReceipt(
                        side_effect_id="provider-completion.reconciled",
                        action_fingerprint="provider-completion.reconciled",
                        idempotency_key="provider-request.pending",
                        action_kind="provider_completion",
                        request_id=plan.request_id,
                        plan_id=plan.plan_id,
                        frame_id="frame-root",
                        node_id="respond",
                        request_digest="provider-request.pending",
                        backend="local",
                        status="reconciled",
                        trace_context=OpenAITraceContext(request_id=plan.request_id, task_id=task.task_id, seed=0, op_id="respond"),
                        result_ref={"text": "hello from receipt", "model_name": "replay/small", "input_tokens": 1, "output_tokens": 1},
                        created_at=now_ts(),
                    )
                )
            ]
        },
    )

    provider = ReconcilingReplayProvider([])
    resumed = TaskRuntime(runtime, shell, provider).resume_from_checkpoint(envelope)

    assert provider.generate_calls == 0
    assert resumed.hard_invalid is False
    assert resumed.artifact == "hello from receipt"


def test_resume_uses_typed_snapshots_not_working_state_summary(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_direct_response_task("resume.typed-snapshot")
    plan = compile_execution_plan_from_task(
        task,
        request_id="resume.typed-snapshot",
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    envelope = CheckpointEnvelope(
        checkpoint_id="checkpoint.resume.typed-snapshot.0001",
        runtime_abi=runtime.kernel_manifest.runtime_abi,
        storage_schema_version=runtime.kernel_manifest.storage_schema_version,
        runtime_hash=runtime.runtime_hash,
        request_id=plan.request_id,
        plan_id=plan.plan_id,
        task_id=task.task_id,
        seed=0,
        sequence_no=1,
        boundary="after_provider_completion",
        created_at=now_ts(),
        plan_snapshot=model_dump(plan),
        task_payload=model_dump(task),
        runtime_state_snapshot={
            "request_id": plan.request_id,
            "plan_id": plan.plan_id,
            "execution_state": "running",
            "mode": "single",
            "artifacts": {"response": "typed-artifact"},
            "plan_node_status": {"respond": "completed"},
            "active_frame": {
                "frame_id": "frame-root",
                "request_id": plan.request_id,
                "plan_id": plan.plan_id,
                "objective": plan.objective,
                "operation_ids": ["respond"],
                "depth": 0,
                "role": "root",
                "trace_context": model_dump(plan.trace_context),
                "agent_snapshot": model_dump(_canonical_root_snapshot()),
            },
            "budget_totals": {"calls": 1},
        },
        shell_state_snapshot={
            "message_board": {
                "entries": [{"worker_id": "w0", "kind": "from-snapshot"}],
                "cursors": {"w0": 1},
            }
        },
        side_effect_ledger={"receipts": []},
        working_state_summary={
            "mode": "horizontal",
            "message_board_entries": [{"worker_id": "wrong", "kind": "diagnostic-only"}],
            "message_board_cursors": {"wrong": 999},
        },
    )

    resumed = TaskRuntime(runtime, shell, ReplayProvider([])).resume_from_checkpoint(envelope)

    assert resumed.hard_invalid is False
    assert resumed.mode == "single"
    assert resumed.artifact == {"response": "typed-artifact"}
    assert shell.message_board.entries == [{"worker_id": "w0", "kind": "from-snapshot"}]
>>>>>>> REPLACE

Notes for the orchestrator:
- I intentionally did not propose `runtime_host.py`, `runtime_sdk/runtime_entry.py`, `run_store.py`, or run-manifest patches here beyond what the schema already needs, because Worker 01 owns durable run-root creation, manifest lifecycle, and resume targeting.
- I intentionally did not propose frontier-only branch selection, plan arg separation, or batch episode ordering here, because Worker 03 owns those semantics.
- If you want stricter organization after merge, the two new resume-specific tests can be moved into `tests/test_resume_snapshot.py`, but I kept them inside `tests/test_runtime_execution.py` so the SEARCH/REPLACE proposal stays self-contained.
