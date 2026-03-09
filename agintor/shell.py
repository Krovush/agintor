from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .exceptions import HardInvalidation
from .memory_graph import LongTermGraph, ShortTermGraph
from .predictors import DecisionFamilyModelBank
from .pydantic_compat import model_copy
from .schemas import AgentTemplate, AsyncHandle
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
            required = [handle.handle_id, handle.tool_name, handle.sandbox_hash, handle.working_directory, handle.stdout_path, handle.stderr_path, handle.state]
            if any(value in (None, "") for value in required):
                raise HardInvalidation("open-handle table becomes inconsistent")

    def to_jsonable(self) -> list[dict[str, Any]]:
        return [handle.dict() for handle in self.handles.values()]


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
            self._agents[agent.agent_id] = model_copy(agent, deep=True)

    def list(self) -> list[AgentTemplate]:
        return [model_copy(agent, deep=True) for agent in self._agents.values()]

    def get_canonical(self, agent_id: str) -> AgentTemplate:
        agent = model_copy(self._agents[agent_id], deep=True)
        setattr(agent, "_canonical", True)
        setattr(agent, "_clone", False)
        return agent

    def clone(self, agent_id: str) -> AgentTemplate:
        agent = model_copy(self._agents[agent_id], deep=True)
        setattr(agent, "_canonical", False)
        setattr(agent, "_clone", True)
        return agent

    def assert_clone(self, agent: AgentTemplate) -> None:
        if getattr(agent, "_canonical", False) or not getattr(agent, "_clone", False):
            raise HardInvalidation("canonical stored agent executed directly instead of clone-on-run")


class FixedShell:
    def __init__(self, workspace: Path) -> None:
        self.workspace = ensure_directory(workspace)
        self.short_term = ShortTermGraph()
        self.long_term = LongTermGraph()
        self.message_board = MessageBoard()
        self.open_handles = OpenHandleTable()
        self.predictors = DecisionFamilyModelBank()
        self.agent_pool = AgentPool()
        self.safety_guard = SafetyGuard()
        self.sandbox_manager = SandboxManager(self.workspace / "sandboxes")
        self.tool_registry = ToolRegistry(self.sandbox_manager, self.safety_guard)
        self.tool_executor = ToolExecutor(self.tool_registry, self.sandbox_manager)
        self.trace_dir = ensure_directory(self.workspace / "traces")

    def reset_for_task(self, transfer_scored: bool = False) -> None:
        self.short_term = ShortTermGraph()
        self.message_board = MessageBoard()
        self.open_handles = OpenHandleTable()
        self.tool_registry.reset_task_local()
        if not transfer_scored:
            self.long_term.reset()

    def save_trace(self, task_id: str, seed: int, trace: list[dict[str, Any]]) -> Path:
        path = self.trace_dir / f"{task_id.replace('/', '_')}_{seed}.json"
        path.write_text(json.dumps(trace, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def validate_invariants(self, transfer_scored: bool = False) -> None:
        self.open_handles.validate()
        if transfer_scored is False:
            # In this MVP, reset_for_task clears long-term memory. A non-empty graph after reset
            # at independent-task boundaries would indicate carryover.
            pass
