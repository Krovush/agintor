<code_review>
# WS2 Real Dynamic Issues

## Blocks WS3 Handoff

### Explicit verify nodes can be skipped after merge or singleton-frontier completion

- Area:
  - `agintor/runner.py`
  - `TaskRuntime._run_root_frame()`
- Current behavior:
  - when a non-single execution path finishes a singleton frontier node and terminal outputs are now present, root execution calls `_maybe_verify()` inline and returns
  - on plans that already contain an explicit `verify` node after a merge or other late frontier step, that `verify` node never executes
  - `plan_node_status`, checkpoints, and trace events then diverge from the frozen execution plan even though the runtime reports a verified terminal artifact
- Why this is WS3-blocking:
  - WS3 persistence, resume, and recovery lineage depend on the checkpointed runtime state matching the frozen plan contract
  - skipping an explicit plan node breaks that contract directly and makes it unsafe to treat plan-node completion state and structured runtime events as authoritative
- Required fix target:
  - when terminal outputs exist but an explicit `verify` node is still the next runnable plan node, queue the continuation and let that node execute in dependency order
  - do not use inline `_maybe_verify()` as a substitute for a frozen `verify` plan node

### Resume does not reconstruct in-flight branch execution from branch-boundary checkpoints

- Area:
  - `agintor/runner.py`
  - `TaskRuntime._restored_branch_frontier()`
  - `TaskRuntime._restore_runtime_state_snapshot()`
- Current behavior:
  - WS2 publishes checkpoints while branch groups are still running, including branch-side-effect and branch-node-completion boundaries
  - resume only knows how to reuse terminal branch states
  - non-terminal branch state is restored into metadata, but worker execution frames are not recreated from that state
  - a resumed run therefore loses in-flight branch progress and can either refan out from scratch or fail receipt reconciliation
- Why this must be fixed before WS3:
  - WS3 is supposed to inherit the checkpoint envelope and resume contract as a stable foundation
  - a restartable checkpoint artifact is not actually restartable if in-flight branch execution cannot be reconstructed
  - handing this forward would freeze a broken branch-resume semantic into the persistence layer
- Follow-up target:
  - make resume reconstruct runnable branch work from checkpointed branch state instead of only accepting already-terminal branches
  - keep branch-side-effect checkpoints and branch-node checkpoints explicitly covered in runtime resume tests

### Failed branch groups can publish resumable checkpoints that are not actually resumable

- Area:
  - `agintor/runner.py`
  - `TaskRuntime._execute_horizontal_branches()`
- Current behavior:
  - the runtime publishes `after_branch_completion` before checking whether any branch in the group failed
  - if one branch failed, the run still exits through `HardInvalidation`, but `latest_checkpoint_ref` has already been advanced to that published checkpoint
  - host finalization can therefore surface the run as paused or resumable even though resume will immediately fail on the checkpointed failed branch state
- Why this must be fixed before WS3:
  - this breaks the truthfulness of the runtime's resumability contract at the exact point WS3 needs to persist and trust it
  - WS3 should not build on checkpoints that the runtime itself cannot successfully resume
- Follow-up target:
  - do not publish or advertise a resumable post-branch checkpoint once the branch group has already entered a failed terminal condition
  - keep host finalization aligned with only genuinely resumable checkpoints

### Async handle resume cannot reconcile across process restart

- Area:
  - `agintor/runner.py`
  - `TaskRuntime._reconcile_side_effect_receipts()`
  - `agintor/shell.py`
  - checkpoint restore of `open_handles`
- Current behavior:
  - resume restores serialized async handles into `open_handles`, but the live `ToolExecutor._async_processes` table is empty after a process restart
  - the reconciliation path then calls `await_handle()` on a restored `running` handle and gets the synthetic `"async process handle missing"` failure
  - the launch receipt is terminalized as failed instead of being truly reconciled or failing closed as an unresolved launched side effect
- Why this blocks WS3 handoff:
  - WS3 is about durable persistence and recovery on top of the WS2 checkpoint and receipt contract
  - if WS2 already fabricates failure for resumed async launches, WS3 will persist a broken recovery meaning instead of a trustworthy one
  - this directly weakens the claimed solve-time contract for restartable checkpoints and resumable side effects
- Immediate fix target:
  - make resumed async launches reconcile from durable process/job evidence, or fail closed when that evidence is unavailable
  - do not silently convert a missing in-memory process record into a terminal failed handle state during resume
</code_review>

<plan>
# WS2 Remaining Dynamic Issues Fix Plan

## Summary
- All four review items are legitimate.
- The common failure pattern is that WS2 currently treats “terminal outputs exist,” “a checkpoint file exists,” and “a running handle exists” as sufficient proof of correctness/resumability even when the frozen plan or durable recovery contract has not actually been satisfied.
- Existing coverage already proves successful explicit merge/verify execution, successful completed-branch resume, sync/provider receipt reconciliation, and sibling cancellation. It does not cover the remaining restart/recovery edge cases below.

## Issue Assessment
- `verify` skip: real in `TaskRuntime._run_root_frame()` and the other inline `_maybe_verify()` completion branches. The baseline topology usually hides it by returning `single` for a one-node frontier, but the runtime contract is still wrong because explicit plan-node execution should not depend on policy choice.
- Branch resume gap: real in `_restore_runtime_state_snapshot()`, `_restored_branch_frontier()`, and branch persistence inside `_run_branch_plan()`. Current checkpoints persist parent-facing `BranchState`, not branch-local shell/runtime/queue state.
- False resumability after branch failure: real in `_execute_horizontal_branches()`, `_publish_checkpoint_envelope()`, `RunStore.latest_usable_checkpoint_ref()`, and host finalization. The latest checkpoint is treated as resumable even when it snapshots a failed branch group.
- Async handle restart failure: real in `_reconcile_side_effect_receipts()` plus `ToolExecutor.await_handle()`. Resume currently calls a same-process waiter on restored handles and fabricates failure from missing executor memory.

## Key Changes
- Replace implicit terminal verification shortcuts with plan-driven terminal progression in `runner.py`.
  - Add one helper that decides whether runtime may finalize, must queue a root continuation, or may inline best-effort verification.
  - Rule: if any explicit `verify` node exists and is not completed, never call `_maybe_verify()` as a substitute; queue or execute the node in dependency order.
  - Apply that helper to root empty-frontier handling, singleton-frontier handling, merge completion branches, and post-loop finalization so terminalization has one policy.
- Make branch checkpoints actually restartable.
  - Add a serialized `BranchResumeSnapshot` under `RuntimeStateSnapshot.branch_resume_snapshots`.
  - Snapshot contents must include the `BranchPlan`, branch-local runtime state, branch budget totals, active and queued frames, branch-local shell snapshot, and branch side-effect receipts. Keep `BranchState` as the parent-facing summary.
  - Persist or refresh that snapshot at `after_branch_side_effect`, `after_branch_node_completion`, and terminal branch completion.
  - On resume, rebuild in-flight branches from stored snapshots instead of re-running `select_workers()` and refanning out from scratch. Terminal completed or cancelled branches may continue to use the restored-frontier fast path.
  - Extend resume request-id rebinding and container checkpoint path rewriting to recurse through the new nested branch snapshot structures.
- Make checkpoint exposure truthful.
  - Add `resume_eligible` and `resume_ineligibility_reason` to checkpoint metadata (`CheckpointEnvelope` and `CheckpointReference`), and bump only `checkpoint_schema_version` to `agintor.checkpoint-envelope.v3`.
  - Keep every written checkpoint in the index, but treat `LATEST.json`, `RunResult.latest_checkpoint_ref`, and host-side resumability as “latest eligible checkpoint,” not “latest file written.”
  - When a branch group has any failed branch or a propagated `ResumeRecoveryError`, do not advance the eligible checkpoint pointer to that post-failure envelope. Earlier eligible branch-boundary checkpoints remain the resume target.
  - Add host-side validation so a transport-reported checkpoint ref is accepted only if the stored envelope is `resume_eligible`; otherwise fall back to `latest_usable_checkpoint_ref`.
- Separate same-process async waiting from cross-process recovery.
  - Add a durable `reconcile_async_handle()` path in `tool_runtime.py` for restored handles.
  - Resume logic must never call `await_handle()` on a restored running handle unless the live `_async_processes` record still exists.
  - Reconciliation order: reuse durable terminal evidence if present; otherwise report the handle as unresolved and let strict resume fail closed or best-effort resume block the node. Do not mutate the handle or receipt into terminal failure solely because executor memory is gone.

## Public Interfaces
- Add `BranchResumeSnapshot`.
- Add `RuntimeStateSnapshot.branch_resume_snapshots`.
- Add `CheckpointEnvelope.resume_eligible`, `CheckpointEnvelope.resume_ineligibility_reason`, and matching `CheckpointReference` fields.
- Add `ToolExecutor.reconcile_async_handle(handle, handle_table)` or equivalent structured recovery API that can return `completed`, `failed`, `cancelled`, or `unresolved` without forcing a false failure.

## Test Plan
- Keep existing passing coverage for successful explicit merge/verify execution, successful completed-branch resume, sync/provider receipt reconciliation, and sibling cancellation.
- Add a runner test that forces a non-`single` policy on a one-node merge frontier and proves the explicit `verify` node executes instead of inline verification.
- Add a resume test from `after_branch_side_effect` and another from `after_branch_node_completion` that restore in-flight branch workers without reissuing already-launched side effects.
- Add a failure test where a branch group publishes a diagnostic post-failure checkpoint but the run, result, and host expose only the earlier eligible checkpoint as resumable.
- Add strict and best-effort async-handle restart tests for a restored `running` handle with an empty executor table; assert blocked or fail-closed, not synthetic terminal failure.
- Add container and resume-rebind tests for nested branch snapshot path and request-id rewriting.

## Assumptions
- No backward-compatibility work is needed; update tests and fixtures directly to the new checkpoint schema.
- Diagnostic non-resumable checkpoints remain on disk for debugging, but they are never surfaced as `latest_checkpoint_ref`.
- Same-process `await_handle()` remains valid during a live run; only restart-time recovery must use the new reconciliation path.

## Execution Handoff
`workstream_candidates`
- terminal-progress centralization in `runner.py`
- branch snapshot schema and plumbing in `schemas.py`, `runtime_api.py`, `runner.py`, and `shell.py`
- checkpoint eligibility and host/run-store truthfulness in `runner.py`, `run_store.py`, and `runtime_host.py`
- async-handle recovery in `tool_runtime.py` and resume reconciliation in `runner.py`

`shared_file_risks`
- `agintor/runner.py`, `agintor/schemas.py`, and `tests/test_runtime_execution.py` will be touched by almost every fix
- nested snapshot support also risks `runtime_api.py` and `container_runtime.py`

`ordering_constraints`
- land schema additions and nested rewrite/rebind support first
- land async-handle recovery API before in-flight branch resume
- land branch-resume reconstruction before checkpoint-eligibility exposure changes
- finish by centralizing terminal verification and updating end-to-end tests

`validation_invariants`
- a run may not report `verified=True` unless the explicit `verify` plan node completed or was safely restored as completed
- a checkpoint may be surfaced as resumable only if resume can re-enter the runtime state machine without immediate hard invalidation
- restored running async handles may become terminal only from durable evidence, never from missing in-memory executor state
- branch resume must preserve branch-local receipts, open handles, node status, and accepted publications

`open_decisions`
- none; use the diagnostic-vs-eligible checkpoint split and the embedded `BranchResumeSnapshot` design above
</plan>