from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from threading import Event, Lock
from typing import Any, Mapping, Sequence
from ....core.exceptions import BranchCancelled, HardInvalidation, ProviderExhaustedError, ResumeRecoveryError
from ....providers import (
    ModelProvider,
    ReplayProvider,
    clone_provider,
    known_provider_environment_names,
    provider_environment_names,
    provider_environment_names_for_instance,
)
from ...api import (
    AgentFrame,
    PolicyContext,
    RuntimeBudget,
    RuntimeState,
    compile_execution_plan_from_task,
    get_plan_node_descriptor,
    normalize_benchmark_request_id,
)
from ....contracts import (
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
from ....utils import count_tokens_rough, ensure_directory, merge_provider_usage, now_ts, stable_hash

class BranchBudgetMixin:
    def _branch_minimum_model_calls(self, plan: ExecutionPlan, worker: Mapping[str, Any]) -> int:
        count = 0
        for node_id in worker.get("op_ids", []):
            node = self._plan_node_by_id(plan, str(node_id))
            descriptor = get_plan_node_descriptor(str(node.node_kind))
            if descriptor.provider_backed_metadata_key:
                if bool(node.metadata.get(descriptor.provider_backed_metadata_key)):
                    count += 1
            elif descriptor.requires_default_provider:
                count += 1
        return count

    def _branch_minimum_latency(self, plan: ExecutionPlan, worker: Mapping[str, Any]) -> float:
        branch_latency_floor_s = max(1.0, float(self.runtime_profile.execution.branch_latency_floor_s or 0.0))
        latency_slices = 0
        for node_id in worker.get("op_ids", []):
            node_kind = str(self._plan_node_by_id(plan, str(node_id)).node_kind)
            if node_kind in {"merge", "checkpoint", "memory_lookup"}:
                continue
            if node_kind in {"direct_response", "tool_synthesis", "tool_call", "repo_patch", "verify", "service_action"}:
                latency_slices += 1
        return float(latency_slices) * branch_latency_floor_s

    def _apportion_integer_budget(
        self,
        total: int,
        weights: Sequence[float],
        minimums: Sequence[int],
    ) -> list[int]:
        if not weights:
            return []
        if total < sum(minimums):
            return []
        allocations = list(minimums)
        remainder = total - sum(allocations)
        if remainder <= 0:
            return allocations
        normalized_weights = [max(0.0, float(weight)) for weight in weights]
        weight_total = sum(normalized_weights) or float(len(normalized_weights))
        ideal_extras = [(weight / weight_total) * remainder for weight in normalized_weights]
        base_extras = [int(value) for value in ideal_extras]
        allocations = [allocation + extra for allocation, extra in zip(allocations, base_extras)]
        assigned = sum(base_extras)
        order = sorted(
            range(len(normalized_weights)),
            key=lambda idx: (
                ideal_extras[idx] - base_extras[idx],
                normalized_weights[idx],
                -idx,
            ),
            reverse=True,
        )
        for idx in order[: remainder - assigned]:
            allocations[idx] += 1
        return allocations

    def _apportion_latency_budget(
        self,
        total: float,
        weights: Sequence[float],
        minimums: Sequence[float],
    ) -> list[float]:
        if not weights:
            return []
        minimum_total = sum(float(value) for value in minimums)
        if float(total) + 1e-9 < minimum_total:
            return []
        allocations = [float(value) for value in minimums]
        remainder = max(0.0, float(total) - minimum_total)
        if remainder <= 1e-9:
            return allocations
        normalized_weights = [max(0.0, float(weight)) for weight in weights]
        weight_total = sum(normalized_weights) or float(len(normalized_weights))
        remaining = remainder
        for index, weight in enumerate(normalized_weights):
            if index == len(normalized_weights) - 1:
                allocations[index] += max(0.0, remaining)
                break
            share = round((weight / weight_total) * remainder, 6)
            share = min(max(0.0, share), remaining)
            allocations[index] += share
            remaining -= share
        return allocations

    def _launchable_branch_plans(
        self,
        context: PolicyContext,
        frame: AgentFrame,
        plan: ExecutionPlan,
        workers: Sequence[dict[str, Any]],
    ) -> list[BranchPlan]:
        remaining_calls = context.budget.remaining_model_calls()
        remaining_checks = context.budget.remaining_checks()
        remaining_latency = context.budget.remaining_latency()
        ranked_workers = sorted(
            list(workers),
            key=lambda worker: (
                -float(worker.get("predicted_solve", 0.0) or 0.0),
                str(worker.get("worker_id", "")),
            ),
        )
        branch_specs: list[dict[str, Any]] = []
        for worker in ranked_workers:
            predicted_solve = max(0.01, float(worker.get("predicted_solve", 0.0) or 0.0))
            branch_specs.append(
                {
                    "worker": worker,
                    "weight": predicted_solve,
                    "min_calls": self._branch_minimum_model_calls(plan, worker),
                    "min_checks": 0,
                    "min_latency": self._branch_minimum_latency(plan, worker),
                }
            )
        while branch_specs and (
            sum(spec["min_calls"] for spec in branch_specs) > remaining_calls
            or sum(spec["min_checks"] for spec in branch_specs) > remaining_checks
            or sum(spec["min_latency"] for spec in branch_specs) > remaining_latency + 1e-9
        ):
            branch_specs.pop()
        if not branch_specs:
            return []
        call_allocations = self._apportion_integer_budget(
            remaining_calls,
            [spec["weight"] for spec in branch_specs],
            [spec["min_calls"] for spec in branch_specs],
        )
        check_allocations = self._apportion_integer_budget(
            remaining_checks,
            [spec["weight"] for spec in branch_specs],
            [spec["min_checks"] for spec in branch_specs],
        )
        latency_allocations = self._apportion_latency_budget(
            remaining_latency,
            [spec["weight"] for spec in branch_specs],
            [spec["min_latency"] for spec in branch_specs],
        )
        if not call_allocations or not check_allocations or not latency_allocations:
            return []
        branch_plans: list[BranchPlan] = []
        for index, spec in enumerate(branch_specs):
            worker = spec["worker"]
            branch_id = str(worker.get("worker_id", f"branch_{index}"))
            branch_plans.append(
                BranchPlan(
                    branch_id=branch_id,
                    parent_frame_id=frame.frame_id,
                    request_id=plan.request_id,
                    trace_context=context.derive_trace_context(
                        worker_id=branch_id,
                        frame_role="worker",
                        agent_id=str(worker.get("agent_id", "root")),
                    ),
                    assigned_node_ids=list(worker.get("op_ids", [])),
                    merge_priority=index,
                    predicted_solve=float(worker.get("predicted_solve", 0.0) or 0.0),
                    reserved_budget=BranchBudget(
                        model_calls_max=call_allocations[index],
                        checks_max=check_allocations[index],
                        latency_max=latency_allocations[index],
                        allow_tool_synthesis=bool(plan.execution_flags.allow_tool_synthesis),
                    ),
                    cancel_on_parent_stop=True,
                )
            )
        return branch_plans
