from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Mapping, Sequence
from ..providers import (
    ModelProvider,
    ReplayProvider,
    clone_provider,
    known_provider_environment_names,
    provider_environment_names,
    provider_environment_names_for_instance,
)
from ..runtime_profile import RuntimeProfile, load_runtime_profile
from ..runtime_api import (
    AgentFrame,
    PolicyContext,
    RuntimeBudget,
    RuntimeState,
    compile_execution_plan_from_task,
    get_plan_node_descriptor,
    normalize_benchmark_request_id,
)
from ..runtime_loader import LoadedRuntime
from ..pydantic_compat import model_copy, model_dump, model_validate
from ..schemas import (
    AgentTemplate,
    AsyncHandle,
    BenchmarkTask,
    BranchBudget,
    BranchPlan,
    BranchPublication,
    BranchResumeSnapshot,
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
    ReplayAllocation,
    RunResult,
    SideEffectReceipt,
    capability_scope_allows,
    plan_node_requires_default_provider,
    service_action_transport_compatibility,
    is_terminal_receipt,
    terminalize_receipt,
)
from ..shell import FixedShell
from ..utils import count_tokens_rough, ensure_directory, merge_provider_usage, now_ts, stable_hash
from .bounded_io import BoundedIOMixin
from .branch_execution import BranchExecutionMixin
from .branching import BranchingMixin
from .checkpointing import CheckpointingMixin
from .execution_loop import ExecutionLoopMixin
from .frames import FramesMixin
from .memory import MemoryMixin
from .operations import OperationsMixin
from .plan_helpers import PlanHelpersMixin
from .side_effects import SideEffectsMixin
from .tooling import ToolingMixin
from .verification import VerificationMixin


class TaskRuntime(ExecutionLoopMixin, CheckpointingMixin, SideEffectsMixin, BranchExecutionMixin, BranchingMixin, PlanHelpersMixin, FramesMixin, MemoryMixin, OperationsMixin, ToolingMixin, BoundedIOMixin, VerificationMixin):
    def __init__(
        self,
        runtime: LoadedRuntime,
        shell: FixedShell,
        provider: ModelProvider,
        budget_overrides: Mapping[str, Any] | None = None,
        runtime_profile: RuntimeProfile | None = None,
        runtime_backend: str | None = None,
    ) -> None:
        self.runtime = runtime
        self.shell = shell
        self.provider = provider
        self.budget_overrides = dict(budget_overrides or {})
        self.runtime_profile = runtime_profile or load_runtime_profile(runtime.runtime_dir)
        self.runtime_backend = str(
            runtime_backend or os.environ.get("AGINTOR_RUNTIME_BACKEND", "local")
        ).strip().lower()

    def _runtime_budget_overrides(self) -> dict[str, Any]:
        profile = self.runtime_profile.execution
        overrides = {
            "C_max": profile.cost_max,
            "L_max": profile.latency_max,
            "M_max": profile.model_calls_max,
            "Q_max": profile.checks_max,
            "context_window_tokens": profile.context_window_tokens,
        }
        overrides.update(self.budget_overrides)
        return overrides

    @contextmanager
    def _isolated_provider_environment(self):
        known_envs = set(known_provider_environment_names(include_api_key_file_env=True))
        known_envs.update(
            provider_environment_names(
                self.runtime_profile.runtime_provider.name,
                provider_profile=self.runtime_profile.runtime_provider,
                include_api_key_file_env=True,
            )
        )
        selected_envs = set(provider_environment_names_for_instance(self.provider))
        removed: dict[str, str] = {}
        for env_name in sorted(known_envs - selected_envs):
            if env_name in os.environ:
                removed[env_name] = os.environ.pop(env_name)
        try:
            yield
        finally:
            for env_name, value in removed.items():
                os.environ[env_name] = value

    @staticmethod
    def _provider_usage_snapshot(provider: ModelProvider | None) -> dict[str, Any]:
        if provider is None:
            return {}
        return dict(provider.usage_summary())

    @classmethod
    def _provider_usage_delta(
        cls,
        before: Mapping[str, Any] | None,
        after: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        delta: dict[str, Any] = {}
        before_payload = dict(before or {})
        after_payload = dict(after or {})
        for key in sorted(set(before_payload) | set(after_payload)):
            previous = before_payload.get(key, 0)
            current = after_payload.get(key, 0)
            if isinstance(previous, (int, float)) and isinstance(current, (int, float)):
                delta[key] = current - previous
            else:
                delta[key] = current
        return delta

    @staticmethod
    def _merge_provider_usage_into(
        target: dict[str, Any],
        payload: Mapping[str, Any] | None,
    ) -> None:
        merged = merge_provider_usage(dict(target), payload)
        target.clear()
        target.update(merged)

    def run_task(
        self,
        task: BenchmarkTask,
        seed: int,
        *,
        request_id: str | None = None,
        trace_context: Any | None = None,
        plan: ExecutionPlan | None = None,
    ) -> RunResult:
        normalized_request_id = request_id or normalize_benchmark_request_id(task.task_id, seed)
        compiled_plan = plan or compile_execution_plan_from_task(
            task,
            request_id=normalized_request_id,
            seed=seed,
            runtime_hash=self.runtime.runtime_hash,
            runtime_dir=str(self.runtime.runtime_dir),
            trace_context=trace_context,
            budget_overrides=self.budget_overrides,
        )
        return self._run_execution_plan(task, compiled_plan, seed)

    def resume_from_checkpoint(
        self,
        envelope: CheckpointEnvelope,
        *,
        reconciliation_policy: str = "strict",
    ) -> RunResult:
        task = model_validate(BenchmarkTask, envelope.task_payload)
        plan = model_validate(ExecutionPlan, envelope.plan_snapshot)
        return self._run_execution_plan(
            task,
            plan,
            envelope.seed,
            checkpoint_envelope=envelope,
            reconciliation_policy=reconciliation_policy,
        )


__all__ = ["TaskRuntime"]
