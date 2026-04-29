from __future__ import annotations

import json
from typing import Any, Mapping, Sequence
from ..exceptions import BranchCancelled, HardInvalidation, ProviderExhaustedError, ResumeRecoveryError
from ..runtime_api import (
    AgentFrame,
    PolicyContext,
    RuntimeBudget,
    RuntimeState,
    compile_execution_plan_from_task,
    get_plan_node_descriptor,
    normalize_benchmark_request_id,
)
from ..memory_graph import LongTermGraph
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
    LongTermGraphSnapshot,
    MemoryNode,
    OpenAITraceContext,
    PlanNode,
    PredictorSnapshot,
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
from ..utils import count_tokens_rough, ensure_directory, merge_provider_usage, now_ts, stable_hash


class MemoryMixin:
    def _apply_session_seed(self, seed: RuntimeSessionSeed) -> None:
        """Hydrate runtime memory from a prior message in the same chat session.

        Carries forward long-term graph and (optionally) predictor state. Open
        handles, side-effect ledger, and message-board sequence are not seeded;
        they remain fresh for the new message.

        ``short_term_carryover`` rows from the prior message are appended to the
        message board and short-term graph as recap entries with provenance
        ``session_carryover`` so the new run's first context-ingestion step sees
        them.
        """
        self.shell.long_term = LongTermGraph.fork_from_snapshot(seed.long_term_graph)
        if seed.predictor_snapshot is not None:
            self.shell.predictors.restore(seed.predictor_snapshot)
        for row in seed.short_term_carryover:
            if not isinstance(row, Mapping):
                continue
            payload = dict(row)
            payload.setdefault("provenance", {"source": "session_carryover", "session_id": seed.session_id})
            payload.setdefault("parent_message_id", seed.parent_message_id)
            self.shell.message_board.append(
                "session",
                {
                    "kind": "session_carryover",
                    "session_id": seed.session_id,
                    "parent_message_id": seed.parent_message_id,
                    "payload": payload,
                },
            )
            label = str(payload.get("label") or payload.get("kind") or "carryover")
            self.shell.short_term.add_node("RawBlob", label, payload)

    def _export_post_message_state(
        self,
        *,
        run_result: RunResult | None = None,
    ) -> tuple[LongTermGraphSnapshot, PredictorSnapshot, list[dict[str, Any]]]:
        """Capture state to seed the next message in this chat session.

        Returns the post-message long-term graph, predictor snapshot, and a
        condensed short-term export (the user prompt and the final assistant
        terminal answer) for the next message's recap header.
        """
        long_term = self.shell.long_term.snapshot()
        predictor = self.shell.predictors.snapshot()
        short_term_export: list[dict[str, Any]] = []
        if run_result is not None:
            objective = ""
            if run_result.trace_context is not None:
                objective = str(run_result.trace_context.objective or "").strip()
            if objective:
                short_term_export.append(
                    {
                        "kind": "user_message",
                        "content": objective,
                        "request_id": run_result.request_id,
                    }
                )
            artifact = run_result.artifact
            if artifact is not None:
                short_term_export.append(
                    {
                        "kind": "assistant_summary",
                        "content": artifact,
                        "request_id": run_result.request_id,
                        "lifecycle_state": run_result.run_lifecycle_state or run_result.lifecycle_state,
                    }
                )
        for node_id, node in self.shell.short_term.nodes.items():
            node_type = str(node.get("type") or "")
            if node_type not in {"Artifact", "VerifierEvidence", "TaskNote"}:
                continue
            short_term_export.append(
                {
                    "kind": "post_message_export",
                    "node_id": node_id,
                    "node_type": node_type,
                    "label": str(node.get("label") or ""),
                    "content": node.get("content"),
                }
            )
        return long_term, predictor, short_term_export

    def _promote_memory_candidate(self, context: PolicyContext, candidate: MemoryNode) -> None:
        score = self.runtime.memory.score_memory_unit(context, candidate, self.shell.long_term.all_nodes())
        if not self.runtime.memory.should_promote(context, candidate, score):
            return
        action, target_id = self.runtime.memory.dedup_candidates(context, candidate, self.shell.long_term.all_nodes())
        provenance = dict(candidate.provenance or {})
        verifier_support_refs = []
        for key in ("verifier_support_refs", "supporting_verifier_ids", "supporting_receipt_ids"):
            raw_refs = provenance.get(key, [])
            if isinstance(raw_refs, str):
                raw_refs = [raw_refs]
            if isinstance(raw_refs, Sequence):
                verifier_support_refs.extend(str(ref) for ref in raw_refs if str(ref).strip())
        with self.shell.long_term.write_scope(
            action=action,
            target_node_id=target_id,
            source_task_id=context.task.task_id,
            source_attempt_id=str(getattr(self.shell, "attempt_id", "") or ""),
            source_checkpoint_ref=context.state.latest_checkpoint_ref,
            verifier_support_refs=verifier_support_refs,
        ):
            self.runtime.memory.upsert_memory(context, candidate, action, target_id)
        context.state.promoted_nodes += 1
        context.record("memory_promoted", node_id=candidate.node_id, node_type=candidate.type, action=action)

    def _ingest_context(self, context: PolicyContext) -> None:
        task = context.task
        for item in task.context_items:
            raw_id = self.shell.short_term.add_node("RawBlob", "context", item)
            context.record("context_ingested", raw_id=raw_id, item=item)
            candidate = None
            if "symbol" in item:
                candidate = MemoryNode(
                    node_id=stable_hash(task.task_id, item["symbol"], item.get("value"))[:16],
                    type="Symbol",
                    label=item["symbol"],
                    content=str(item.get("value")),
                    embedding=[],
                    symbol_set=[item["symbol"]],
                    file_paths=[],
                    source_task_id=task.task_id,
                    verifier_support=1.0,
                    timestamps={"created": now_ts()},
                    provenance={"source": "task_context"},
                    tombstoned=False,
                )
            elif "file_path" in item:
                candidate = MemoryNode(
                    node_id=stable_hash(task.task_id, item["file_path"], item.get("owner"))[:16],
                    type="File",
                    label=item["file_path"],
                    content=str(item.get("owner")),
                    embedding=[],
                    symbol_set=[],
                    file_paths=[item["file_path"]],
                    source_task_id=task.task_id,
                    verifier_support=1.0,
                    timestamps={"created": now_ts()},
                    provenance={"source": "task_context"},
                    tombstoned=False,
                )
            elif "rows" in item:
                candidate = MemoryNode(
                    node_id=stable_hash(task.task_id, stable_hash(item))[:16],
                    type="TaskNote",
                    label="rows",
                    content=json.dumps(item["rows"], sort_keys=True),
                    embedding=[],
                    symbol_set=[],
                    file_paths=[],
                    source_task_id=task.task_id,
                    verifier_support=0.5,
                    timestamps={"created": now_ts()},
                    provenance={"source": "task_context"},
                    tombstoned=False,
                )
            if candidate is not None:
                self._promote_memory_candidate(context, candidate)

    def _compact_if_needed(self, context: PolicyContext) -> None:
        short_term = self.shell.short_term
        total_text = " ".join(str(node["content"]) for node in short_term.nodes.values())
        used_tokens = count_tokens_rough(total_text)
        fraction = used_tokens / max(1.0, float(context.budget.context_window_tokens))
        if fraction <= context.profile.memory.b_hi:
            return
        span_ids = [node_id for node_id, node in short_term.nodes.items() if node["type"] in {"Event", "RawBlob"}]
        if not span_ids:
            return
        selected = self.runtime.memory.select_spans_for_compaction(context, span_ids, fraction)
        for group in selected:
            summary = self.runtime.memory.summarize_span(context, [short_term.nodes[node_id] for node_id in group])
            short_term.summary_replace(group, summary)
            context.record("compaction", node_ids=group, summary=(summary).model_dump())

    def _execute_memory_lookup(self, context: PolicyContext, operation: Any, run_node_id: str | None) -> Any:
        required_symbol = str(operation.static_args.get("requires_exact_symbol", "")).strip()
        exact_symbols = [required_symbol] if required_symbol else context.task.symbolic_seeds
        candidates = self.shell.long_term.retrieve_candidates(
            context.task.prompt,
            exact_symbols,
            context.task.file_paths,
            task_id=context.task.task_id,
            seed=context.seed,
            request_id=context.request_id,
            scope_id=str(getattr(self.shell, "_memory_scope_id", "") or context.task.task_id),
            emit_diagnostic=False,
        )
        ranked = self.runtime.memory.retrieve_long_term(context, context.task.prompt, exact_symbols, context.task.file_paths, candidates)
        self.shell.long_term.record_retrieval_diagnostic(
            context.task.prompt,
            exact_symbols,
            context.task.file_paths,
            ranked,
            task_id=context.task.task_id,
            seed=context.seed,
            request_id=context.request_id,
            scope_id=str(getattr(self.shell, "_memory_scope_id", "") or context.task.task_id),
        )
        if not ranked:
            raise HardInvalidation("memory retrieval returned no candidates for exact symbol/path query")
        node = ranked[0]
        evidence_id = self.shell.short_term.add_node(
            "VerifierEvidence",
            operation.output_key,
            {"retrieved": node.node_id, "label": node.label, "type": node.type},
        )
        if run_node_id and run_node_id in self.shell.short_term.nodes:
            self.shell.short_term.add_edge(run_node_id, evidence_id, "VALIDATED_BY")
        feeds_downstream = any(operation.output_key in candidate.dependencies for candidate in context.plan.nodes)
        return self._coerce(node.content) if feeds_downstream else node.content

    def _record_artifact_signature(self, context: PolicyContext, artifact: Any, verifier_score: float) -> None:
        if verifier_score <= 0.0:
            return
        candidate = MemoryNode(
            node_id=stable_hash(context.task.task_id, stable_hash(artifact))[:16],
            type="ArtifactSignature",
            label=context.task.task_id,
            content=json.dumps(artifact, sort_keys=True, default=str),
            embedding=[],
            symbol_set=list(context.task.symbolic_seeds),
            file_paths=list(context.task.file_paths),
            source_task_id=context.task.task_id,
            verifier_support=verifier_score,
            timestamps={"created": now_ts()},
            provenance={"source": "verifier", "verifier_type": context.task.verifier_type},
            tombstoned=False,
        )
        self._promote_memory_candidate(context, candidate)
