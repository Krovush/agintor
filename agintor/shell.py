from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .artifacts import ArtifactMode, ArtifactPolicy
from .exceptions import HardInvalidation
from .memory_graph import GraphEdge, LongTermGraph, ShortTermGraph
from .predictors import DecisionFamilyModelBank
from .run_store import RunStore, _write_json_atomic
from .schemas import (
    AgentTemplate,
    AsyncHandle,
    AttemptSnapshot,
    BranchPublication,
    CheckpointEnvelope,
    CheckpointReference,
    MessageBoardSnapshot,
    OpenHandleTableSnapshot,
    RuntimeEvent,
    ShellStateSnapshot,
    SideEffectReceipt,
)
from .tool_runtime import SafetyGuard, SandboxManager, ToolExecutor, ToolRegistry
from .utils import ensure_directory, now_ts, stable_hash


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

    def snapshot(self) -> MessageBoardSnapshot:
        return MessageBoardSnapshot(
            entries=[dict(entry) for entry in self.entries],
            cursors={str(worker_id): int(cursor) for worker_id, cursor in self.cursors.items()},
        )

    def restore(self, snapshot: Mapping[str, Any] | MessageBoardSnapshot) -> None:
        board_snapshot = (
            snapshot
            if isinstance(snapshot, MessageBoardSnapshot)
            else (MessageBoardSnapshot).model_validate(snapshot)
        )
        self.entries = [dict(entry) for entry in board_snapshot.entries]
        self.cursors = {
            str(worker_id): int(cursor)
            for worker_id, cursor in board_snapshot.cursors.items()
        }

    @classmethod
    def fork_from_snapshot(cls, snapshot: Mapping[str, Any] | MessageBoardSnapshot) -> "MessageBoard":
        board = cls()
        board.restore(snapshot)
        return board


class OpenHandleTable:
    def __init__(self) -> None:
        self.handles: dict[str, AsyncHandle] = {}

    def add(self, handle: AsyncHandle) -> None:
        self.handles[handle.handle_id] = handle

    def get(self, handle_id: str) -> AsyncHandle:
        return self.handles[handle_id]

    def update_state(self, handle_id: str, state: str) -> None:
        handle = self.handles[handle_id]
        handle.state = state
        self.handles[handle_id] = handle

    def validate(self) -> None:
        for handle in self.handles.values():
            required = [handle.handle_id, handle.tool_name, handle.sandbox_hash, handle.working_directory, handle.state]
            if any(value in (None, "") for value in required):
                raise HardInvalidation("open-handle table becomes inconsistent")

    def snapshot(self) -> OpenHandleTableSnapshot:
        return OpenHandleTableSnapshot(
            handles=[
                (handle).model_copy(deep=True)
                for handle in sorted(self.handles.values(), key=lambda item: item.handle_id)
            ]
        )

    def restore(self, snapshot: Mapping[str, Any] | OpenHandleTableSnapshot) -> None:
        table_snapshot = (
            snapshot
            if isinstance(snapshot, OpenHandleTableSnapshot)
            else (OpenHandleTableSnapshot).model_validate(snapshot)
        )
        restored: dict[str, AsyncHandle] = {}
        for handle in table_snapshot.handles:
            restored[handle.handle_id] = (handle).model_copy(deep=True)
        self.handles = restored

    @classmethod
    def fork_from_snapshot(cls, snapshot: Mapping[str, Any] | OpenHandleTableSnapshot) -> "OpenHandleTable":
        table = cls()
        table.restore(snapshot)
        return table

    def to_jsonable(self) -> list[dict[str, Any]]:
        return [(handle).model_dump() for handle in self.snapshot().handles]


class AgentPool:
    def __init__(self) -> None:
        self._agents: dict[str, AgentTemplate] = {}
        self._install_defaults()

    def _install_defaults(self) -> None:
        defaults = [
            AgentTemplate(agent_id="root", description="General root coordinator", capability_set=["plan", "merge", "verify"], symbol_set=[], default_tool_scope=[], success_stats={"global": 0.5}, staleness_clock=0, model_policy_tag="medium"),
            AgentTemplate(agent_id="arith", description="Arithmetic specialist", capability_set=["arithmetic", "aggregate"], symbol_set=["sum", "product", "median", "max", "min"], default_tool_scope=["math/basic/sum_numbers", "math/basic/product_numbers", "math/basic/median_number", "math/basic/max_number", "math/basic/min_number"], success_stats={"top": 0.7}, staleness_clock=0, model_policy_tag="small"),
            AgentTemplate(agent_id="memory", description="Memory specialist", capability_set=["memory", "retrieve", "symbol"], symbol_set=["path", "symbol", "lookup"], default_tool_scope=[], success_stats={"mem": 0.7}, staleness_clock=0, model_policy_tag="small"),
            AgentTemplate(agent_id="toolmaker", description="Tool synthesis specialist", capability_set=["tooling", "python", "validate"], symbol_set=["expression", "tool"], default_tool_scope=[], success_stats={"tool": 0.65}, staleness_clock=0, model_policy_tag="medium"),
            AgentTemplate(agent_id="verifier", description="Verification specialist", capability_set=["verify", "check"], symbol_set=["verifier"], default_tool_scope=[], success_stats={"global": 0.75}, staleness_clock=0, model_policy_tag="small"),
        ]
        for agent in defaults:
            self._agents[agent.agent_id] = (agent).model_copy(deep=True)

    def list(self) -> list[AgentTemplate]:
        return [(agent).model_copy(deep=True) for agent in self._agents.values()]

    def get_canonical(self, agent_id: str) -> AgentTemplate:
        agent = (self._agents[agent_id]).model_copy(deep=True)
        setattr(agent, "_canonical", True)
        setattr(agent, "_clone", False)
        return agent

    def clone(self, agent_id: str) -> AgentTemplate:
        agent = (self._agents[agent_id]).model_copy(deep=True)
        setattr(agent, "_canonical", False)
        setattr(agent, "_clone", True)
        return agent

    def assert_clone(self, agent: AgentTemplate) -> None:
        if getattr(agent, "_canonical", False) or not getattr(agent, "_clone", False):
            raise HardInvalidation("canonical stored agent executed directly instead of clone-on-run")


class FixedShell:
    def __init__(
        self,
        workspace: Path,
        predictors: DecisionFamilyModelBank | None = None,
        *,
        artifact_mode: str | ArtifactMode | None = None,
        sandbox_root: Path | None = None,
        run_store: RunStore | None = None,
        run_id: str = "",
        attempt_id: str = "",
    ) -> None:
        self.workspace = Path(workspace)
        self.run_store = run_store
        self.run_id = run_id
        self.attempt_id = attempt_id
        self.run_root = Path(run_store.run_root).resolve() if run_store is not None and run_store.run_root is not None else self.workspace
        self.artifact_policy = ArtifactPolicy.resolve(
            artifact_mode=artifact_mode,
            sandbox_root=sandbox_root,
        )
        self.retain_artifacts = self.artifact_policy.write_traces
        self.short_term = ShortTermGraph()
        self.long_term = LongTermGraph()
        self.message_board = MessageBoard()
        self.open_handles = OpenHandleTable()
        self.predictors = predictors or DecisionFamilyModelBank()
        self._shared_predictors = predictors is not None
        self.agent_pool = AgentPool()
        self.safety_guard = SafetyGuard()
        self.sandbox_manager = SandboxManager(self.artifact_policy.sandbox_root)
        self.tool_registry = ToolRegistry(
            self.sandbox_manager,
            self.safety_guard,
            workspace_root=self.workspace,
        )
        self.tool_executor = ToolExecutor(
            self.tool_registry,
            self.sandbox_manager,
            persist_artifacts=self.artifact_policy.persist_tool_artifacts,
        )
        self.trace_dir = self.run_root / "traces" if self.run_store is not None else self.workspace / "traces"
        self.event_dir = self.run_root / "events" if self.run_store is not None else self.workspace / "events"
        self.checkpoint_dir = self.run_root / "checkpoints" if self.run_store is not None else self.workspace / "checkpoints"
        self.side_effect_dir = self.run_root / "side_effects" if self.run_store is not None else self.workspace / "side_effects"
        self._runtime_event_lock = Lock()
        self._runtime_event_state: dict[str, int] = {"next_sequence_no": 0}
        self._resume_checkpoint_store_dir: Path | None = None
        self._current_task_id: str | None = None
        self._current_episode_id: str | None = None
        self._memory_scope_kind: str | None = None
        self._memory_scope_id: str | None = None
        self._trace_save_counter = 0

    def reset_for_task(self, task_id: str = "", transfer_scored: bool = False, episode_id: str | None = None) -> None:
        self.short_term = ShortTermGraph()
        self.message_board = MessageBoard()
        self.open_handles = OpenHandleTable()
        if not self._shared_predictors:
            self.predictors = DecisionFamilyModelBank()
        self.tool_registry.reset_task_local()
        self._current_task_id = task_id
        self._current_episode_id = episode_id
        memory_scope_kind = "episode" if transfer_scored else "task"
        memory_scope_id = episode_id or task_id
        if memory_scope_kind == "task" or (self._memory_scope_kind, self._memory_scope_id) != (memory_scope_kind, memory_scope_id):
            self.long_term.reset()
        self._memory_scope_kind = memory_scope_kind
        self._memory_scope_id = memory_scope_id

    def save_trace(self, task_id: str, seed: int, trace: list[dict[str, Any]]) -> Path | None:
        if not self.retain_artifacts:
            return None
        ensure_directory(self.trace_dir)
        self._trace_save_counter += 1
        episode_step_index = 0
        for row in reversed(trace):
            if not isinstance(row, Mapping):
                continue
            trace_context = row.get("trace_context")
            if isinstance(trace_context, Mapping) and trace_context.get("episode_step_index") is not None:
                try:
                    episode_step_index = int(trace_context.get("episode_step_index") or 0)
                except Exception:
                    episode_step_index = 0
                break
        prefix = f"{self.attempt_id}." if self.attempt_id else ""
        safe_task_id = task_id.replace("/", "_")
        path = self.trace_dir / f"{prefix}{safe_task_id}_{seed}_step{episode_step_index}_{self._trace_save_counter:04d}.json"
        _write_json_atomic(path, trace)
        return path

    def _latest_runtime_event_sequence(self) -> int:
        if not self.event_dir.exists():
            return 0
        latest = 0
        for path in self.event_dir.glob("*.json"):
            prefix = path.name.split(".", 1)[0]
            if prefix.isdigit():
                latest = max(latest, int(prefix))
        return latest

    def restore_runtime_event_cursor(self, sequence_no: int) -> None:
        with self._runtime_event_lock:
            self._runtime_event_state["next_sequence_no"] = max(
                int(self._runtime_event_state.get("next_sequence_no", 0) or 0),
                int(sequence_no or 0),
                self._latest_runtime_event_sequence(),
            )

    def latest_runtime_event_sequence(self) -> int:
        with self._runtime_event_lock:
            return max(
                int(self._runtime_event_state.get("next_sequence_no", 0) or 0),
                self._latest_runtime_event_sequence(),
            )

    def append_runtime_event(self, event: RuntimeEvent) -> RuntimeEvent:
        ensure_directory(self.event_dir)
        with self._runtime_event_lock:
            next_sequence_no = int(self._runtime_event_state.get("next_sequence_no", 0) or 0) + 1
            self._runtime_event_state["next_sequence_no"] = next_sequence_no
            event_id = event.event_id or f"runtime-event.{next_sequence_no:06d}.{stable_hash(event.request_id, event.plan_id, event.event, next_sequence_no)[:12]}"
            persisted = (event).model_copy(update={"event_id": event_id, "sequence_no": next_sequence_no}, deep=True)
            if self.run_store is not None:
                self.run_store.write_runtime_event(self.run_root, persisted)
            else:
                path = self.event_dir / f"{next_sequence_no:06d}.{persisted.event}.json"
                _write_json_atomic(path, (persisted).model_dump())
            return persisted

    def load_runtime_events(
        self,
        *,
        request_id: str | None = None,
        after_sequence_no: int | None = None,
    ) -> list[RuntimeEvent]:
        if not self.event_dir.exists():
            return []
        events: list[RuntimeEvent] = []
        for path in sorted(self.event_dir.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            event = (RuntimeEvent).model_validate(payload)
            if request_id and str(event.request_id or "") != str(request_id):
                continue
            if after_sequence_no is not None and int(event.sequence_no or 0) <= int(after_sequence_no or 0):
                continue
            events.append(event)
        events.sort(key=lambda item: (int(item.sequence_no or 0), str(item.event_id)))
        return events

    def save_checkpoint_envelope(self, envelope: CheckpointEnvelope) -> CheckpointReference:
        if self.run_store is not None:
            return self.run_store.write_checkpoint(envelope)
        request_dir = ensure_directory(self.checkpoint_dir / envelope.request_id)
        path = request_dir / f"{envelope.checkpoint_id}.json"
        _write_json_atomic(path, (envelope).model_dump())
        index_path = request_dir / "index.json"
        index_rows: list[dict[str, Any]] = []
        if index_path.exists():
            try:
                payload = json.loads(index_path.read_text(encoding="utf-8"))
            except Exception:
                payload = []
            if isinstance(payload, list):
                index_rows = [dict(row) for row in payload if isinstance(row, dict)]
        index_rows.append(
            {
                "checkpoint_ref": str(path),
                "checkpoint_id": envelope.checkpoint_id,
                "sequence_no": envelope.sequence_no,
                "boundary": envelope.boundary,
                "created_at": envelope.created_at,
                "resume_eligible": bool(envelope.resume_eligible),
                "resume_ineligibility_reason": envelope.resume_ineligibility_reason,
            }
        )
        index_rows.sort(key=lambda row: (int(row.get("sequence_no", 0) or 0), str(row.get("checkpoint_id", ""))))
        _write_json_atomic(index_path, index_rows)
        latest_path = request_dir / "LATEST.json"
        latest_eligible = next(
            (row for row in reversed(index_rows) if bool(row.get("resume_eligible", True))),
            None,
        )
        if latest_eligible is not None:
            _write_json_atomic(latest_path, latest_eligible)
        elif latest_path.exists():
            latest_path.unlink()
        checkpoint_count = len(index_rows)
        return CheckpointReference(
            ref=str(path),
            run_id=envelope.run_id,
            run_root=envelope.run_root,
            attempt_id=envelope.attempt_id,
            task_id=envelope.task_id,
            seed=envelope.seed,
            request_id=envelope.request_id,
            plan_id=envelope.plan_id,
            checkpoint_id=envelope.checkpoint_id,
            sequence_no=envelope.sequence_no,
            boundary=envelope.boundary,
            created_at=envelope.created_at,
            checkpoint_count=checkpoint_count,
            latest=bool(latest_eligible and latest_eligible.get("checkpoint_id") == envelope.checkpoint_id),
            resume_eligible=bool(envelope.resume_eligible),
            resume_ineligibility_reason=envelope.resume_ineligibility_reason,
        )

    def configure_resume_checkpoint_store(self, checkpoint_store_dir: str | Path | None) -> None:
        text = str(checkpoint_store_dir or "").strip()
        self._resume_checkpoint_store_dir = Path(text) if text else None

    def _checkpoint_lookup_roots(self, checkpoint_store_dir: str | Path | None = None) -> list[Path]:
        roots = [self.checkpoint_dir]
        extra = checkpoint_store_dir or self._resume_checkpoint_store_dir
        if extra:
            candidate = Path(extra)
            if candidate not in roots:
                roots.append(candidate)
        return roots

    @staticmethod
    def _checkpoint_path_from_index_row(request_dir: Path, row: Mapping[str, Any]) -> str | None:
        checkpoint_ref = str(row.get("checkpoint_ref", "") or "").strip()
        if checkpoint_ref:
            checkpoint_path = Path(checkpoint_ref)
            if checkpoint_path.exists():
                return str(checkpoint_path.resolve())
        checkpoint_id = str(row.get("checkpoint_id", "") or "").strip()
        if checkpoint_id:
            candidate = request_dir / f"{checkpoint_id}.json"
            if candidate.exists():
                return str(candidate.resolve())
        return checkpoint_ref or None

    def latest_checkpoint_ref(self, request_id: str, checkpoint_store_dir: str | Path | None = None) -> str | None:
        if self.run_store is not None:
            run_ref = request_id or self.run_id or str(self.run_root)
            return self.run_store.latest_usable_checkpoint_ref(run_ref)
        for checkpoint_root in self._checkpoint_lookup_roots(checkpoint_store_dir):
            request_dir = checkpoint_root / request_id
            index_path = request_dir / "index.json"
            if index_path.exists():
                try:
                    payload = json.loads(index_path.read_text(encoding="utf-8"))
                except Exception:
                    payload = []
                if isinstance(payload, list) and payload:
                    eligible_rows = [
                        dict(row)
                        for row in payload
                        if isinstance(row, dict) and bool(row.get("resume_eligible", True))
                    ]
                    latest = max(
                        eligible_rows,
                        key=lambda row: (int(row.get("sequence_no", 0) or 0), str(row.get("checkpoint_id", ""))),
                        default=None,
                    )
                    checkpoint_ref = self._checkpoint_path_from_index_row(request_dir, latest or {})
                    if checkpoint_ref:
                        return checkpoint_ref
            latest_path = request_dir / "LATEST.json"
            if not latest_path.exists():
                continue
            try:
                payload = json.loads(latest_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            checkpoint_ref = self._checkpoint_path_from_index_row(request_dir, payload)
            if checkpoint_ref:
                return checkpoint_ref
        return None

    def load_checkpoint_envelope(
        self,
        *,
        checkpoint_ref: str | None = None,
        run_ref: str | None = None,
        request_id: str | None = None,
        checkpoint_store_dir: str | Path | None = None,
    ) -> CheckpointEnvelope:
        target_ref = str(checkpoint_ref or "").strip()
        if not target_ref:
            lookup_ref = run_ref or self.run_id or request_id
            if not lookup_ref:
                raise FileNotFoundError("resume requires checkpoint_ref or run_ref")
            latest = self.latest_checkpoint_ref(str(lookup_ref), checkpoint_store_dir=checkpoint_store_dir)
            if not latest:
                raise FileNotFoundError(f"no latest checkpoint published for {lookup_ref}")
            target_ref = latest
        path = Path(target_ref)
        payload = json.loads(path.read_text(encoding="utf-8"))
        loaded_ref = str(path.resolve())
        envelope = (CheckpointEnvelope).model_validate_persisted(payload)
        update = {"selected_checkpoint_ref": loaded_ref}
        if not str(envelope.source_checkpoint_ref or "").strip():
            update["source_checkpoint_ref"] = loaded_ref
        envelope = envelope.model_copy(update=update, deep=True)
        return envelope

    def save_side_effect_receipt(self, receipt: SideEffectReceipt) -> Path:
        if self.run_store is not None:
            return self.run_store.write_side_effect_receipt(self.run_root, receipt)
        ensure_directory(self.side_effect_dir)
        path = self.side_effect_dir / f"{receipt.side_effect_id}.json"
        _write_json_atomic(path, (receipt).model_dump())
        return path

    def snapshot_attempt_state(self, *, boundary: str, published_at: float) -> AttemptSnapshot:
        return AttemptSnapshot(
            run_id=self.run_id,
            run_root=str(self.run_root),
            attempt_id=self.attempt_id,
            published_boundary=boundary,
            published_at=published_at,
        )

    def snapshot_checkpoint_shell_state(
        self,
        *,
        checkpoint_id: str = "",
        boundary: str = "",
        branch_publications: Iterable[Mapping[str, Any] | BranchPublication] = (),
        side_effect_receipts: Iterable[Mapping[str, Any] | SideEffectReceipt] = (),
        runtime_event_refs: Iterable[str] = (),
    ) -> ShellStateSnapshot:
        safe_checkpoint_id = str(checkpoint_id or "checkpoint").replace("/", "_")
        write_log_ref = f"state/long_term/writes/{safe_checkpoint_id}.jsonl" if checkpoint_id else ""
        diagnostic_ref = f"state/long_term/retrieval/{safe_checkpoint_id}.jsonl" if checkpoint_id else ""
        publication_rows = [
            (item).model_dump() if isinstance(item, BranchPublication) else dict(item)
            for item in branch_publications
        ]
        receipt_rows = [
            (item).model_dump() if isinstance(item, SideEffectReceipt) else dict(item)
            for item in side_effect_receipts
        ]
        return ShellStateSnapshot(
            short_term_graph=self.short_term.snapshot(
                branch_publications=publication_rows,
                side_effect_receipts=receipt_rows,
                runtime_event_refs=list(runtime_event_refs),
            ),
            long_term_graph=self.long_term.snapshot(
                write_log_refs=[write_log_ref] if write_log_ref else [],
                diagnostic_refs=[diagnostic_ref] if diagnostic_ref else [],
            ),
            message_board=self.message_board.snapshot(),
            open_handles=self.open_handles.snapshot(),
            task_local_tool_registry=self.tool_registry.snapshot(),
            predictor_snapshot=self.predictors.snapshot(),
            current_task_id=self._current_task_id or "",
            current_episode_id=self._current_episode_id,
            memory_scope_kind=self._memory_scope_kind or "",
            memory_scope_id=self._memory_scope_id or "",
        )

    def restore_checkpoint_shell_state(self, snapshot: Mapping[str, Any] | ShellStateSnapshot) -> None:
        shell_snapshot = (
            snapshot
            if isinstance(snapshot, ShellStateSnapshot)
            else (ShellStateSnapshot).model_validate(snapshot)
        )
        self.short_term = ShortTermGraph.fork_from_snapshot(shell_snapshot.short_term_graph)
        self.long_term = LongTermGraph.fork_from_snapshot(shell_snapshot.long_term_graph)
        self.message_board = MessageBoard.fork_from_snapshot(shell_snapshot.message_board)
        self.open_handles = OpenHandleTable.fork_from_snapshot(shell_snapshot.open_handles)
        self.tool_registry.restore(shell_snapshot.task_local_tool_registry)
        self.predictors.restore(shell_snapshot.predictor_snapshot)
        self._current_task_id = shell_snapshot.current_task_id or None
        self._current_episode_id = shell_snapshot.current_episode_id
        self._memory_scope_kind = shell_snapshot.memory_scope_kind or None
        self._memory_scope_id = shell_snapshot.memory_scope_id or None

    def restore_open_handles(self, handles: Iterable[AsyncHandle] | OpenHandleTableSnapshot) -> None:
        if isinstance(handles, OpenHandleTableSnapshot):
            self.open_handles.restore(handles)
            return
        self.open_handles.restore(OpenHandleTableSnapshot(handles=list(handles)))

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

    def fork_branch(self, branch_id: str) -> "FixedShell":
        short_term_snapshot = self.short_term.snapshot()
        long_term_snapshot = self.long_term.snapshot()
        message_board_snapshot = self.message_board.snapshot()
        open_handle_snapshot = self.open_handles.snapshot()
        task_local_snapshot = self.tool_registry.snapshot()
        predictor_snapshot = self.predictors.snapshot()

        branch_workspace = ensure_directory(self.workspace / "branches" / branch_id)
        branch_shell = FixedShell(
            branch_workspace,
            artifact_mode=self.artifact_policy.mode,
            sandbox_root=self.artifact_policy.sandbox_root,
            run_store=self.run_store,
            run_id=self.run_id,
            attempt_id=self.attempt_id,
        )
        branch_shell.short_term = ShortTermGraph.fork_from_snapshot(short_term_snapshot)
        branch_shell.long_term = LongTermGraph.fork_from_snapshot(long_term_snapshot)
        branch_shell.message_board = MessageBoard.fork_from_snapshot(message_board_snapshot)
        branch_shell.open_handles = OpenHandleTable.fork_from_snapshot(open_handle_snapshot)
        branch_shell.tool_registry = ToolRegistry.fork_from_snapshot(
            task_local_snapshot,
            sandbox_manager=branch_shell.sandbox_manager,
            safety_guard=branch_shell.safety_guard,
            workspace_root=branch_shell.workspace,
        )
        branch_shell.tool_executor = ToolExecutor(
            branch_shell.tool_registry,
            branch_shell.sandbox_manager,
            persist_artifacts=branch_shell.artifact_policy.persist_tool_artifacts,
        )
        branch_shell.predictors = DecisionFamilyModelBank.fork_from_snapshot(predictor_snapshot)
        branch_shell._shared_predictors = False
        if self.run_store is not None:
            branch_shell.run_root = self.run_root
            branch_shell.trace_dir = self.trace_dir
            branch_shell.checkpoint_dir = self.checkpoint_dir
            branch_shell.side_effect_dir = self.side_effect_dir
        branch_shell.event_dir = self.event_dir
        branch_shell._runtime_event_lock = self._runtime_event_lock
        branch_shell._runtime_event_state = self._runtime_event_state
        branch_shell._current_task_id = self._current_task_id
        branch_shell._current_episode_id = self._current_episode_id
        branch_shell._memory_scope_kind = self._memory_scope_kind
        branch_shell._memory_scope_id = self._memory_scope_id
        return branch_shell

    def validate_invariants(self, transfer_scored: bool = False) -> None:
        self.open_handles.validate()
        self.short_term.validate_hidden_reachability()
        if transfer_scored is False and self._current_task_id is not None:
            if self._memory_scope_kind != "task" or self._memory_scope_id != self._current_task_id:
                raise HardInvalidation("long-term memory carries across tasks when transfer is not explicitly scored")
            leaked = [
                node.node_id
                for node in self.long_term.nodes.values()
                if node.source_task_id not in {"", self._current_task_id}
            ]
            if leaked:
                raise HardInvalidation("long-term memory carries across tasks when transfer is not explicitly scored")
