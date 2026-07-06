from __future__ import annotations

import os
import time
from contextlib import contextmanager
from typing import Any, Mapping, Sequence
from ...providers import (
    ModelProvider,
    ReplayProvider,
    clone_provider,
    known_provider_environment_names,
    provider_environment_names,
    provider_environment_names_for_instance,
)
from ..profile import RuntimeProfile, load_runtime_profile
from ..api import (
    AgentFrame,
    PolicyContext,
    RuntimeBudget,
    RuntimeState,
    compile_execution_plan_from_task,
    get_plan_node_descriptor,
    normalize_benchmark_request_id,
)
from ..loader import LoadedRuntime
from ...contracts import (
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
    RuntimeSessionSeed,
    SideEffectReceipt,
    capability_scope_allows,
    plan_node_requires_default_provider,
    service_action_transport_compatibility,
    is_terminal_receipt,
    terminalize_receipt,
)
from .shell import FixedShell
from ...utils import count_tokens_rough, ensure_directory, merge_provider_usage, now_ts, stable_hash
from .branching import BranchingMixin
from .checkpointing import CheckpointingMixin
from .frames import FramesMixin
from .io import BoundedIOMixin
from .memory import MemoryMixin
from .operations import OperationsMixin
from .plan_helpers import PlanHelpersMixin
from .branches import BranchExecutionMixin
from .side_effects import SideEffectsMixin
from .tooling import ToolingMixin
from .verification import VerificationMixin
from .loop import RuntimeLoopMixin
from .progress import ProgressMixin
from .root_frame import RootFrameMixin


class TaskRuntime(ProgressMixin, RootFrameMixin, RuntimeLoopMixin, CheckpointingMixin, SideEffectsMixin, BranchExecutionMixin, BranchingMixin, PlanHelpersMixin, FramesMixin, MemoryMixin, OperationsMixin, ToolingMixin, BoundedIOMixin, VerificationMixin):
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
        session_seed: RuntimeSessionSeed | None = None,
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
        if getattr(self.runtime, "runtime_spec", None) is not None:
            from ...contracts.verifiers import verify_task_with_evidence
            from ..langgraph.adapters import run_spec_task

            before_usage = self._provider_usage_snapshot(self.provider)
            start = time.perf_counter()
            payload = run_spec_task(
                self.runtime.runtime_dir,
                task,
                request_id=normalized_request_id,
                seed=seed,
                provider=self.provider,
                runtime_hash=self.runtime.runtime_hash,
                trace_context=trace_context,
            )
            latency = time.perf_counter() - start
            trace = [dict(row) for row in payload.get("trace", [])]
            artifact = payload.get("artifact")
            try:
                verifier_score, _ = verify_task_with_evidence(task, artifact, trace)
            except Exception as exc:
                verifier_score = 0.0
                trace.append(
                    {
                        "event": "verifier_failed",
                        "request_id": normalized_request_id,
                        "error": str(exc),
                        "created_at": now_ts(),
                    }
                )
            provider_usage = self._provider_usage_delta(before_usage, self._provider_usage_snapshot(self.provider))
            status = str(payload.get("status") or "completed")
            failed = status == "failed"
            return RunResult(
                request_id=normalized_request_id,
                plan_id=compiled_plan.plan_id,
                run_id=self.shell.run_id,
                run_root=str(self.shell.run_root),
                attempt_id=self.shell.attempt_id,
                runtime_hash=self.runtime.runtime_hash,
                runtime_backend=self.runtime_backend,
                latest_checkpoint_ref=None,
                run_lifecycle_state="failed" if failed else "completed",
                run_resumable=False,
                run_prune_eligible=True,
                task_id=task.task_id,
                seed=seed,
                artifact=artifact,
                verifier_score=verifier_score,
                cost=float(provider_usage.get("dollar_cost", 0.0) or 0.0),
                latency=latency,
                faults=1 if failed else 0,
                trace=trace,
                runtime_evidence_manifest=dict(payload.get("runtime_evidence_manifest", {}) or {}),
                trace_context=trace_context,
                hard_invalid=failed,
                invalid_reason=str(payload.get("error", "")) if failed else None,
                failure_kind="langgraph_spec_failed" if failed else None,
                mode="benchmark",
                lifecycle_state="failed" if failed else "completed",
                model_calls=int(provider_usage.get("calls", 0) or 0),
                tokens_used=int(provider_usage.get("total_tokens", 0) or 0),
                input_tokens=int(provider_usage.get("input_tokens", 0) or 0),
                output_tokens=int(provider_usage.get("output_tokens", 0) or 0),
                provider_usage=provider_usage,
            )
        return self._run_execution_plan(task, compiled_plan, seed, session_seed=session_seed)

    def resume_from_checkpoint(
        self,
        envelope: CheckpointEnvelope,
        *,
        reconciliation_policy: str = "strict",
    ) -> RunResult:
        task = (BenchmarkTask).model_validate(envelope.task_payload)
        plan = (ExecutionPlan).model_validate(envelope.plan_snapshot)
        return self._run_execution_plan(
            task,
            plan,
            envelope.seed,
            checkpoint_envelope=envelope,
            reconciliation_policy=reconciliation_policy,
        )


__all__ = ["TaskRuntime"]
