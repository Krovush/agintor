from __future__ import annotations

import time
from typing import Any, Mapping, Sequence
from ....core.exceptions import BranchCancelled, HardInvalidation, ProviderExhaustedError, ResumeRecoveryError
from ....tracing import resolve_trace_session_id, trace_grouping_key, trace_session_dir_name
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
    CHECKPOINT_ENVELOPE_SCHEMA_VERSION,
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
    EnvironmentFingerprint,
    ExecutionPlan,
    FingerprintDelta,
    MemoryNode,
    OpenAITraceContext,
    PlanNode,
    QueuedAgentSnapshot,
    QueuedFrameSnapshot,
    RecoveryFailureKind,
    RecoveryAttempt,
    ReceiptReconciliationRecord,
    ReplayAllocation,
    RunResult,
    SideEffectReceipt,
    TraceCursorSnapshot,
    VerifiedFactRef,
    WorkingMemorySnapshot,
    capability_scope_allows,
    plan_node_requires_default_provider,
    service_action_transport_compatibility,
    is_terminal_receipt,
    terminalize_receipt,
)
from ....utils import count_tokens_rough, ensure_directory, merge_provider_usage, now_ts, stable_hash

class CheckpointSnapshotMixin:
    def _build_working_memory_snapshot(
        self,
        context: PolicyContext,
        *,
        boundary: str,
    ) -> WorkingMemorySnapshot:
        verified_facts: list[VerifiedFactRef] = []
        for receipt_payload in context.state.side_effect_receipts:
            receipt = (SideEffectReceipt).model_validate(receipt_payload)
            if receipt.status not in {"completed", "reconciled"}:
                continue
            result_ref = dict(receipt.result_ref or {})
            if "text" not in result_ref and "output" not in result_ref:
                continue
            content = str(result_ref.get("text", result_ref.get("output", "")))[:500]
            if not content.strip():
                continue
            verified_facts.append(
                VerifiedFactRef(
                    fact_id=f"fact.{stable_hash(receipt.side_effect_id, content)[:16]}",
                    content=content,
                    supporting_receipt_ids=[receipt.side_effect_id],
                )
            )
        branch_refs = sorted(
            str(branch_id)
            for branch_id, payload in context.state.branch_states.items()
            if str(payload.get("status", "") or "") in {"running", "completed", "published"}
        )
        selected_refs = sorted(
            {
                ref
                for ref in [
                    context.state.latest_checkpoint_ref,
                    *[
                        str(payload.get("checkpoint_ref", "") or "")
                        for payload in context.state.branch_resume_snapshots.values()
                        if isinstance(payload, Mapping)
                    ],
                ]
                if str(ref or "").strip()
            }
        )
        warnings = []
        if not context.state.unresolved_goals:
            active_summary = f"{boundary}: no unresolved goals"
        else:
            active_summary = f"{boundary}: {len(context.state.unresolved_goals)} unresolved goal(s)"
            warnings = [str(goal) for goal in context.state.unresolved_goals[:5]]
        return WorkingMemorySnapshot(
            current_objective=context.objective or context.task.prompt,
            accepted_constraints=list(context.task.symbolic_seeds) + list(context.task.file_paths),
            active_plan_summary=active_summary,
            verified_facts=verified_facts[:10],
            unresolved_critical_items=list(context.state.unresolved_goals),
            active_branch_refs=branch_refs,
            selected_checkpoint_refs=selected_refs,
            active_recovery_warnings=warnings,
            captured_at=now_ts(),
        )

    def _build_trace_cursor_snapshot(
        self,
        context: PolicyContext,
        task: BenchmarkTask,
        seed: int,
    ) -> TraceCursorSnapshot:
        latest_event = context.trace[-1] if context.trace else {}
        trace_context = context.trace_context
        linked_call_ids = sorted(
            {
                str(row.get("trace_call_id") or row.get("call_id") or row.get("openai_call_id") or "")
                for row in context.trace
                if isinstance(row, Mapping)
                and str(row.get("trace_call_id") or row.get("call_id") or row.get("openai_call_id") or "").strip()
            }
        )
        grouping = trace_grouping_key(trace_context)
        runtime_task_key = grouping[1] if grouping is not None else ""
        resolved_session_id = resolve_trace_session_id(trace_context.session_id)
        return TraceCursorSnapshot(
            runtime_trace_length=len(context.trace),
            latest_runtime_event=str(latest_event.get("event") or "") or None,
            latest_runtime_event_sequence_no=int(context.state.event_sequence_no or 0),
            last_session_id=resolved_session_id,
            last_build_id=trace_context.build_id,
            last_solve_request_id=context.request_id,
            last_runtime_task_key=runtime_task_key,
            linked_call_ids=linked_call_ids,
            materialization_state_ref=(
                f"openai_api_traces/sessions/{trace_session_dir_name(resolved_session_id)}/materialization_state.json"
            ),
            captured_at=now_ts(),
        )

    def _capture_environment_fingerprint(
        self,
        context: PolicyContext,
        *,
        source_checkpoint_ref: str | None,
    ) -> EnvironmentFingerprint:
        kernel_manifest = self.runtime.kernel_manifest
        provider_identity = [context.provider.__class__.__name__]
        tool_runtime_ids = sorted(self.shell.tool_registry.tools.keys())
        dependency_digest = stable_hash(getattr(kernel_manifest, "files", {}))
        fingerprint = EnvironmentFingerprint(
            runtime_backend=context.runtime_backend,
            runtime_hash=self.runtime.runtime_hash,
            runtime_contract_version=kernel_manifest.runtime_contract_version,
            runtime_isolation_policy=context.runtime_backend,
            supported_guarantees=[
                "checkpoint_envelopes",
                "canonical_json_run_store",
                "sqlite_state_index",
                "typed_subsystem_snapshots",
            ],
            provider_identity=provider_identity,
            model_class=getattr(context.profile, "default_model_class", None),
            sandbox_hash=stable_hash(str(getattr(self.shell.artifact_policy, "sandbox_root", ""))),
            tool_runtime_ids=tool_runtime_ids,
            dependency_digest=dependency_digest,
            filesystem_policy=str(getattr(self.shell.artifact_policy, "mode", "")),
            network_policy="host_policy",
            captured_at=now_ts(),
            source_attempt_id=str(getattr(self.shell, "attempt_id", "") or "") or None,
            source_checkpoint_ref=source_checkpoint_ref,
        )
        if getattr(self.shell, "run_store", None) is not None:
            self.shell.run_store.write_environment_fingerprint(self.shell.run_root, fingerprint)
        return fingerprint

    def _fingerprint_deltas(
        self,
        source_fingerprint: EnvironmentFingerprint | None,
        current_fingerprint: EnvironmentFingerprint,
    ) -> list[FingerprintDelta]:
        if source_fingerprint is None:
            return []
        source_payload = EnvironmentFingerprint.content_payload((source_fingerprint).model_dump())
        current_payload = EnvironmentFingerprint.content_payload((current_fingerprint).model_dump())
        deltas: list[FingerprintDelta] = []
        for field_name in EnvironmentFingerprint.content_field_names():
            if source_payload.get(field_name) != current_payload.get(field_name):
                deltas.append(
                    FingerprintDelta(
                        field=field_name,
                        previous=source_payload.get(field_name),
                        current=current_payload.get(field_name),
                    )
                )
        return deltas
