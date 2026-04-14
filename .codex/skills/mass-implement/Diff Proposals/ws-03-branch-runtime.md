# Worker 03 Diff Proposal: Branch / Runtime Execution Correctness

## Scope

This proposal stays inside the Worker 03 slice from `WS2 Resume Run-Root Reconstruction Plan.md`:

- branch `PolicyContext` persistence hooks
- branch publications / receipts at the required boundaries
- fatal sibling fault cancellation ordering
- cancellation fences at irreversible boundaries and node boundaries
- frontier-only horizontal scheduling
- parent-state dependency gating for downstream nodes
- compiler metadata vs runtime args separation
- batch transfer ordering in shared-runtime episode execution
- focused runtime execution tests

I intentionally did **not** propose run-root / run-manifest transport changes beyond the hooks this slice needs; those remain Worker 01 ownership. I also did **not** propose snapshot section reshaping; that remains Worker 02 ownership.

## 2026 Best-Practice Note

- The current Python 3.14 `concurrent.futures` docs explicitly state that `shutdown(cancel_futures=True)` only cancels futures that have **not started**; running futures are not cancelled and must stop cooperatively. That means the runtime must set the shared cancellation signal **before** draining sibling futures, and running branches must check that signal at every irreversible boundary and immediately after node completion.
- For this codebase, the practical consequence is: cancellation must be cooperative and publication-gated. Once the branch group is cancelled, normal candidate publications must stop; only cleanup / reconciliation records may still be published while the group drains.

## Assumptions

- `PlanNode.expression` remains the authoritative runtime-visible generated-expression field. It is already present in the schema and is enough for runtime execution and memory promotion.
- Branch-publication persistence can use the current parent checkpoint machinery by mirroring branch-local publications / receipts into parent state under a lock before each checkpoint boundary is published.
- Result ordering for `RuntimeBatchResponse.run_results` should remain transport-stable even if execution order inside a transfer-scored episode is sorted. The proposal below preserves response ordering while fixing execution ordering.

---

## File: `agintor/runtime_api.py`

### 1. Stop leaking compiler metadata into runtime args

This is the smallest change that fully fixes the `OperationSpec.expression -> PlanNode.static_args -> _resolve_plan_node_args()` leak. It keeps the expression on the dedicated `PlanNode.expression` field and leaves `static_args` as runtime-argument material only.

```text
<<<<<<< SEARCH
        static_args = dict(operation.args)
        if operation.requires_exact_symbol:
            static_args["requires_exact_symbol"] = operation.requires_exact_symbol
        if operation.expression:
            static_args["expression"] = operation.expression
=======
        static_args = dict(operation.args)
        if operation.requires_exact_symbol:
            static_args["requires_exact_symbol"] = operation.requires_exact_symbol
>>>>>>> REPLACE
```

### 2. Reuse `reconciled` provider receipts exactly like `completed` receipts, and re-check cancellation immediately before the irreversible provider call

```text
<<<<<<< SEARCH
        for receipt_payload in self.state.side_effect_receipts:
            if str(receipt_payload.get("idempotency_key", "")) != request_digest:
                continue
            if str(receipt_payload.get("status", "")) != "completed":
                continue
            result_ref = receipt_payload.get("result_ref") or {}
=======
        for receipt_payload in self.state.side_effect_receipts:
            if str(receipt_payload.get("idempotency_key", "")) != request_digest:
                continue
            if str(receipt_payload.get("action_kind", "")) != "provider_completion":
                continue
            if str(receipt_payload.get("status", "")) not in {"completed", "reconciled"}:
                continue
            result_ref = receipt_payload.get("result_ref") or {}
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
        self.record_side_effect(launch_receipt)
        self.publish_checkpoint_boundary("after_provider_launch")
        response = self.provider.generate(
=======
        self.record_side_effect(launch_receipt)
        self.publish_checkpoint_boundary("after_provider_launch")
        self.raise_if_cancelled()
        response = self.provider.generate(
>>>>>>> REPLACE
```

Rationale:

- `reconciled` is terminal and reusable under the WS2 resume contract; it must not trigger a fresh provider call.
- The extra `raise_if_cancelled()` is required because `provider.generate(...)` is the irreversible boundary, and `ThreadPoolExecutor` cancellation is cooperative for already-running futures.

---

## File: `agintor/runner.py`

### 3. Make `_resolve_plan_node_args()` build runtime args from bindings only

This pairs with the `runtime_api.py` change above and stops `static_args` from silently acting as a second metadata channel.

```text
<<<<<<< SEARCH
    def _resolve_plan_node_args(self, context: PolicyContext, node: PlanNode) -> dict[str, Any]:
        resolved = dict(node.static_args)
        for binding in node.input_bindings:
            if binding.source_kind == "plan_constant":
                if binding.source_ref in context.plan.plan_constants:
                    resolved[binding.target_arg] = context.plan.plan_constants[binding.source_ref]
                elif binding.source_ref in node.static_args:
                    resolved[binding.target_arg] = node.static_args[binding.source_ref]
                elif binding.required:
                    raise HardInvalidation(
                        f"plan node {node.node_id} requires plan constant {binding.source_ref!r}"
                    )
=======
    def _resolve_plan_node_args(self, context: PolicyContext, node: PlanNode) -> dict[str, Any]:
        resolved: dict[str, Any] = {}
        for binding in node.input_bindings:
            if binding.source_kind == "plan_constant":
                if binding.source_ref in context.plan.plan_constants:
                    resolved[binding.target_arg] = context.plan.plan_constants[binding.source_ref]
                elif binding.required:
                    raise HardInvalidation(
                        f"plan node {node.node_id} requires plan constant {binding.source_ref!r}"
                    )
>>>>>>> REPLACE
```

### 4. Read generated-expression source from the dedicated field, not from `static_args`

```text
<<<<<<< SEARCH
        expression = operation.static_args.get("expression") or operation.metadata.get("expression")
=======
        expression = getattr(operation, "expression", None) or operation.metadata.get("expression")
>>>>>>> REPLACE
```

### 5. Reuse `reconciled` tool receipts exactly like `completed` ones

```text
<<<<<<< SEARCH
        for receipt_payload in context.state.side_effect_receipts:
            if str(receipt_payload.get("idempotency_key", "")) != side_effect_key:
                continue
            if str(receipt_payload.get("status", "")) != "completed":
                continue
            result_ref = receipt_payload.get("result_ref") or {}
=======
        for receipt_payload in context.state.side_effect_receipts:
            if str(receipt_payload.get("idempotency_key", "")) != side_effect_key:
                continue
            if str(receipt_payload.get("action_kind", "")) != "tool_completion":
                continue
            if str(receipt_payload.get("status", "")) not in {"completed", "reconciled"}:
                continue
            result_ref = receipt_payload.get("result_ref") or {}
>>>>>>> REPLACE
```

### 6. Treat `reconciled` receipts as terminal during checkpoint restore too

```text
<<<<<<< SEARCH
        completed_by_key = {
            receipt.idempotency_key: receipt
            for receipt in receipts
            if receipt.status == "completed"
        }
        for receipt in receipts:
            if receipt.status == "completed":
                resolved.append(receipt)
=======
        terminal_by_key = {
            receipt.idempotency_key: receipt
            for receipt in receipts
            if receipt.status in {"completed", "reconciled"}
        }
        for receipt in receipts:
            if receipt.status in {"completed", "reconciled"}:
                resolved.append(receipt)
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
            completed_receipt = completed_by_key.get(receipt.idempotency_key)
            if completed_receipt is not None:
                resolved.append(completed_receipt)
=======
            terminal_receipt = terminal_by_key.get(receipt.idempotency_key)
            if terminal_receipt is not None:
                resolved.append(terminal_receipt)
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
                    "side_effect_reconciled",
                    side_effect_id=completed_receipt.side_effect_id,
                    action_kind=completed_receipt.action_kind,
                    reconciliation_status="reused_completed_receipt",
=======
                    "side_effect_reconciled",
                    side_effect_id=terminal_receipt.side_effect_id,
                    action_kind=terminal_receipt.action_kind,
                    reconciliation_status="reused_terminal_receipt",
>>>>>>> REPLACE
```

### 7. Track the active frame inside `_execute_isolated_frame()` so branch receipts/publications carry the correct frame identity

This fixes a concrete branch bug: provider completions issued inside `_run_branch_plan()` currently record `frame_id=""` because `branch_context.active_frame` is never set in the isolated-frame path.

```text
<<<<<<< SEARCH
        self.shell.short_term = isolated_short_term
        try:
            frame.metadata["run_node_id"] = self._start_agent_run(isolated_short_term, frame, 0, frame.checkpoint)
=======
        previous_active_frame = context.active_frame
        self.shell.short_term = isolated_short_term
        context.active_frame = frame
        try:
            frame.metadata["run_node_id"] = self._start_agent_run(isolated_short_term, frame, 0, frame.checkpoint)
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
        finally:
            self.shell.short_term = parent_short_term
=======
        finally:
            context.active_frame = previous_active_frame
            self.shell.short_term = parent_short_term
>>>>>>> REPLACE
```

### 8. Add branch-publication helpers plus runnable-frontier helpers

Insert these helpers just below `_ordered_execution_nodes()`. They keep the rest of the branch rewrite readable and deterministic.

```text
<<<<<<< SEARCH
    def _candidate_artifact_publication(
=======
    def _emit_branch_publication(
        self,
        context: PolicyContext,
        *,
        publication_kind: str,
        logical_key: str,
        payload: Mapping[str, Any],
        verifier_support: float = 0.0,
        unresolved_critical: int = 0,
        allow_when_cancelled: bool = False,
    ) -> BranchPublication | None:
        branch_id = (
            getattr(context.active_frame, "worker_id", None)
            or getattr(context.trace_context, "worker_id", None)
        )
        if not branch_id:
            return None
        if (
            context.cancellation_event is not None
            and getattr(context.cancellation_event, "is_set", lambda: False)()
            and not allow_when_cancelled
        ):
            return None
        branch_rank = 0
        if context.active_frame is not None:
            branch_rank = int(context.active_frame.metadata.get("merge_priority", 0) or 0)
        publication = BranchPublication(
            publication_id=f"publication.{stable_hash(branch_id, logical_key, len(context.state.branch_publications))[:12]}",
            publication_kind=publication_kind,
            logical_key=logical_key,
            sequence_no=len(context.state.branch_publications),
            accepted=True,
            branch_id=branch_id,
            trace_context=context.trace_context,
            verifier_support=verifier_support,
            unresolved_critical=unresolved_critical,
            branch_rank=branch_rank,
            payload=dict(payload),
        )
        existing_ids = {str(row.get("publication_id", "")) for row in context.state.branch_publications}
        if publication.publication_id in existing_ids:
            return publication
        context.state.branch_publications.append(model_dump(publication))
        return publication

    def _active_runnable_frontier(
        self,
        context: PolicyContext,
        plan: ExecutionPlan,
        *,
        branch_group_id: str | None = None,
    ) -> list[PlanNode]:
        ordered_nodes = self._ordered_execution_nodes(plan)
        runnable = [
            node
            for node in ordered_nodes
            if context.state.plan_node_status.get(node.node_id) != "completed"
            and all(context.state.plan_node_status.get(dep_id) == "completed" for dep_id in node.dependencies)
        ]
        if not runnable:
            return []
        active_group_id = branch_group_id or next((node.branch_group_id for node in runnable if node.branch_group_id), None)
        if active_group_id is None:
            return runnable[:1]
        return [node for node in runnable if node.branch_group_id == active_group_id]

    def _apply_horizontal_frontier_outputs(
        self,
        context: PolicyContext,
        plan: ExecutionPlan,
        frontier_nodes: Sequence[PlanNode],
        artifact: Any,
    ) -> None:
        if len(frontier_nodes) == 1 and not isinstance(artifact, Mapping):
            artifact_payload = {frontier_nodes[0].output_key: artifact}
        elif isinstance(artifact, Mapping):
            artifact_payload = dict(artifact)
        else:
            raise HardInvalidation("horizontal merge must return a mapping for multi-node frontier output")
        for node in frontier_nodes:
            if node.output_key not in artifact_payload:
                raise HardInvalidation(
                    f"horizontal merge did not return frontier output {node.output_key!r} for node {node.node_id}"
                )
            context.state.artifacts[node.output_key] = artifact_payload[node.output_key]
            context.state.plan_node_status[node.node_id] = "completed"

    def _queue_root_continuation(self, context: PolicyContext, frame: AgentFrame) -> None:
        context.state.queue.insert(
            0,
            AgentFrame(
                frame_id=stable_hash(context.request_id, "root-continuation", len(context.state.queue))[:16],
                agent=self.shell.agent_pool.clone("root"),
                request_id=context.request_id,
                plan_id=context.plan.plan_id,
                trace_context=context.trace_context,
                objective=context.plan.objective,
                operation_ids=[
                    node.node_id
                    for node in self._execution_nodes(context.plan)
                    if context.state.plan_node_status.get(node.node_id) != "completed"
                ],
                depth=frame.depth,
                role="root",
                tool_scope=list(context.state.visible_tool_names),
                model_class=frame.model_class,
                branch_group_id=frame.branch_group_id,
                metadata={"continued_from_frame_id": frame.frame_id},
            ),
        )

    def _candidate_artifact_publication(
>>>>>>> REPLACE
```

### 9. Make horizontal execution frontier-only and continue parent execution after a frontier merge instead of treating the frontier merge as final by default

Replace the current horizontal half of `_run_root_frame()` with the following logic:

```text
<<<<<<< SEARCH
        restored_worker_outputs = self._restored_branch_frontier(context, frame)
        if restored_worker_outputs is None:
            context.state.execution_state = "branching"
            workers = self.runtime.topology.select_workers(context, frame, execution_nodes)
            worker_outputs, local_faults = self._execute_horizontal_branches(context, frame, task, plan, workers)
            faults += local_faults
        else:
            worker_outputs = restored_worker_outputs
            context.record(
                "branch_frontier_restored",
                parent_frame_id=frame.frame_id,
                branch_count=len(restored_worker_outputs),
            )
        context.state.execution_state = "merging"
        context.state.queue.append(
            AgentFrame(
                frame_id=stable_hash(plan.request_id, "merge_horizontal", len(context.state.queue))[:16],
                agent=self.shell.agent_pool.clone("root"),
                request_id=plan.request_id,
                plan_id=plan.plan_id,
                trace_context=context.trace_context,
                objective="merge",
                operation_ids=[],
                depth=frame.depth,
                role="merge_horizontal",
                metadata={"worker_outputs": worker_outputs, "parent_run_node_id": frame.metadata.get("run_node_id")},
            )
        )
        context.state.execution_state = "running"
        return artifact, faults, verifier_score, verified_terminal
=======
        frontier_nodes = self._active_runnable_frontier(context, plan, branch_group_id=frame.branch_group_id)
        if len(frontier_nodes) < 2:
            artifact, local_faults = self._execute_operations(context, frame, frontier_nodes or self._ordered_execution_nodes(plan))
            faults += local_faults
            verifier_score = self._maybe_verify(
                context,
                artifact,
                frame.metadata.get("run_node_id"),
                exact_verifier_exists=self._has_exact_verifier(task),
            )
            verified_terminal = verifier_score >= 1.0
            return artifact, faults, verifier_score, verified_terminal
        restored_worker_outputs = self._restored_branch_frontier(context, frame)
        if restored_worker_outputs is None:
            context.state.execution_state = "branching"
            workers = self.runtime.topology.select_workers(context, frame, frontier_nodes)
            worker_outputs, local_faults = self._execute_horizontal_branches(context, frame, task, plan, workers)
            faults += local_faults
        else:
            worker_outputs = restored_worker_outputs
            context.record(
                "branch_frontier_restored",
                parent_frame_id=frame.frame_id,
                branch_count=len(restored_worker_outputs),
            )
        context.state.execution_state = "merging"
        context.state.queue.append(
            AgentFrame(
                frame_id=stable_hash(plan.request_id, "merge_horizontal", len(context.state.queue))[:16],
                agent=self.shell.agent_pool.clone("root"),
                request_id=plan.request_id,
                plan_id=plan.plan_id,
                trace_context=context.trace_context,
                objective="merge",
                operation_ids=[],
                depth=frame.depth,
                role="merge_horizontal",
                metadata={
                    "worker_outputs": worker_outputs,
                    "frontier_node_ids": [node.node_id for node in frontier_nodes],
                    "parent_run_node_id": frame.metadata.get("run_node_id"),
                },
            )
        )
        context.state.execution_state = "running"
        return artifact, faults, verifier_score, verified_terminal
>>>>>>> REPLACE
```

Then update the `merge_horizontal` case in `_run_execution_plan()` so it materializes frontier outputs into parent plan state and queues the next parent pass when the plan is not terminal yet:

```text
<<<<<<< SEARCH
                    elif frame.role == "merge_horizontal":
                        worker_outputs = frame.metadata.get("worker_outputs", [])
                        artifact = self.runtime.topology.merge_ensemble(context, worker_outputs)
                        verifier_score = self._maybe_verify(
                            context,
                            artifact,
                            frame.metadata.get("run_node_id"),
                            exact_verifier_exists=self._has_exact_verifier(task),
                        )
                        verified_terminal = verifier_score >= 1.0
                        self._record_artifact_node(self.shell.short_term, "ensemble", artifact, frame.metadata.get("run_node_id"))
                        context.record("merge_completed", artifact=artifact, merge_kind="horizontal")
=======
                    elif frame.role == "merge_horizontal":
                        worker_outputs = frame.metadata.get("worker_outputs", [])
                        frontier_node_ids = list(frame.metadata.get("frontier_node_ids", []))
                        frontier_nodes = [self._plan_node_by_id(plan, node_id) for node_id in frontier_node_ids]
                        merged_frontier_artifact = self.runtime.topology.merge_ensemble(context, worker_outputs)
                        self._apply_horizontal_frontier_outputs(context, plan, frontier_nodes, merged_frontier_artifact)
                        self._record_artifact_node(
                            self.shell.short_term,
                            "ensemble_frontier",
                            merged_frontier_artifact,
                            frame.metadata.get("run_node_id"),
                        )
                        if self._all_outputs_present(plan, state.artifacts):
                            artifact = {output_key: state.artifacts.get(output_key) for output_key in plan.terminal_output_keys}
                            verifier_score = self._maybe_verify(
                                context,
                                artifact,
                                frame.metadata.get("run_node_id"),
                                exact_verifier_exists=self._has_exact_verifier(task),
                            )
                            verified_terminal = verifier_score >= 1.0
                        else:
                            artifact = None
                            self._queue_root_continuation(context, frame)
                        context.record(
                            "merge_completed",
                            artifact=merged_frontier_artifact,
                            merge_kind="horizontal",
                            frontier_node_ids=frontier_node_ids,
                            terminal_ready=self._all_outputs_present(plan, state.artifacts),
                        )
>>>>>>> REPLACE
```

This is the key correctness fix for:

- frontier-only worker selection
- no dependent nodes before parent dependencies complete
- horizontal mode that can advance beyond the first branch group instead of implicitly assuming the branch frontier is also the terminal artifact

### 10. Add post-operation cancellation fences and branch checkpoint publication at node/verifier boundaries

Inside `_execute_operations()`:

```text
<<<<<<< SEARCH
            results[operation.output_key] = output
            context.state.artifacts[operation.output_key] = output
            self._record_artifact_node(self.shell.short_term, operation.output_key, output, run_node_id if isinstance(run_node_id, str) else None)
            context.state.plan_node_status[operation.node_id] = "completed"
            context.record("node_completed", node_id=operation.node_id, output_key=operation.output_key)
            context.state.unresolved_goals = [key for key in context.plan.terminal_output_keys if key not in context.state.artifacts]
=======
            context.raise_if_cancelled()
            results[operation.output_key] = output
            context.state.artifacts[operation.output_key] = output
            self._record_artifact_node(self.shell.short_term, operation.output_key, output, run_node_id if isinstance(run_node_id, str) else None)
            context.state.plan_node_status[operation.node_id] = "completed"
            context.record("node_completed", node_id=operation.node_id, output_key=operation.output_key)
            if frame.worker_id:
                self._emit_branch_publication(
                    context,
                    publication_kind="trace_rows",
                    logical_key=f"{frame.worker_id}.node.{operation.node_id}.completed",
                    payload={
                        "event": "branch_node_completed",
                        "node_id": operation.node_id,
                        "output_key": operation.output_key,
                    },
                )
                context.publish_checkpoint_boundary("after_branch_node_completion")
            context.state.unresolved_goals = [key for key in context.plan.terminal_output_keys if key not in context.state.artifacts]
            context.raise_if_cancelled()
>>>>>>> REPLACE
```

At the end of `_maybe_verify()`:

```text
<<<<<<< SEARCH
        context.state.checks_used += executed_checks
        context.budget.consume_check(executed_checks, total_latency)
        self._record_artifact_signature(context, artifact, verifier_score)
        return verifier_score
=======
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
                    "checks": list(checkers),
                    "executed_checks": executed_checks,
                    "verifier_score": verifier_score,
                },
                verifier_support=verifier_score,
            )
            context.publish_checkpoint_boundary("after_branch_verifier_completion")
            context.raise_if_cancelled()
        return verifier_score
>>>>>>> REPLACE
```

### 11. Replace the current branch-group executor flow so cancellation is set first, remaining futures are drained cooperatively, and only cleanup/reconciliation publications survive cancellation

This is the most important concurrency correction in the file. The existing code raises immediately after the first fatal branch result, which drops cleanup / reconciliation output from still-running siblings and contradicts the cooperative-cancellation model that `ThreadPoolExecutor` actually implements.

Change the import and `_execute_horizontal_branches()`:

```text
<<<<<<< SEARCH
from threading import Event
=======
from threading import Event, Lock
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
        cancellation_event = Event()
        branch_results: list[BranchResult] = []
        faults = 0
        context.state.active_branch_count = len(branch_plans)
        executor = ThreadPoolExecutor(max_workers=len(branch_plans), thread_name_prefix=f"branch-{plan.plan_id[:8]}")
        try:
            future_map = {
                executor.submit(self._run_branch_plan, context, task, plan, branch_plan, cancellation_event): branch_plan
                for branch_plan in branch_plans
            }
            done, pending = wait(future_map, return_when=FIRST_EXCEPTION)
            fatal_error: Exception | None = None
            for future in done:
                branch_plan = future_map[future]
                try:
                    branch_results.append(future.result())
                except Exception as exc:
                    fatal_error = exc
                    context.state.branch_states[branch_plan.branch_id] = model_dump(
                        BranchState(
                            branch_id=branch_plan.branch_id,
                            status="failed",
                            parent_frame_id=frame.frame_id,
                            assigned_node_ids=list(branch_plan.assigned_node_ids),
                            merge_priority=branch_plan.merge_priority,
                            predicted_solve=branch_plan.predicted_solve,
                            reserved_budget=branch_plan.reserved_budget,
                            error=str(exc),
                            cancellation_record=CancellationRecord(
                                reason="fatal_branch_fault",
                                details={"error": str(exc)},
                                created_at=now_ts(),
                            ),
                        )
                    )
                    faults += 1
            if fatal_error is not None:
                cancellation_event.set()
                for future in pending:
                    future.cancel()
                executor.shutdown(wait=True, cancel_futures=True)
                raise HardInvalidation(f"branch execution failed: {fatal_error}")
            for future in pending:
                branch_results.append(future.result())
        finally:
            context.state.active_branch_count = 0
            executor.shutdown(wait=True, cancel_futures=True)
=======
        cancellation_event = Event()
        persist_lock = Lock()
        branch_results: list[BranchResult] = []
        faults = 0
        context.state.active_branch_count = len(branch_plans)
        executor = ThreadPoolExecutor(max_workers=len(branch_plans), thread_name_prefix=f"branch-{plan.plan_id[:8]}")
        try:
            future_map = {
                executor.submit(self._run_branch_plan, context, task, plan, branch_plan, cancellation_event, persist_lock): branch_plan
                for branch_plan in branch_plans
            }
            done, pending = wait(future_map, return_when=FIRST_EXCEPTION)
            fatal_error: Exception | None = None
            for future in done:
                branch_plan = future_map[future]
                try:
                    branch_results.append(future.result())
                except Exception as exc:
                    fatal_error = exc
                    context.state.branch_states[branch_plan.branch_id] = model_dump(
                        BranchState(
                            branch_id=branch_plan.branch_id,
                            status="failed",
                            parent_frame_id=frame.frame_id,
                            assigned_node_ids=list(branch_plan.assigned_node_ids),
                            merge_priority=branch_plan.merge_priority,
                            predicted_solve=branch_plan.predicted_solve,
                            reserved_budget=branch_plan.reserved_budget,
                            error=str(exc),
                            cancellation_record=CancellationRecord(
                                reason="fatal_branch_fault",
                                details={"error": str(exc)},
                                created_at=now_ts(),
                            ),
                        )
                    )
                    faults += 1
            if fatal_error is not None:
                cancellation_event.set()
                for future in pending:
                    branch_plan = future_map[future]
                    if future.cancel():
                        branch_results.append(
                            self._cancelled_branch_result(
                                branch_plan,
                                None,
                                len(branch_plan.assigned_node_ids),
                                reason="fatal_branch_fault",
                                details={"error": str(fatal_error)},
                            )
                        )
                for future in pending:
                    if future.cancelled():
                        continue
                    branch_plan = future_map[future]
                    try:
                        branch_results.append(future.result())
                    except Exception as exc:
                        faults += 1
                        context.state.branch_states[branch_plan.branch_id] = model_dump(
                            BranchState(
                                branch_id=branch_plan.branch_id,
                                status="failed",
                                parent_frame_id=frame.frame_id,
                                assigned_node_ids=list(branch_plan.assigned_node_ids),
                                merge_priority=branch_plan.merge_priority,
                                predicted_solve=branch_plan.predicted_solve,
                                reserved_budget=branch_plan.reserved_budget,
                                error=str(exc),
                                cancellation_record=CancellationRecord(
                                    reason="fatal_branch_fault",
                                    details={"error": str(exc)},
                                    created_at=now_ts(),
                                ),
                            )
                        )
                raise HardInvalidation(f"branch execution failed: {fatal_error}")
            for future in pending:
                branch_results.append(future.result())
        finally:
            context.state.active_branch_count = 0
            executor.shutdown(wait=True, cancel_futures=True)
>>>>>>> REPLACE
```

### 12. Rewrite `_run_branch_plan()` so branch contexts inherit parent persistence hooks, publish progress immediately, and suppress normal publications after cancellation

Replace the entire function with the following version. This is large, but it is the cleanest way to keep the branch-local logic internally consistent:

```text
<<<<<<< SEARCH
    def _run_branch_plan(
        self,
        parent_context: PolicyContext,
        task: BenchmarkTask,
        plan: ExecutionPlan,
        branch_plan: BranchPlan,
        cancellation_event: Event,
    ) -> BranchResult:
        branch_shell = parent_context.shell.fork_branch(branch_plan.branch_id)
        branch_provider = clone_provider(self.provider, provider_profile=self.runtime_profile.runtime_provider)
        branch_runtime = TaskRuntime(
            self.runtime,
            branch_shell,
            branch_provider,
            budget_overrides={},
            runtime_profile=self.runtime_profile,
        )
        branch_budget = RuntimeBudget(
            C_max=parent_context.budget.C_max,
            L_max=branch_plan.reserved_budget.latency_max,
            M_max=branch_plan.reserved_budget.model_calls_max,
            Q_max=branch_plan.reserved_budget.checks_max,
            context_window_tokens=parent_context.budget.context_window_tokens,
        )
        branch_state = RuntimeState(
            request_id=plan.request_id,
            plan_id=plan.plan_id,
            execution_state="branching",
            visible_tool_names=list(parent_context.state.visible_tool_names),
        )
        branch_trace: list[dict[str, Any]] = []
        branch_context = PolicyContext(
            runtime_dir=self.runtime.runtime_dir,
            shell=branch_shell,
            task=task,
            request_id=plan.request_id,
            plan=plan,
            trace_context=branch_plan.trace_context or parent_context.trace_context,
            provider=branch_provider,
            profile=self.runtime_profile,
            seed=parent_context.seed,
            state=branch_state,
            budget=branch_budget,
            trace=branch_trace,
            objective=plan.objective,
            runtime_backend=parent_context.runtime_backend,
            cancellation_event=cancellation_event,
        )
        publications: list[BranchPublication] = [
            BranchPublication(
                publication_id=f"publication.{stable_hash(branch_plan.branch_id, 'start')[:12]}",
                publication_kind="trace_rows",
                logical_key=f"{branch_plan.branch_id}.start",
                sequence_no=0,
                accepted=True,
                branch_id=branch_plan.branch_id,
                trace_context=branch_context.trace_context,
                branch_rank=branch_plan.merge_priority,
                payload={"event": "branch_started"},
            )
        ]
        if cancellation_event.is_set():
            return self._cancelled_branch_result(branch_plan, publications, len(branch_plan.assigned_node_ids))
        frame = AgentFrame(
            frame_id=stable_hash(plan.request_id, branch_plan.branch_id, "frame")[:16],
            agent=branch_shell.agent_pool.clone("root"),
            request_id=plan.request_id,
            plan_id=plan.plan_id,
            trace_context=branch_plan.trace_context,
            objective=plan.objective,
            operation_ids=list(branch_plan.assigned_node_ids),
            depth=1,
            role="worker",
            worker_id=branch_plan.branch_id,
            tool_scope=list(parent_context.state.visible_tool_names),
            model_class="small",
            branch_group_id="root-frontier",
            metadata={"parent_run_node_id": parent_context.trace_context.run_node_id if parent_context.trace_context else None},
        )
        operations = [self._plan_node_by_id(plan, node_id) for node_id in branch_plan.assigned_node_ids]
        try:
            output, _, checkpoint = branch_runtime._execute_isolated_frame(
                branch_context,
                frame,
                operations,
                isolate_runtime_state=False,
            )
            branch_context.raise_if_cancelled()
        except BranchCancelled:
            return self._cancelled_branch_result(branch_plan, publications, len(branch_plan.assigned_node_ids))
        if (
            branch_budget.calls > branch_plan.reserved_budget.model_calls_max
            or branch_budget.checks > branch_plan.reserved_budget.checks_max
            or branch_budget.latency > branch_plan.reserved_budget.latency_max + 1e-9
        ):
            raise HardInvalidation(f"branch {branch_plan.branch_id} exceeded reserved budget")
        verifier_support = self._worker_support(task, output)
        unresolved_critical = 0 if output else len(branch_plan.assigned_node_ids)
        publications.append(
            BranchPublication(
                publication_id=f"publication.{stable_hash(branch_plan.branch_id, 'artifact')[:12]}",
                publication_kind="candidate_artifact",
                logical_key=f"{branch_plan.branch_id}.artifact",
                sequence_no=1,
                accepted=True,
                branch_id=branch_plan.branch_id,
                trace_context=branch_context.trace_context,
                verifier_support=verifier_support,
                unresolved_critical=unresolved_critical,
                branch_rank=branch_plan.merge_priority,
                payload={
                    "artifact": output,
                    "summary": model_dump(checkpoint.summary),
                    "predicted_solve": branch_plan.predicted_solve,
                },
            )
        )
        publications.append(
            BranchPublication(
                publication_id=f"publication.{stable_hash(branch_plan.branch_id, 'terminal')[:12]}",
                publication_kind="trace_rows",
                logical_key=f"{branch_plan.branch_id}.terminal",
                sequence_no=2,
                accepted=True,
                branch_id=branch_plan.branch_id,
                trace_context=branch_context.trace_context,
                branch_rank=branch_plan.merge_priority,
                payload={"event": "branch_completed"},
            )
        )
        return BranchResult(
            branch_plan=branch_plan,
            branch_state=BranchState(
                branch_id=branch_plan.branch_id,
                status="completed",
                parent_frame_id=branch_plan.parent_frame_id,
                assigned_node_ids=list(branch_plan.assigned_node_ids),
                merge_priority=branch_plan.merge_priority,
                predicted_solve=branch_plan.predicted_solve,
                reserved_budget=branch_plan.reserved_budget,
                publications=publications,
                budget_consumed={
                    "cost": branch_budget.cost,
                    "latency": branch_budget.latency,
                    "model_calls": branch_budget.calls,
                    "checks": branch_budget.checks,
                    "tokens": branch_budget.tokens,
                    "input_tokens": branch_budget.input_tokens,
                    "output_tokens": branch_budget.output_tokens,
                    "created_tools": branch_context.state.created_tools,
                    "promoted_nodes": branch_context.state.promoted_nodes,
                },
                verifier_support=verifier_support,
                unresolved_critical=unresolved_critical,
            ),
            artifact=output,
            verifier_support=verifier_support,
            unresolved_critical=unresolved_critical,
            side_effect_receipts=[
                model_validate(SideEffectReceipt, payload)
                for payload in branch_context.state.side_effect_receipts
            ],
        )
=======
    def _run_branch_plan(
        self,
        parent_context: PolicyContext,
        task: BenchmarkTask,
        plan: ExecutionPlan,
        branch_plan: BranchPlan,
        cancellation_event: Event,
        persist_lock: Lock,
    ) -> BranchResult:
        branch_shell = parent_context.shell.fork_branch(branch_plan.branch_id)
        branch_provider = clone_provider(self.provider, provider_profile=self.runtime_profile.runtime_provider)
        branch_runtime = TaskRuntime(
            self.runtime,
            branch_shell,
            branch_provider,
            budget_overrides={},
            runtime_profile=self.runtime_profile,
        )
        branch_budget = RuntimeBudget(
            C_max=parent_context.budget.C_max,
            L_max=branch_plan.reserved_budget.latency_max,
            M_max=branch_plan.reserved_budget.model_calls_max,
            Q_max=branch_plan.reserved_budget.checks_max,
            context_window_tokens=parent_context.budget.context_window_tokens,
        )
        branch_state = RuntimeState(
            request_id=plan.request_id,
            plan_id=plan.plan_id,
            execution_state="branching",
            visible_tool_names=list(parent_context.state.visible_tool_names),
        )
        branch_trace: list[dict[str, Any]] = []
        branch_context = PolicyContext(
            runtime_dir=self.runtime.runtime_dir,
            shell=branch_shell,
            task=task,
            request_id=plan.request_id,
            plan=plan,
            trace_context=branch_plan.trace_context or parent_context.trace_context,
            provider=branch_provider,
            profile=self.runtime_profile,
            seed=parent_context.seed,
            state=branch_state,
            budget=branch_budget,
            trace=branch_trace,
            objective=plan.objective,
            runtime_backend=parent_context.runtime_backend,
            cancellation_event=cancellation_event,
        )

        def _branch_budget_consumed() -> dict[str, Any]:
            return {
                "cost": branch_budget.cost,
                "latency": branch_budget.latency,
                "model_calls": branch_budget.calls,
                "checks": branch_budget.checks,
                "tokens": branch_budget.tokens,
                "input_tokens": branch_budget.input_tokens,
                "output_tokens": branch_budget.output_tokens,
                "created_tools": branch_context.state.created_tools,
                "promoted_nodes": branch_context.state.promoted_nodes,
            }

        def _persist_branch_progress(
            boundary: str,
            *,
            status: str = "running",
            verifier_support: float = 0.0,
            unresolved_critical: int | None = None,
            error: str | None = None,
            cancellation_record: CancellationRecord | None = None,
        ) -> None:
            publications = [
                model_validate(BranchPublication, payload)
                for payload in branch_context.state.branch_publications
            ]
            if unresolved_critical is None:
                unresolved_critical = max(
                    0,
                    len(branch_plan.assigned_node_ids)
                    - sum(1 for node_id in branch_plan.assigned_node_ids if branch_context.state.plan_node_status.get(node_id) == "completed")
                )
            with persist_lock:
                existing_pub_ids = {str(payload.get("publication_id", "")) for payload in parent_context.state.branch_publications}
                for publication in publications:
                    if publication.publication_id in existing_pub_ids:
                        continue
                    parent_context.state.branch_publications.append(model_dump(publication))
                    existing_pub_ids.add(publication.publication_id)
                parent_context.state.branch_states[branch_plan.branch_id] = model_dump(
                    BranchState(
                        branch_id=branch_plan.branch_id,
                        status=status,
                        parent_frame_id=branch_plan.parent_frame_id,
                        assigned_node_ids=list(branch_plan.assigned_node_ids),
                        merge_priority=branch_plan.merge_priority,
                        predicted_solve=branch_plan.predicted_solve,
                        reserved_budget=branch_plan.reserved_budget,
                        publications=publications,
                        budget_consumed=_branch_budget_consumed(),
                        verifier_support=verifier_support,
                        unresolved_critical=unresolved_critical,
                        cancellation_record=cancellation_record,
                        error=error,
                    )
                )
                self._publish_checkpoint_envelope(parent_context, task, plan, parent_context.seed, boundary)

        def _mirror_branch_side_effect(receipt: SideEffectReceipt) -> None:
            with persist_lock:
                self._record_side_effect_receipt(parent_context, receipt)
            publication_kind = "trace_rows"
            allow_when_cancelled = False
            payload = {
                "event": "branch_side_effect_recorded",
                "side_effect_id": receipt.side_effect_id,
                "action_kind": receipt.action_kind,
                "status": receipt.status,
            }
            if receipt.action_kind == "tool_launch":
                publication_kind = "handle_or_job_refs"
                payload["result_ref"] = dict(receipt.result_ref or {})
            if cancellation_event.is_set():
                publication_kind = "cleanup_reconciliation"
                allow_when_cancelled = True
                payload["event"] = "branch_cleanup_reconciliation"
            self._emit_branch_publication(
                branch_context,
                publication_kind=publication_kind,
                logical_key=f"{branch_plan.branch_id}.{receipt.action_kind}.{receipt.side_effect_id}",
                payload=payload,
                allow_when_cancelled=allow_when_cancelled,
            )

        branch_context.side_effect_callback = _mirror_branch_side_effect
        branch_context.checkpoint_callback = _persist_branch_progress

        frame = AgentFrame(
            frame_id=stable_hash(plan.request_id, branch_plan.branch_id, "frame")[:16],
            agent=branch_shell.agent_pool.clone("root"),
            request_id=plan.request_id,
            plan_id=plan.plan_id,
            trace_context=branch_plan.trace_context or branch_context.trace_context,
            objective=plan.objective,
            operation_ids=list(branch_plan.assigned_node_ids),
            depth=1,
            role="worker",
            worker_id=branch_plan.branch_id,
            tool_scope=list(parent_context.state.visible_tool_names),
            model_class="small",
            branch_group_id="root-frontier",
            metadata={
                "merge_priority": branch_plan.merge_priority,
                "parent_run_node_id": parent_context.trace_context.run_node_id if parent_context.trace_context else None,
            },
        )
        branch_context.active_frame = frame
        self._emit_branch_publication(
            branch_context,
            publication_kind="trace_rows",
            logical_key=f"{branch_plan.branch_id}.validated",
            payload={
                "event": "branch_plan_validated",
                "assigned_node_ids": list(branch_plan.assigned_node_ids),
            },
        )
        branch_context.publish_checkpoint_boundary("after_branch_plan_validation")
        if cancellation_event.is_set():
            return self._cancelled_branch_result(
                branch_plan,
                branch_context,
                len(branch_plan.assigned_node_ids),
                reason="fatal_branch_fault",
                details={},
            )

        operations = [self._plan_node_by_id(plan, node_id) for node_id in branch_plan.assigned_node_ids]
        try:
            output, _, checkpoint = branch_runtime._execute_isolated_frame(
                branch_context,
                frame,
                operations,
                isolate_runtime_state=False,
            )
            branch_context.raise_if_cancelled()
        except BranchCancelled:
            return self._cancelled_branch_result(
                branch_plan,
                branch_context,
                len(branch_plan.assigned_node_ids),
                reason="fatal_branch_fault" if cancellation_event.is_set() else "parent_stop_policy",
                details={},
            )
        if (
            branch_budget.calls > branch_plan.reserved_budget.model_calls_max
            or branch_budget.checks > branch_plan.reserved_budget.checks_max
            or branch_budget.latency > branch_plan.reserved_budget.latency_max + 1e-9
        ):
            raise HardInvalidation(f"branch {branch_plan.branch_id} exceeded reserved budget")

        verifier_support = self._worker_support(task, output)
        unresolved_critical = 0 if output else len(branch_plan.assigned_node_ids)
        self._emit_branch_publication(
            branch_context,
            publication_kind="candidate_artifact",
            logical_key=f"{branch_plan.branch_id}.artifact",
            payload={
                "artifact": output,
                "summary": model_dump(checkpoint.summary),
                "predicted_solve": branch_plan.predicted_solve,
            },
            verifier_support=verifier_support,
            unresolved_critical=unresolved_critical,
        )
        self._emit_branch_publication(
            branch_context,
            publication_kind="trace_rows",
            logical_key=f"{branch_plan.branch_id}.terminal",
            payload={"event": "branch_completed"},
        )
        branch_context.publish_checkpoint_boundary("after_branch_terminal_completion")
        return BranchResult(
            branch_plan=branch_plan,
            branch_state=BranchState(
                branch_id=branch_plan.branch_id,
                status="completed",
                parent_frame_id=branch_plan.parent_frame_id,
                assigned_node_ids=list(branch_plan.assigned_node_ids),
                merge_priority=branch_plan.merge_priority,
                predicted_solve=branch_plan.predicted_solve,
                reserved_budget=branch_plan.reserved_budget,
                publications=[
                    model_validate(BranchPublication, payload)
                    for payload in branch_context.state.branch_publications
                ],
                budget_consumed=_branch_budget_consumed(),
                verifier_support=verifier_support,
                unresolved_critical=unresolved_critical,
            ),
            artifact=output,
            verifier_support=verifier_support,
            unresolved_critical=unresolved_critical,
            side_effect_receipts=[
                model_validate(SideEffectReceipt, payload)
                for payload in branch_context.state.side_effect_receipts
            ],
        )
>>>>>>> REPLACE
```

### 13. Replace `_cancelled_branch_result()` so cancellation records are explicit and cancelled branches only publish cleanup/reconciliation records

```text
<<<<<<< SEARCH
    def _cancelled_branch_result(
        self,
        branch_plan: BranchPlan,
        publications: list[BranchPublication],
        unresolved_critical: int,
    ) -> BranchResult:
        return BranchResult(
            branch_plan=branch_plan,
            branch_state=BranchState(
                branch_id=branch_plan.branch_id,
                status="cancelled",
                parent_frame_id=branch_plan.parent_frame_id,
                assigned_node_ids=list(branch_plan.assigned_node_ids),
                merge_priority=branch_plan.merge_priority,
                predicted_solve=branch_plan.predicted_solve,
                reserved_budget=branch_plan.reserved_budget,
                publications=publications,
                budget_consumed={},
                unresolved_critical=unresolved_critical,
                cancellation_record=CancellationRecord(
                    reason="parent_stop_policy",
                    details={},
                    created_at=now_ts(),
                ),
            ),
            artifact=None,
            verifier_support=0.0,
            unresolved_critical=unresolved_critical,
        )
=======
    def _cancelled_branch_result(
        self,
        branch_plan: BranchPlan,
        branch_context: PolicyContext | None,
        unresolved_critical: int,
        *,
        reason: str,
        details: Mapping[str, Any] | None,
    ) -> BranchResult:
        publications: list[BranchPublication] = []
        if branch_context is not None:
            self._emit_branch_publication(
                branch_context,
                publication_kind="cleanup_reconciliation",
                logical_key=f"{branch_plan.branch_id}.cancelled.cleanup",
                payload={
                    "event": "branch_cancelled_cleanup",
                    "side_effect_ids": [
                        str(payload.get("side_effect_id", ""))
                        for payload in branch_context.state.side_effect_receipts
                    ],
                    "open_handle_ids": list(branch_context.state.open_handle_ids),
                },
                unresolved_critical=unresolved_critical,
                allow_when_cancelled=True,
            )
            branch_context.publish_checkpoint_boundary("after_branch_cancellation_cleanup")
            publications = [
                model_validate(BranchPublication, payload)
                for payload in branch_context.state.branch_publications
            ]
        return BranchResult(
            branch_plan=branch_plan,
            branch_state=BranchState(
                branch_id=branch_plan.branch_id,
                status="cancelled",
                parent_frame_id=branch_plan.parent_frame_id,
                assigned_node_ids=list(branch_plan.assigned_node_ids),
                merge_priority=branch_plan.merge_priority,
                predicted_solve=branch_plan.predicted_solve,
                reserved_budget=branch_plan.reserved_budget,
                publications=publications,
                budget_consumed={},
                unresolved_critical=unresolved_critical,
                cancellation_record=CancellationRecord(
                    reason=reason,
                    details=dict(details or {}),
                    created_at=now_ts(),
                ),
            ),
            artifact=None,
            verifier_support=0.0,
            unresolved_critical=unresolved_critical,
        )
>>>>>>> REPLACE
```

### 14. Update the two existing `_cancelled_branch_result(...)` call sites to the new signature

```text
<<<<<<< SEARCH
            return self._cancelled_branch_result(branch_plan, publications, len(branch_plan.assigned_node_ids))
=======
            return self._cancelled_branch_result(
                branch_plan,
                branch_context,
                len(branch_plan.assigned_node_ids),
                reason="parent_stop_policy",
                details={},
            )
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
            return self._cancelled_branch_result(branch_plan, publications, len(branch_plan.assigned_node_ids))
=======
            return self._cancelled_branch_result(
                branch_plan,
                branch_context,
                len(branch_plan.assigned_node_ids),
                reason="fatal_branch_fault" if cancellation_event.is_set() else "parent_stop_policy",
                details={},
            )
>>>>>>> REPLACE
```

### 15. Make the baseline topology policy explicitly frontier-oriented

This is a small but useful cleanup once `runner.py` starts passing only runnable frontier nodes. It also future-proofs the policy if `node_id` and `op_id` ever diverge.

```text
<<<<<<< SEARCH
    def select_workers(self, ctx, frame, operations: Sequence[Any]) -> list[dict[str, Any]]:
        config = ctx.profile.topology
        op_ids = [op.op_id for op in operations]
=======
    def select_workers(self, ctx, frame, operations: Sequence[Any]) -> list[dict[str, Any]]:
        config = ctx.profile.topology
        frontier_nodes = list(operations)
        op_ids = [op.node_id for op in frontier_nodes]
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
                    op_ids,
                    key=lambda op_id: 0 if any(op.op_id == op_id and op.dependencies for op in operations) else 1,
=======
                    op_ids,
                    key=lambda op_id: 0 if any(op.node_id == op_id and op.dependencies for op in frontier_nodes) else 1,
>>>>>>> REPLACE
```

---

## File: `agintor/runtime_sdk/runtime_entry.py`

### 16. Sort transfer-scored invocations inside each shared runtime group by `(episode_order, task_id, request_id)` while preserving response order

Add a helper above `_run_batch()`:

```text
<<<<<<< SEARCH
def _run_batch(args: argparse.Namespace) -> int:
=======
def _ordered_batch_invocations(request: RuntimeBatchRequest) -> list[tuple[int, str, RuntimeTaskInvocation]]:
    indexed_invocations = [
        (index, model_validate(RuntimeTaskInvocation, model_dump(invocation_payload)))
        for index, invocation_payload in enumerate(request.invocations)
    ]
    grouped: dict[str, list[tuple[int, RuntimeTaskInvocation]]] = {}
    for index, invocation in indexed_invocations:
        grouped.setdefault(batch_evaluation_unit_key(invocation), []).append((index, invocation))
    ordered: list[tuple[int, str, RuntimeTaskInvocation]] = []
    for group_key, rows in grouped.items():
        if rows and rows[0][1].task.transfer_scored and str(rows[0][1].task.episode_id or "").strip():
            rows = sorted(
                rows,
                key=lambda item: (
                    int(getattr(item[1].task, "episode_order", 0) or 0),
                    item[1].task.task_id,
                    item[1].request_id,
                ),
            )
        ordered.extend((index, group_key, invocation) for index, invocation in rows)
    return ordered


def _run_batch(args: argparse.Namespace) -> int:
>>>>>>> REPLACE
```

Then replace the execution loop:

```text
<<<<<<< SEARCH
    results: list[RunResult] = []
    runners_by_group: dict[str, TaskRuntime] = {}
    for invocation_payload in request.invocations:
        invocation = model_validate(RuntimeTaskInvocation, model_dump(invocation_payload))
        group_key = batch_evaluation_unit_key(invocation)
        runner = runners_by_group.get(group_key)
        if runner is None:
            shell = FixedShell(
                Path(args.workspace) / group_key.replace("/", "_"),
                artifact_mode=ArtifactMode(args.artifact_mode),
            )
            runner = TaskRuntime(
                runtime,
                shell,
                provider,
                budget_overrides=request.budget_overrides,
                runtime_profile=runtime_profile,
            )
            runners_by_group[group_key] = runner
        results.append(
            runner.run_task(
                model_validate(BenchmarkTask, model_dump(invocation.task)),
                invocation.seed,
                request_id=invocation.request_id,
                trace_context=invocation.trace_context,
            )
        )
=======
    results_by_index: dict[int, RunResult] = {}
    runners_by_group: dict[str, TaskRuntime] = {}
    for original_index, group_key, invocation in _ordered_batch_invocations(request):
        runner = runners_by_group.get(group_key)
        if runner is None:
            shell = FixedShell(
                Path(args.workspace) / group_key.replace("/", "_"),
                artifact_mode=ArtifactMode(args.artifact_mode),
            )
            runner = TaskRuntime(
                runtime,
                shell,
                provider,
                budget_overrides=request.budget_overrides,
                runtime_profile=runtime_profile,
            )
            runners_by_group[group_key] = runner
        results_by_index[original_index] = runner.run_task(
            model_validate(BenchmarkTask, model_dump(invocation.task)),
            invocation.seed,
            request_id=invocation.request_id,
            trace_context=invocation.trace_context,
        )
    results = [results_by_index[index] for index in sorted(results_by_index)]
>>>>>>> REPLACE
```

---

## File: `tests/test_runtime_execution.py`

### 17. Make the checkpoint helper return the last matching boundary by default

Branch-side provider / tool boundaries will now appear multiple times per request. The test helper should stop taking the first match.

```text
<<<<<<< SEARCH
def _checkpoint_for_boundary(shell: FixedShell, request_id: str, boundary: str) -> CheckpointEnvelope:
    index_path = shell.workspace / "checkpoints" / request_id / "index.json"
    rows = json.loads(index_path.read_text(encoding="utf-8"))
    target = next(row for row in rows if row["boundary"] == boundary)
    return shell.load_checkpoint_envelope(checkpoint_ref=target["checkpoint_ref"])
=======
def _checkpoint_for_boundary(
    shell: FixedShell,
    request_id: str,
    boundary: str,
    *,
    occurrence: int = -1,
) -> CheckpointEnvelope:
    index_path = shell.workspace / "checkpoints" / request_id / "index.json"
    rows = json.loads(index_path.read_text(encoding="utf-8"))
    matches = [row for row in rows if row["boundary"] == boundary]
    if not matches:
        raise AssertionError(f"checkpoint boundary {boundary!r} was not published")
    target = matches[occurrence]
    return shell.load_checkpoint_envelope(checkpoint_ref=target["checkpoint_ref"])
>>>>>>> REPLACE
```

### 18. Add focused regression tests for the Worker 03 slice

Append the following tests near the end of `tests/test_runtime_execution.py`:

```text
<<<<<<< SEARCH
def test_resume_from_after_branch_completion_reuses_saved_branch_frontier(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    _force_horizontal(monkeypatch, runtime, ["w0", "w1"])
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_direct_response_task("horizontal.resume")
    first_runner = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([{"text": "same"}]),
        budget_overrides={"M_max": 4, "Q_max": 1},
    )
    first_run = first_runner.run_task(task, 0)
    envelope = _checkpoint_for_boundary(shell, first_run.request_id, "after_branch_completion")

    resume_runner = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([]),
        budget_overrides={"M_max": 4, "Q_max": 1},
    )
    resumed_run = resume_runner.resume_from_checkpoint(envelope)

    assert resumed_run.hard_invalid is False
    assert resumed_run.model_calls == first_run.model_calls
    assert resumed_run.artifact == first_run.artifact
=======
def test_resume_from_after_branch_completion_reuses_saved_branch_frontier(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    _force_horizontal(monkeypatch, runtime, ["w0", "w1"])
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_direct_response_task("horizontal.resume")
    first_runner = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([{"text": "same"}]),
        budget_overrides={"M_max": 4, "Q_max": 1},
    )
    first_run = first_runner.run_task(task, 0)
    envelope = _checkpoint_for_boundary(shell, first_run.request_id, "after_branch_completion")

    resume_runner = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([]),
        budget_overrides={"M_max": 4, "Q_max": 1},
    )
    resumed_run = resume_runner.resume_from_checkpoint(envelope)

    assert resumed_run.hard_invalid is False
    assert resumed_run.model_calls == first_run.model_calls
    assert resumed_run.artifact == first_run.artifact


def test_resume_reuses_reconciled_provider_completion_without_generate(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_direct_response_task("resume.reconciled-provider")
    plan = compile_execution_plan_from_task(
        task,
        request_id="resume.reconciled-provider",
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    envelope = CheckpointEnvelope(
        checkpoint_id="checkpoint.resume.reconciled-provider.0001",
        runtime_abi=runtime.kernel_manifest.runtime_abi,
        storage_schema_version=runtime.kernel_manifest.storage_schema_version,
        runtime_hash=runtime.runtime_hash,
        request_id=plan.request_id,
        plan_id=plan.plan_id,
        task_id=task.task_id,
        seed=0,
        sequence_no=1,
        boundary="after_provider_completion",
        created_at=now_ts(),
        plan_snapshot=model_dump(plan),
        task_payload=model_dump(task),
        queued_frames=[
            QueuedFrameSnapshot(
                frame_id="frame-root",
                request_id=plan.request_id,
                plan_id=plan.plan_id,
                objective=plan.objective,
                operation_ids=["respond"],
                depth=0,
                role="root",
                trace_context=plan.trace_context,
                agent_snapshot=_canonical_root_snapshot(),
            )
        ],
        side_effect_receipts=[
            SideEffectReceipt(
                side_effect_id="provider-completion.reconciled",
                action_fingerprint="provider-completion.reconciled",
                idempotency_key="provider-completion.reconciled",
                action_kind="provider_completion",
                request_id=plan.request_id,
                plan_id=plan.plan_id,
                frame_id="frame-root",
                node_id="respond",
                request_digest="provider-completion.reconciled",
                backend="local",
                status="reconciled",
                trace_context=OpenAITraceContext(request_id=plan.request_id, task_id=task.task_id, seed=0, op_id="respond"),
                result_ref={"text": "reconciled hello", "model_name": "replay/small", "input_tokens": 0, "output_tokens": 0},
                created_at=now_ts(),
            )
        ],
        plan_node_status={"respond": "running"},
    )

    provider = ReconcilingReplayProvider([])
    resumed = TaskRuntime(runtime, shell, provider).resume_from_checkpoint(envelope)

    assert resumed.hard_invalid is False
    assert resumed.artifact == "reconciled hello"
    assert provider.generate_calls == 0


def test_resume_reuses_reconciled_tool_completion_without_rerun(tmp_path):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = BenchmarkTask(
        task_id="resume.reconciled-tool",
        family="top",
        prompt="Return the maximum of [3, 9, 4]",
        task_type="structured_ops",
        allowed_tool_categories=["math/basic"],
        operations=[
            OperationSpec(
                op_id="max",
                kind="builtin",
                output_key="max",
                description="Compute maximum number",
                tool_hint="math/basic/max_number",
                args={"numbers": [3, 9, 4]},
            )
        ],
        expected=9,
        verifier_type="number_exact",
        verification_required=True,
        allow_best_effort=False,
    )
    plan = compile_execution_plan_from_task(
        task,
        request_id="resume.reconciled-tool",
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    envelope = CheckpointEnvelope(
        checkpoint_id="checkpoint.resume.reconciled-tool.0001",
        runtime_abi=runtime.kernel_manifest.runtime_abi,
        storage_schema_version=runtime.kernel_manifest.storage_schema_version,
        runtime_hash=runtime.runtime_hash,
        request_id=plan.request_id,
        plan_id=plan.plan_id,
        task_id=task.task_id,
        seed=0,
        sequence_no=1,
        boundary="after_tool_completion",
        created_at=now_ts(),
        plan_snapshot=model_dump(plan),
        task_payload=model_dump(task),
        queued_frames=[
            QueuedFrameSnapshot(
                frame_id="frame-root",
                request_id=plan.request_id,
                plan_id=plan.plan_id,
                objective=plan.objective,
                operation_ids=["max"],
                depth=0,
                role="root",
                trace_context=plan.trace_context,
                agent_snapshot=_canonical_root_snapshot(),
            )
        ],
        side_effect_receipts=[
            SideEffectReceipt(
                side_effect_id="tool-completion.reconciled",
                action_fingerprint="tool-completion.reconciled",
                idempotency_key="tool-completion.reconciled",
                action_kind="tool_completion",
                request_id=plan.request_id,
                plan_id=plan.plan_id,
                frame_id="frame-root",
                node_id="max",
                request_digest="tool-completion.reconciled",
                backend="local",
                status="reconciled",
                trace_context=OpenAITraceContext(request_id=plan.request_id, task_id=task.task_id, seed=0, op_id="max"),
                result_ref={"tool_name": "math/basic/max_number", "output": 9},
                created_at=now_ts(),
            )
        ],
        plan_node_status={"max": "running"},
    )

    original_run_tool = shell.tool_executor.run_tool
    called = {"count": 0}

    def _forbidden_run_tool(*args, **kwargs):
        called["count"] += 1
        return original_run_tool(*args, **kwargs)

    shell.tool_executor.run_tool = _forbidden_run_tool
    resumed = TaskRuntime(runtime, shell, ReplayProvider([])).resume_from_checkpoint(envelope)

    assert resumed.hard_invalid is False
    assert resumed.artifact == 9
    assert called["count"] == 0


def test_branch_side_effect_boundaries_checkpoint_before_branch_completion(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    _force_horizontal(monkeypatch, runtime, ["w0", "w1"])
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_direct_response_task("horizontal.branch-provider-boundaries")
    runner = TaskRuntime(
        runtime,
        shell,
        ReplayProvider([{"text": "same"}]),
        budget_overrides={"M_max": 4, "Q_max": 1},
    )
    result = runner.run_task(task, 0)

    provider_launch = _checkpoint_for_boundary(shell, result.request_id, "after_provider_launch")
    assert any(receipt.branch_id in {"w0", "w1"} for receipt in provider_launch.side_effect_receipts)
    assert any(branch_state.status in {"running", "completed"} for branch_state in provider_launch.branch_state)


def test_horizontal_mode_passes_only_active_frontier_to_select_workers(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = BenchmarkTask(
        task_id="horizontal.frontier-only",
        family="top",
        prompt="Compute root values and then a dependent value",
        task_type="structured_ops",
        allowed_tool_categories=["math/basic"],
        operations=[
            OperationSpec(op_id="a", kind="builtin", output_key="a", description="Compute maximum number", tool_hint="math/basic/max_number", args={"numbers": [1, 3]}),
            OperationSpec(op_id="b", kind="builtin", output_key="b", description="Compute median number", tool_hint="math/basic/median_number", args={"numbers": [2, 4]}),
            OperationSpec(op_id="c", kind="builtin", output_key="c", description="Compute maximum number", tool_hint="math/basic/max_number", args={"numbers": [5, 6]}, dependencies=["a", "b"]),
        ],
        expected={},
        verifier_type="none",
        verification_required=False,
        allow_best_effort=True,
    )
    runner = TaskRuntime(runtime, shell, ReplayProvider([]))
    plan = compile_execution_plan_from_task(
        task,
        request_id="horizontal.frontier-only",
        seed=0,
        runtime_hash=runtime.runtime_hash,
        runtime_dir=str(runtime.runtime_dir),
    )
    captured: dict[str, list[str]] = {}

    def _select_mode(ctx, frame, operations):
        return "horizontal"

    def _select_workers(ctx, frame, operations):
        captured["frontier"] = [node.node_id for node in operations]
        return [{"worker_id": "w0", "instruction": "frontier", "op_ids": [node.node_id for node in operations], "predicted_solve": 1.0, "tool_scope": ctx.state.visible_tool_names, "agent_id": "root"}]

    monkeypatch.setattr(runtime.topology, "select_mode", _select_mode)
    monkeypatch.setattr(runtime.topology, "select_workers", _select_workers)
    result = runner.run_task(task, 0, plan=plan)

    assert result.hard_invalid is False
    assert captured["frontier"] == ["a", "b"]


def test_fatal_branch_fault_allows_only_cleanup_publications_after_cancellation(tmp_path, monkeypatch):
    runtime_dir = init_runtime(tmp_path / "runtime")
    runtime = load_runtime(runtime_dir, runtime_backend="local")
    _force_horizontal(monkeypatch, runtime, ["w0", "w1"])
    shell = FixedShell(tmp_path / "workspace", artifact_mode=ArtifactMode.ALWAYS)
    task = _make_direct_response_task("horizontal.cancelled-cleanup-only")
    runner = TaskRuntime(runtime, shell, ReplayProvider([{"text": "unused"}]), budget_overrides={"M_max": 4, "Q_max": 1})

    def _stub_run_branch_plan(parent_context, task, plan, branch_plan, cancellation_event, persist_lock):
        if branch_plan.branch_id == "w0":
            raise RuntimeError("boom")
        cancellation_event.wait(timeout=1.0)
        branch_context = PolicyContext(
            runtime_dir=runner.runtime.runtime_dir,
            shell=parent_context.shell.fork_branch(branch_plan.branch_id),
            task=task,
            request_id=plan.request_id,
            plan=plan,
            trace_context=branch_plan.trace_context or parent_context.trace_context,
            provider=ReplayProvider([]),
            profile=runner.runtime_profile,
            seed=parent_context.seed,
            state=RuntimeState(request_id=plan.request_id, plan_id=plan.plan_id, execution_state="branching", visible_tool_names=list(parent_context.state.visible_tool_names)),
            budget=RuntimeBudget(**runner._runtime_budget_overrides()),
            trace=[],
            objective=plan.objective,
            runtime_backend=parent_context.runtime_backend,
            cancellation_event=cancellation_event,
        )
        return runner._cancelled_branch_result(
            branch_plan,
            branch_context,
            len(branch_plan.assigned_node_ids),
            reason="fatal_branch_fault",
            details={"error": "boom"},
        )

    monkeypatch.setattr(runner, "_run_branch_plan", _stub_run_branch_plan)
    with pytest.raises(Exception):
        runner.run_task(task, 0)
    envelope = _checkpoint_for_boundary(shell, "horizontal.cancelled-cleanup-only", "after_branch_cancellation_cleanup")
    cancelled_publications = [publication for publication in envelope.branch_publications if publication.branch_id == "w1"]
    assert all(publication.publication_kind != "candidate_artifact" for publication in cancelled_publications)
    assert any(publication.publication_kind == "cleanup_reconciliation" for publication in cancelled_publications)
>>>>>>> REPLACE
```

Note:

- The last test is intentionally scheduler-focused and uses a stubbed branch executor to verify cancellation ordering and publication gating without depending on provider timing flukes.
- If you prefer not to test through `runner.run_task(...)`, you can instead expose a tiny helper around the branch-drain logic and unit-test that helper directly.

### 19. Add a batch-ordering test that targets the new helper in `runtime_entry.py`

Append this focused test near the other runtime execution contract tests:

```text
<<<<<<< SEARCH
from agintor.runtime_api import (
    batch_evaluation_unit_key,
    compile_execution_plan_from_solve_request,
    compile_execution_plan_from_task,
    load_solve_request,
)
=======
from agintor.runtime_api import (
    batch_evaluation_unit_key,
    compile_execution_plan_from_solve_request,
    compile_execution_plan_from_task,
    load_solve_request,
)
from agintor.runtime_sdk.runtime_entry import _ordered_batch_invocations
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
    assert batch_evaluation_unit_key(invocations[0]) == batch_evaluation_unit_key(invocations[1])
    assert batch_evaluation_unit_key(invocations[0]) != batch_evaluation_unit_key(invocations[2])
=======
    assert batch_evaluation_unit_key(invocations[0]) == batch_evaluation_unit_key(invocations[1])
    assert batch_evaluation_unit_key(invocations[0]) != batch_evaluation_unit_key(invocations[2])


def test_ordered_batch_invocations_sorts_transfer_episode_execution_but_preserves_transport_identity():
    request = type(
        "Req",
        (),
        {
            "invocations": [
                invocations[1],
                invocations[0],
                invocations[2],
            ]
        },
    )()

    ordered = _ordered_batch_invocations(request)

    assert [(group_key, invocation.task.task_id) for _, group_key, invocation in ordered] == [
        ("episode.episode-alpha.seed_1", "episode.step1"),
        ("episode.episode-alpha.seed_1", "episode.step2"),
        ("episode.episode-alpha.seed_2", "episode.step1"),
    ]
    assert [original_index for original_index, _, _ in ordered] == [1, 0, 2]
>>>>>>> REPLACE
```

---

## File: `tests/test_runtime_host.py`

No host-transport diff is required for this Worker 03 slice. The existing host tests already cover request normalization and resume transport shape, which are the only host-facing surfaces touched indirectly here.

If the orchestrator wants explicit coverage that batch response ordering remains transport-stable after execution-order sorting, add that as a host-transport regression only after Worker 01’s run-root transport changes land, because that test belongs to the host/runtime boundary rather than branch execution itself.
