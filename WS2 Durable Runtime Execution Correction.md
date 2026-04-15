# WS2 Durable Runtime Execution Correction

## Summary
Implement this as one coherent correction across kernel bundling, durable run persistence, resume, provider preflight/accounting, and branch cleanup. The current WS2 code has the right major building blocks, but several boundary contracts drifted apart:

- exported runtimes import `run_store` but do not bundle it
- durable run manifests can persist relative `run_root` values, which breaks checkpoint lookup from the runtime subprocess working directory
- resume still assumes solve-only persistence even though batch/grouped runs are persisted as durable runs
- grouped batch lifecycle is derived from the last invocation only
- prompt-mode provider preflight uses a `context_items` heuristic instead of compiled execution semantics
- usage accounting is read from provider instances, so branch-local cloned providers disappear from final totals
- cancelled branches emit cleanup metadata without actually cancelling or reconciling outstanding async work

Main implementation surfaces for this correction are:

- `agintor/runtime_sdk/bundle.py` and `agintor/run_store.py`
- `agintor/runtime_host.py` and `agintor/runtime_sdk/runtime_entry.py`
- `agintor/runner.py` and `agintor/tool_runtime.py`
- `tests/test_runtime_host.py`, `tests/test_runtime_execution.py`, and `tests/test_container_runtime.py`

Keep the current WS2 version line (`agintor-runtime-abi-v4`, `agintor-storage-v2`) unless schema validation makes a bump unavoidable. This is a semantics correction, not a new feature line.

## Implementation changes
1. **Bundle and path hardening**
   - Add `agintor/run_store.py` to the bundled kernel source list and manifest generation.
   - Resolve `RunStore.workspace` at construction time and derive `runs_root` from the resolved workspace.
   - Persist `RunManifest.run_root` as an absolute resolved path from `create_run()`, not the caller’s raw relative workspace string.
   - Do not change Docker/container path rewriting behavior; only the stored host-side run root becomes canonical.

2. **Make durable runs execution-unit based instead of solve-only**
   - Keep the current `request/request.json`, `task.json`, `plan.json`, and `runtime_identity.json` layout; do not introduce a second persistence tree.
   - Treat `request/request.json` as an execution-unit envelope that may contain either `runtime_solve_request` or `runtime_task_invocation`.
   - Remove the solve-only gate in `RuntimeHost._resolve_runtime_resume_request()`. Accept both known request kinds and fail only on unknown kinds or missing checkpoint data.
   - Load the selected checkpoint envelope during resume resolution and treat it as the authoritative source for the task and plan being resumed. This is required because the latest checkpoint in a grouped batch run may belong to a later invocation than the one originally written to `request/request.json`.
   - Use the stored request bundle only to recover the original prompt-mode `SolveRequest` when the checkpoint plan origin is `user_request`.
   - Keep resume granularity at the grouped execution-unit level: one run ID per `batch_evaluation_unit_key`, one resume target per durable run, no per-invocation resume within a grouped episode.

3. **Fix resume request identity and grouped batch lifecycle**
   - In runtime entry resume, always reapply `RuntimeResumeRequest.request_id` to the logical `SolveRequest` returned in the final `SolveResult`.
   - For benchmark resume, continue using `benchmark_task_to_solve_request(..., request_id=override)`.
   - For user-request resume, recover the stored `SolveRequest` and copy it with the overridden request ID before building the final `SolveResult`.
   - Finalize grouped batch runs by reducing over every `RunResult` in the group, in evaluation order.
   - Use this exact grouped lifecycle rule:
     - `completed` only if every grouped result is terminal and non-failing.
     - `paused` if any grouped result failed but at least one usable checkpoint exists for the run.
     - `failed` if a grouped failure occurred and no usable checkpoint exists.
   - Persist the first failing `failure_kind` in evaluation order, not the last result’s failure kind.
   - Persist the latest non-empty checkpoint reference found across the grouped results, not `runs[-1]` by assumption.
   - Update every `RunResult` in the group with the same final run lifecycle metadata after the reduction step.

4. **Replace provider heuristics with execution-derived preflight**
   - Delete the `bool(solve_request.context_items)` heuristic from `_request_may_trigger_default_provider_side_paths()`.
   - Add a host-side helper that compiles the runtime execution plan for prompt-mode solves using the same runtime-side compilation path already used by solve execution.
   - Treat a prompt-mode request as requiring hosted/default provider support only when the compiled plan contains provider-backed node kinds.
   - The local-only allowlist is:
     - `memory_lookup`
     - `checkpoint`
     - `merge`
     - `verify`
   - Any compiled plan containing other node kinds should still require provider support.
   - If prompt-mode compilation fails (`unsupported_operation`, `missing_capability`, `budget_exceeded`, `template_mismatch`), do not infer provider requirements from raw request shape; let runtime solve fail in the normal typed way.

5. **Make usage accounting execution-scoped, not provider-instance-scoped**
   - Add a runtime-owned usage ledger for each `TaskRuntime` execution. Do not use provider objects as the source of truth.
   - Snapshot `self.provider.usage_summary()` at `run_task()` and `resume_from_checkpoint()` entry and exit, compute the main-thread delta, and merge that into the execution ledger.
   - Extend `BranchResult` to carry `provider_usage`, computed as the delta of the branch provider clone across the branch run.
   - When a branch completes, merge `branch_result.provider_usage` into the parent execution ledger before building the final `RunResult`.
   - Add `provider_usage` to `RunResult` and treat it as the authoritative per-execution usage summary.
   - In runtime entry:
     - solve and resume responses use `run_result.provider_usage`, not `provider.usage_summary()`
     - batch responses sum `run.provider_usage` across returned `RunResult`s, not the shared provider object’s counters
   - This must work correctly for grouped batch invocations that reuse one `TaskRuntime` across several invocations; each `RunResult` should carry only the delta for that invocation.

6. **Make branch cancellation terminal only after real cleanup**
   - Add an explicit async-handle cancellation API to `ToolExecutor` that:
     - terminates the tracked subprocess if still alive
     - removes it from `_async_processes`
     - writes final stderr/result artifacts as needed
     - updates the handle table state to `cancelled`
   - Refactor cancelled-branch finalization so it performs cleanup before returning a `BranchResult(status="cancelled")`.
   - Cleanup must:
     - cancel any branch-owned open handles still running
     - reconcile or close branch-owned side-effect receipts
     - emit receipt and reconciliation publications only after cleanup succeeds
   - Use these status rules:
     - handle state: `cancelled`
     - receipt status for terminated-but-not-completed work: `abandoned`
   - If any branch-owned handle or side effect cannot be brought to a terminal reconciled or abandoned state, fail closed by raising a runtime failure or hard invalidation instead of returning a cleanly cancelled branch.
   - Keep post-cancellation publications limited to cleanup and reconciliation records.

## Public/API/type changes
- `RunResult`
  - add `provider_usage: dict[str, Any]`
- `BranchResult`
  - add `provider_usage: dict[str, Any]`
- `ToolExecutor`
  - add a public cancellation helper for async handles used by branch cleanup
- `RuntimeHost`
  - resume resolution now supports both stored execution-unit envelope kinds (`runtime_solve_request`, `runtime_task_invocation`)
- No CLI command shape changes are required.

## Test plan
- Add a focused kernel-bundle regression that materializes a runtime bundle and asserts `runtime_sdk/agintor_runtime/run_store.py` exists.
- Extend durable-run tests to cover relative host workspace inputs and assert persisted `run_root` and latest checkpoint refs are absolute, resolved host paths.
- Add resume coverage for a durable run whose stored request bundle kind is `runtime_task_invocation`; assert `resume` no longer rejects batch and eval runs.
- Add grouped batch lifecycle coverage:
  - earlier invocation fails, later invocation succeeds -> run is not marked `completed`
  - checkpoint exists on a failing grouped run -> run becomes `paused` and resumable
  - no checkpoint exists on a failing grouped run -> run becomes `failed`
- Add resume identity coverage asserting an overridden `ResumeRequest.request_id` reaches the final `SolveResult.request_id` for both benchmark and user-request resumes.
- Add host preflight coverage for a prompt-mode exact symbol or file lookup with `context_items` and no hosted credentials; it must pass preflight because the compiled plan is local-only.
- Add execution usage coverage:
  - sequential direct-response run reports non-zero per-run usage
  - horizontal branch run reports the sum of all branch model calls, tokens, and cost
  - batch response total equals the sum of `run_results[*].provider_usage`
- Add branch cancellation coverage:
  - cancellation terminates running async handles
  - cancelled branches publish cleanup and reconciliation records only
  - unresolved cleanup fails the run closed instead of returning `cancelled`
- Keep the existing checkpoint/resume and Docker path-rewrite tests passing under the new absolute-path canonicalization.

## Assumptions and defaults
- Resume granularity is the durable execution unit keyed by `batch_evaluation_unit_key`, not individual invocations inside that unit.
- The checkpoint envelope is the authoritative source for the task and plan being resumed; stored request bundles are supplemental context only.
- Provider requirement detection is execution-plan based. `context_items` alone never imply hosted-provider requirement.
- Execution usage is owned by `TaskRuntime` and surfaced through `RunResult.provider_usage`.
- Branch cleanup failures fail closed during live execution; they may not degrade to a clean cancellation.
- Keep the current WS2 version line unless schema validation makes an ABI/storage bump unavoidable.
