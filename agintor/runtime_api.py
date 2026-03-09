from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .benchmarks import BenchmarkTask
from .providers import ModelProvider
from .schemas import AgentTemplate, Checkpoint


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


@dataclass
class RuntimeBudget:
    cost: float = 0.0
    latency: float = 0.0
    calls: int = 0
    checks: int = 0
    C_max: float = 100.0
    L_max: float = 120.0
    M_max: int = 64
    Q_max: int = 16

    def normalized(self) -> dict[str, float]:
        return {
            "cost": self.cost / max(1.0, self.C_max),
            "latency": self.latency / max(1.0, self.L_max),
            "calls": self.calls / max(1, self.M_max),
            "checks": self.checks / max(1, self.Q_max),
        }

    def exhausted(self) -> bool:
        n = self.normalized()
        return any(value >= 1.0 for value in n.values())


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

    def record(self, event: str, **payload: Any) -> None:
        self.trace.append({"event": event, **payload})
