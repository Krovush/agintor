from __future__ import annotations

from typing import Any, Mapping, Sequence
from ...core.exceptions import BranchCancelled, HardInvalidation, ProviderExhaustedError, ResumeRecoveryError
from ..api import (
    AgentFrame,
    PolicyContext,
    RuntimeBudget,
    RuntimeState,
    compile_execution_plan_from_task,
    get_plan_node_descriptor,
    normalize_benchmark_request_id,
)
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
    SideEffectReceipt,
    capability_scope_allows,
    plan_node_requires_default_provider,
    service_action_transport_compatibility,
    is_terminal_receipt,
    terminalize_receipt,
)
from ..tools.validation import _signature_arg_names
from ...utils import count_tokens_rough, ensure_directory, merge_provider_usage, now_ts, stable_hash


def _category_allowed(allowed_categories: Sequence[str], category_key: str | None) -> bool:
    return capability_scope_allows(allowed_categories, category_key)


class ToolingMixin:
    def _dedupe_tools(self, tools: Sequence[Any]) -> list[Any]:
        deduped: dict[str, Any] = {}
        for tool in tools:
            deduped[tool.spec.name] = tool
        return list(deduped.values())

    def _discover_candidate_tools(
        self,
        context: PolicyContext,
        frame: AgentFrame,
        operation: Any,
    ) -> list[Any]:
        allowed_categories = list(context.task.allowed_tool_categories)
        category_summaries = {
            category_key: summary
            for category_key, summary in self.shell.tool_registry.category_summaries.items()
            if _category_allowed(allowed_categories, category_key)
        }
        categories = self.runtime.tooling.rank_categories(
            context,
            operation,
            category_summaries,
        )
        inspected_categories = categories[: context.profile.tooling.k_c]
        candidate_tools: list[Any] = []
        for category in inspected_categories:
            candidate_tools.extend(self.shell.tool_registry.tools_in_category(category))
        candidate_tools = [
            tool
            for tool in candidate_tools
            if _category_allowed(allowed_categories, tool.category_key)
        ]
        if frame.tool_scope:
            allowed = set(frame.tool_scope)
            candidate_tools = [tool for tool in candidate_tools if tool.spec.name in allowed]
        return self._dedupe_tools(candidate_tools)

    def _execute_tool_operation(
        self,
        context: PolicyContext,
        frame: AgentFrame,
        operation: Any,
        args: Mapping[str, Any],
        run_node_id: str | None,
    ) -> tuple[Any, str, bool, int]:
        context.raise_if_cancelled()
        faults = 0
        candidate_tools = self._discover_candidate_tools(context, frame, operation)
        ranked_tool_names = self.runtime.tooling.rank_tools(context, operation, candidate_tools)
        candidate_tool_names = {tool.spec.name for tool in candidate_tools}
        created_tool = False
        hinted_tool_usable = (
            operation.tool_hint
            and operation.tool_hint in context.state.visible_tool_names
            and operation.tool_hint in candidate_tool_names
        )
        if hinted_tool_usable:
            hint_signature = self.shell.tool_registry.get(operation.tool_hint).spec.signature
            hinted_tool_usable = set(args) <= set(_signature_arg_names(hint_signature))
        if hinted_tool_usable:
            tool_name = operation.tool_hint
        elif ranked_tool_names:
            tool_name = ranked_tool_names[0]
        else:
            tool_name = None
        generated_allowed = _category_allowed(context.task.allowed_tool_categories, "generated/local")
        if operation.node_kind == "tool_synthesis" and generated_allowed and self.runtime.tooling.should_create_tool(context, operation, ranked_tool_names):
            synth_name = operation.tool_hint or f"synth:{operation.node_id}"
            try:
                spec, source, executor = self.runtime.tooling.propose_tool_spec(context, operation, dict(args))
                if self.runtime.tooling.validate_tool(context, spec, source):
                    self.shell.tool_registry.register_generated_tool(spec, source, executor=executor)
                    tool_name = spec.name
                    created_tool = True
                    context.state.created_tools += 1
                    context.state.visible_tool_names.append(tool_name)
            except HardInvalidation:
                raise
            except Exception as exc:
                faults += 1
                stderr = str(exc)
                context.record("tool_fault", tool=synth_name, stderr=stderr)
                self._record_tool_failure(context, operation, synth_name, stderr)
                created_tool = False
                if tool_name is None:
                    raise HardInvalidation("no tool available after category-first discovery") from exc
        if tool_name is None:
            raise HardInvalidation("no tool available after category-first discovery")
        side_effect_key = stable_hash(context.request_id, operation.node_id, tool_name, dict(args))
        unresolved_launch = False
        terminal_receipt: SideEffectReceipt | None = None
        for receipt_payload in context.state.side_effect_receipts:
            receipt = (SideEffectReceipt).model_validate(receipt_payload)
            if receipt.idempotency_key != side_effect_key:
                continue
            if is_terminal_receipt(receipt):
                terminal_receipt = receipt
                continue
            if receipt.action_kind == "tool_launch" and receipt.status == "launched":
                unresolved_launch = True
        if terminal_receipt is not None:
            result_ref = dict(terminal_receipt.result_ref or {})
            if terminal_receipt.status in {"completed", "reconciled"} and "output" in result_ref:
                context.record(
                    "side_effect_reconciled",
                    side_effect_id=terminal_receipt.side_effect_id,
                    action_kind=terminal_receipt.action_kind,
                    reconciliation_status=terminal_receipt.status,
                )
                return result_ref.get("output"), tool_name, created_tool, faults
            raise HardInvalidation(
                f"tool execution {side_effect_key[:12]} already has terminal receipt status {terminal_receipt.status!r}"
            )
        if unresolved_launch:
            raise HardInvalidation("tool execution was already launched and must be reconciled before reissue")
        tool_trace_context = context.derive_trace_context(
            agent_id=frame.agent.agent_id,
            frame_role=frame.role,
            worker_id=frame.worker_id,
            op_id=operation.node_id,
        )
        dispatch_meta = self.runtime.tooling.dispatch_tool(context, tool_name, args)
        if dispatch_meta.get("async"):
            handle_fingerprint = side_effect_key
            context.raise_if_cancelled()
            handle = self.shell.tool_executor.launch_async(
                tool_name,
                args,
                self.shell.workspace / "handles",
                context.task.task_id,
            )
            self.shell.open_handles.add(handle)
            context.state.open_handle_ids.append(handle.handle_id)
            context.record_side_effect(
                SideEffectReceipt(
                    side_effect_id=f"tool-launch.{handle_fingerprint[:12]}",
                    action_fingerprint=handle_fingerprint,
                    idempotency_key=handle_fingerprint,
                    action_kind="tool_launch",
                    request_id=context.request_id,
                    plan_id=context.plan.plan_id,
                    frame_id=frame.frame_id,
                    node_id=operation.node_id,
                    branch_id=frame.worker_id,
                    trace_context=tool_trace_context,
                    request_digest=handle_fingerprint,
                    backend=context.runtime_backend,
                    status="launched",
                    result_ref={"tool_name": tool_name, "launch_mode": "async", "handle_id": handle.handle_id},
                    replay_policy="reconcile_before_reissue",
                    reconciliation_policy="strict",
                    created_at=now_ts(),
                )
            )
            context.publish_checkpoint_boundary("after_tool_launch")
            handle_node_id = self.shell.short_term.add_node("OpenHandle", handle.tool_name, (handle).model_dump())
            if run_node_id and run_node_id in self.shell.short_term.nodes:
                self.shell.short_term.add_edge(run_node_id, handle_node_id, "WAITS_ON")
            context.raise_if_cancelled()
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
            elif hasattr(self.shell.tool_executor, "wait_async"):
                result = self.shell.tool_executor.wait_async(handle)
                context.budget.consume_tool_latency(result.latency_s)
                if not result.success:
                    faults += 1
                    context.record("tool_fault", tool=tool_name, stderr=result.stderr)
                    self._record_tool_failure(context, operation, tool_name, result.stderr)
                    raise HardInvalidation(f"tool execution failed for {tool_name}: {result.stderr}")
                output = result.output
                self.shell.open_handles.update_state(handle.handle_id, "completed")
            else:
                self.shell.open_handles.update_state(handle.handle_id, "completed")
                output = None
            completion_fingerprint = stable_hash(context.request_id, operation.node_id, tool_name, dict(args), handle.handle_id)
            context.record_side_effect(
                SideEffectReceipt(
                    side_effect_id=f"tool-completion.{completion_fingerprint[:12]}",
                    action_fingerprint=completion_fingerprint,
                    idempotency_key=handle_fingerprint,
                    action_kind="tool_completion",
                    request_id=context.request_id,
                    plan_id=context.plan.plan_id,
                    frame_id=frame.frame_id,
                    node_id=operation.node_id,
                    branch_id=frame.worker_id,
                    trace_context=tool_trace_context,
                    request_digest=completion_fingerprint,
                    backend=context.runtime_backend,
                    status="completed",
                    result_ref={
                        "tool_name": tool_name,
                        "launch_mode": "async",
                        "handle_id": handle.handle_id,
                        "output": output,
                    },
                    replay_policy="reuse_if_completed",
                    reconciliation_policy="strict",
                    created_at=now_ts(),
                )
            )
            context.publish_checkpoint_boundary("after_tool_completion")
        else:
            sync_fingerprint = side_effect_key
            context.record_side_effect(
                SideEffectReceipt(
                    side_effect_id=f"tool-launch.{sync_fingerprint[:12]}",
                    action_fingerprint=sync_fingerprint,
                    idempotency_key=sync_fingerprint,
                    action_kind="tool_launch",
                    request_id=context.request_id,
                    plan_id=context.plan.plan_id,
                    frame_id=frame.frame_id,
                    node_id=operation.node_id,
                    branch_id=frame.worker_id,
                    trace_context=tool_trace_context,
                    request_digest=sync_fingerprint,
                    backend=context.runtime_backend,
                    status="launched",
                    result_ref={"tool_name": tool_name, "launch_mode": "sync"},
                    replay_policy="reconcile_before_reissue",
                    reconciliation_policy="strict",
                    created_at=now_ts(),
                )
            )
            context.publish_checkpoint_boundary("after_tool_launch")
            context.raise_if_cancelled()
            try:
                result = self.shell.tool_executor.run_tool(tool_name, args, context.task.task_id)
            except Exception as exc:
                faults += 1
                stderr = str(exc)
                context.record("tool_fault", tool=tool_name, stderr=stderr)
                self._record_tool_failure(context, operation, tool_name, stderr)
                raise HardInvalidation(f"tool execution failed for {tool_name}: {stderr}") from exc
            context.budget.consume_tool_latency(result.latency_s)
            if not result.success:
                context.record_side_effect(
                    SideEffectReceipt(
                        side_effect_id=f"tool-completion.{sync_fingerprint[:12]}",
                        action_fingerprint=sync_fingerprint,
                        idempotency_key=sync_fingerprint,
                        action_kind="tool_completion",
                        request_id=context.request_id,
                        plan_id=context.plan.plan_id,
                        frame_id=frame.frame_id,
                        node_id=operation.node_id,
                        branch_id=frame.worker_id,
                        trace_context=tool_trace_context,
                        request_digest=sync_fingerprint,
                        backend=context.runtime_backend,
                        status="failed",
                        result_ref={"tool_name": tool_name, "launch_mode": "sync", "stderr": result.stderr},
                        replay_policy="reuse_if_completed",
                        reconciliation_policy="strict",
                        created_at=now_ts(),
                    )
                )
                context.publish_checkpoint_boundary("after_tool_completion")
                faults += 1
                context.record("tool_fault", tool=tool_name, stderr=result.stderr)
                self._record_tool_failure(context, operation, tool_name, result.stderr)
                raise HardInvalidation(f"tool execution failed for {tool_name}: {result.stderr}")
            output = result.output
            context.record_side_effect(
                SideEffectReceipt(
                    side_effect_id=f"tool-completion.{sync_fingerprint[:12]}",
                    action_fingerprint=sync_fingerprint,
                    idempotency_key=sync_fingerprint,
                    action_kind="tool_completion",
                    request_id=context.request_id,
                    plan_id=context.plan.plan_id,
                    frame_id=frame.frame_id,
                    node_id=operation.node_id,
                    branch_id=frame.worker_id,
                    trace_context=tool_trace_context,
                    request_digest=sync_fingerprint,
                    backend=context.runtime_backend,
                    status="completed",
                    result_ref={"tool_name": tool_name, "launch_mode": "sync", "output": output},
                    replay_policy="reuse_if_completed",
                    reconciliation_policy="strict",
                    created_at=now_ts(),
                )
            )
            context.publish_checkpoint_boundary("after_tool_completion")
        context.raise_if_cancelled()
        tool = self.shell.tool_registry.get(tool_name)
        if operation.node_kind == "tool_synthesis":
            self._record_procedure(context, operation, tool_name)
        if created_tool and self.runtime.tooling.promote_tool(context, tool):
            context.record("tool_promoted", tool=tool_name)
        return output, tool_name, created_tool, faults

    def _record_tool_failure(self, context: PolicyContext, operation: Any, tool_name: str, stderr: str) -> None:
        candidate = MemoryNode(
            node_id=stable_hash(context.task.task_id, operation.node_id, tool_name, stderr)[:16],
            type="ToolFailure",
            label=tool_name,
            content=stderr,
            embedding=[],
            symbol_set=[operation.node_id],
            file_paths=[],
            source_task_id=context.task.task_id,
            verifier_support=0.0,
            timestamps={"created": now_ts()},
            provenance={"source": "tool_fault", "operation": operation.node_id},
            tombstoned=False,
        )
        self._promote_memory_candidate(context, candidate)

    def _record_procedure(self, context: PolicyContext, operation: Any, tool_name: str) -> None:
        expression = getattr(operation, "expression", None) or operation.metadata.get("expression")
        if not expression:
            return
        candidate = MemoryNode(
            node_id=stable_hash(context.task.task_id, tool_name, expression)[:16],
            type="Procedure",
            label=tool_name,
            content=expression,
            embedding=[],
            symbol_set=[operation.node_id],
            file_paths=[],
            source_task_id=context.task.task_id,
            verifier_support=0.6,
            timestamps={"created": now_ts()},
            provenance={"source": "generated_expression"},
            tombstoned=False,
        )
        self._promote_memory_candidate(context, candidate)
