from __future__ import annotations

import time
from typing import Any, Mapping, Sequence
from ..runtime_api import (
    AgentFrame,
    PolicyContext,
    RuntimeBudget,
    RuntimeState,
    compile_execution_plan_from_task,
    get_plan_node_descriptor,
    normalize_benchmark_request_id,
)
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
from ..verifiers import run_checker, verify_task


class VerificationMixin:
    def _resolve_terminal_progress(
        self,
        context: PolicyContext,
        frame: AgentFrame,
        task: BenchmarkTask,
        plan: ExecutionPlan,
        artifact: Any,
        verifier_score: float,
        verified_terminal: bool,
    ) -> tuple[Any | None, float, bool]:
        explicit_verify = self._resolved_verify_status(plan, context.state.artifacts)
        if explicit_verify is not None:
            verifier_score = float(explicit_verify.get("verifier_score", verifier_score) or 0.0)
            verified_terminal = bool(explicit_verify.get("verified", verifier_score >= 1.0))
            return artifact, verifier_score, verified_terminal
        if self._has_pending_explicit_verify(plan, context.state):
            self._queue_root_continuation(context, frame)
            return None, verifier_score, verified_terminal
        if plan.execution_flags.requires_terminal_verification:
            verifier_score = self._maybe_verify(
                context,
                artifact,
                frame.metadata.get("run_node_id"),
                exact_verifier_exists=self._has_exact_verifier(task),
            )
            verified_terminal = verifier_score >= 1.0
        return artifact, verifier_score, verified_terminal

    def _maybe_verify(
        self,
        context: PolicyContext,
        artifact: Any,
        run_node_id: str | None,
        *,
        exact_verifier_exists: bool | None = None,
    ) -> float:
        exact_verifier_exists = self._has_exact_verifier(context.task) if exact_verifier_exists is None else exact_verifier_exists
        checkers = self.runtime.control.request_checks(
            context,
            artifact,
            exact_verifier_exists=exact_verifier_exists,
            irreversible=True,
            external_visible=context.task.externally_visible,
        )
        available_checks = context.budget.remaining_checks()
        if available_checks <= 0:
            context.record("checks_skipped", reason="check_budget_exhausted")
            return 0.0
        if len(checkers) > available_checks:
            context.record("checks_trimmed", requested=checkers, allowed=available_checks)
        checkers = list(checkers[:available_checks])
        context.record("checks_requested", checks=checkers)
        verifier_score = 0.0
        total_latency = 0.0
        executed_checks = 0
        has_benchmark = "benchmark" in checkers
        for checker in checkers:
            start = time.perf_counter()
            evidence = run_checker(context.task, artifact, context.trace, checker)
            total_latency += time.perf_counter() - start
            executed_checks += 1
            evidence_id = self.shell.short_term.add_node("VerifierEvidence", checker, evidence, checker=checker)
            if run_node_id and run_node_id in self.shell.short_term.nodes:
                self.shell.short_term.add_edge(run_node_id, evidence_id, "VALIDATED_BY")
            context.record("check_result", checker=checker, passed=evidence.get("passed", False))
            if checker == "benchmark":
                verifier_score = float(evidence.get("score", 0.0))
                break
            if not evidence.get("passed", False) and not has_benchmark:
                break
        context.state.checks_used += executed_checks
        context.budget.consume_check(executed_checks, total_latency)
        self._record_artifact_signature(context, artifact, verifier_score)
        if getattr(context.active_frame, "worker_id", None):
            self._emit_branch_publication(
                context,
                publication_kind="verifier_evidence",
                logical_key=f"{context.active_frame.worker_id}.verifier.completed",
                payload={
                    "event": "branch_verifier_completed",
                    "checks": checkers,
                    "verifier_score": verifier_score,
                },
                verifier_support=verifier_score,
            )
            context.publish_checkpoint_boundary("after_branch_verifier_completion")
            context.raise_if_cancelled()
        return verifier_score

    def _has_exact_verifier(self, task: BenchmarkTask) -> bool:
        return str(task.verifier_type).strip().lower() not in {"", "none", "best_effort"}

    def _best_next_action_utility(self, context: PolicyContext, unresolved: Sequence[str], verified_terminal: bool) -> float:
        if not unresolved and verified_terminal:
            return -0.1
        remaining_budget = 1.0 - max(context.budget.normalized().values())
        if remaining_budget <= 0:
            return -1.0
        candidates = []
        for output_key in unresolved:
            operation = next((op for op in context.task.operations if op.output_key == output_key), None)
            if operation is None:
                continue
            solve = 0.55
            cost = 0.06
            latency = 0.05
            fault = 0.04
            if operation.kind == "memory_lookup":
                solve += 0.10
                cost += 0.04
                latency += 0.03
            elif operation.kind == "generated_expression":
                solve += 0.16
                cost += 0.18
                latency += 0.12
                fault += 0.08
            elif operation.kind == "repo_patch":
                solve += 0.08
                cost += 0.18
                latency += 0.14
                fault += 0.09
            elif operation.kind == "service_action":
                solve += 0.05
                cost += 0.06
                latency += 0.12
                fault += 0.10
            elif operation.kind == "builtin":
                solve += 0.12
                cost += 0.02
                latency += 0.02
            solve += 0.03 * min(3, len(operation.dependencies))
            candidates.append(solve - 0.25 * cost - 0.18 * latency - 0.15 * fault + 0.10 * remaining_budget)
        if verified_terminal:
            candidates.append(-0.05)
        return max(candidates or [-0.5])

    def _worker_support(self, task: BenchmarkTask, artifact: Any) -> float:
        return verify_task(task, artifact, [])
