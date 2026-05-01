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

def _clone_provider(provider, *, provider_profile=None):
    return clone_provider(provider, provider_profile=provider_profile)


class BranchProviderMixin:
    def _branch_provider_overrides(self) -> dict[str, ModelProvider]:
        overrides = getattr(self, "_replay_branch_providers", None)
        if overrides is None:
            overrides = {}
            setattr(self, "_replay_branch_providers", overrides)
        return overrides

    def _prepare_branch_provider(
        self,
        plan: ExecutionPlan,
        branch_plan: BranchPlan,
        resume_snapshot: BranchResumeSnapshot | None = None,
    ) -> tuple[BranchPlan, ModelProvider]:
        if not isinstance(self.provider, ReplayProvider):
            return branch_plan, _clone_provider(
                self.provider,
                provider_profile=self.runtime_profile.runtime_provider,
            )
        allocation = branch_plan.replay_allocation
        if allocation is not None and self.provider.can_apply_allocation(allocation):
            prepared_plan = (branch_plan).model_copy(update={"replay_allocation": allocation}, deep=True)
            return prepared_plan, self.provider.clone_for_allocation(allocation)
        provider_node_ids = {
            str(node_id)
            for node_id in branch_plan.assigned_node_ids
            if plan_node_requires_default_provider(self._plan_node_by_id(plan, str(node_id)))
        }
        provider_calls = len(provider_node_ids)
        if resume_snapshot is not None:
            completed_nodes = {
                str(node_id)
                for node_id, status in dict(resume_snapshot.plan_node_status).items()
                if str(status) == "completed"
            }
            unresolved_provider_launch_nodes = {
                str(receipt.node_id or "")
                for receipt in resume_snapshot.side_effect_receipts
                if receipt.action_kind == "provider_request"
                and not is_terminal_receipt(receipt)
                and str(receipt.node_id or "").strip()
            }
            remaining_provider_nodes = provider_node_ids - completed_nodes - unresolved_provider_launch_nodes
            provider_calls = len(remaining_provider_nodes)
        if provider_calls <= 0:
            return branch_plan, _clone_provider(
                self.provider,
                provider_profile=self.runtime_profile.runtime_provider,
            )
        allocation_key = (
            allocation.allocation_key
            if allocation is not None and str(allocation.allocation_key or "").strip()
            else f"{plan.request_id}:{branch_plan.branch_id}"
        )
        try:
            allocation = self.provider.reserve_rows(
                provider_calls,
                allocation_key=allocation_key,
            )
        except ProviderExhaustedError as exc:
            raise HardInvalidation(
                f"replay allocation exhausted for branch {branch_plan.branch_id}: need {provider_calls} rows"
            ) from exc
        prepared_plan = (branch_plan).model_copy(update={"replay_allocation": allocation}, deep=True)
        return prepared_plan, self.provider.clone_for_allocation(allocation)

    @staticmethod
    def _branch_plan_with_updated_replay_cursor(
        branch_plan: BranchPlan,
        branch_provider: ModelProvider,
    ) -> BranchPlan:
        if not isinstance(branch_provider, ReplayProvider):
            return branch_plan
        allocation = branch_provider.current_allocation()
        if allocation is None:
            return branch_plan
        allocation_key = (
            branch_plan.replay_allocation.allocation_key
            if branch_plan.replay_allocation is not None
            else f"{branch_plan.request_id}:{branch_plan.branch_id}"
        )
        return (branch_plan).model_copy(update={"replay_allocation": allocation.model_copy(update={"allocation_key": allocation_key}, deep=True)}, deep=True)
