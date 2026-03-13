from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .benchmarks import BenchmarkTask
from .providers import ModelProvider
from .runtime_profile import RuntimeProfile, default_runtime_profile
from .schemas import AgentTemplate, Checkpoint, ModelResponse


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
    tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    C_max: float = 100.0
    L_max: float = 120.0
    M_max: int = 64
    Q_max: int = 16
    context_window_tokens: int = 768

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

    def consume_model_response(self, response: ModelResponse) -> None:
        self.calls += 1
        self.cost += float(response.dollar_cost)
        self.latency += float(response.latency_s)
        self.input_tokens += int(response.input_tokens)
        self.output_tokens += int(response.output_tokens)
        if response.token_estimate > 0:
            self.tokens += int(response.token_estimate)
        else:
            self.tokens += int(response.input_tokens) + int(response.output_tokens)

    def consume_check(self, count: int = 1, latency_s: float = 0.0) -> None:
        self.checks += int(count)
        self.latency += float(latency_s)

    def consume_tool_latency(self, latency_s: float) -> None:
        self.latency += float(latency_s)


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

    def __post_init__(self) -> None:
        if self.profile is None:
            self.profile = default_runtime_profile()

    def record(self, event: str, **payload: Any) -> None:
        self.trace.append({"event": event, **payload})

    def consume_model_response(self, response: ModelResponse, purpose: str) -> None:
        self.budget.consume_model_response(response)
        self.record(
            "model_response",
            purpose=purpose,
            model_class=response.model_name,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            total_tokens=response.token_estimate,
            dollar_cost=response.dollar_cost,
            latency_s=response.latency_s,
        )
