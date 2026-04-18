# WS2 Fix Plan 04: Sibling Cancellation for Ordinary Branch Failures

## Scope

This plan covers only the sibling-cancellation gap in the horizontal branch orchestration path centered on `agintor/runner.py` near `_execute_horizontal_branches()`. It does not implement the fix.

## Problem Statement

WS2 explicitly requires structured concurrency for horizontal execution:

- `implementation_workstreams/WORKSTREAM_2_RUNTIME_EXECUTION_AND_ORCHESTRATION.md`
  - "Branch groups launch, cancel, and join as one unit."
  - "A fatal unrecoverable branch exception triggers sibling cancellation before merge."
  - Regression and acceptance gates require that sibling cancellation works and leaves no orphaned runtime state.
- `PROJECT TARGET SPEC.md`
  - horizontal workers must not share mutable state
  - merge order must be deterministic
  - runtime traces must explain branch and failure behavior
- `PROJECT PAPER.md`
  - horizontal workers share only append-only state
  - deterministic merge and open-handle integrity are hard invariants

The current implementation violates that contract for ordinary branch failures. If one branch returns a terminal `BranchResult` with `branch_state.status == "failed"`, sibling branches are not cancelled immediately. They keep running until natural completion, and the parent only raises `HardInvalidation` after all branch work has already finished and been accounted for.

This means a doomed branch group can still spend model budget, execute tools, emit publications, and create more cleanup work after success is already impossible.

## Root Cause

The failure model is split across two channels, but the scheduler reacts to only one of them.

### Channel 1: raised exceptions

In `agintor/runner.py`:

- `_execute_horizontal_branches()` waits with `wait(future_map, return_when=FIRST_EXCEPTION)`.
- It only triggers sibling cancellation when `future.result()` raises `ResumeRecoveryError`.

That path works because the future itself resolves exceptionally, so `FIRST_EXCEPTION` returns early and the parent sets `cancellation_event`.

### Channel 2: ordinary branch failures

Also in `agintor/runner.py`:

- `_run_branch_plan()` catches normal execution failures and converts them into `_failed_branch_result(...)`.
- That produces `BranchResult(branch_state.status="failed", ...)` instead of re-raising.

Examples:

- reservation overspend -> `failure_kind="reservation_exceeded"`
- verification failure -> `failure_kind="verification_failure"`
- protocol/runtime errors -> `failure_kind="protocol_failure"` or `branch_execution_error`

Because these failures are returned as ordinary values, the future does not complete exceptionally. `FIRST_EXCEPTION` therefore does not fire early, the parent does not set `cancellation_event`, and siblings keep running.

## Evidence From Current Code

### Files inspected

- `C:\Users\yaros\Desktop\Agintor MVP\implementation_workstreams\WORKSTREAM_2_RUNTIME_EXECUTION_AND_ORCHESTRATION.md`
- `C:\Users\yaros\Desktop\Agintor MVP\PROJECT TARGET SPEC.md`
- `C:\Users\yaros\Desktop\Agintor MVP\PROJECT PAPER.md`
- `C:\Users\yaros\Desktop\Agintor MVP\agintor\runner.py`
- `C:\Users\yaros\Desktop\Agintor MVP\agintor\runtime_api.py`
- `C:\Users\yaros\Desktop\Agintor MVP\agintor\schemas.py`
- `C:\Users\yaros\Desktop\Agintor MVP\tests\test_runtime_execution.py`

### Relevant functions and models

- `agintor/runner.py`
  - `_execute_horizontal_branches()`
  - `_run_branch_plan()`
  - `_failed_branch_result()`
  - `_cancelled_branch_result()`
  - `_classify_branch_failure()`
- `agintor/runtime_api.py`
  - `PolicyContext.raise_if_cancelled()`
- `agintor/schemas.py`
  - `BranchPlan`
  - `CancellationRecord`
  - `BranchState`
  - `BranchResult`

### Observed reproduction

I ran an inline reproduction that monkeypatched `_run_branch_plan()` so:

- branch `w0` returned `BranchResult(status="failed")`
- branch `w1` waited to see whether `cancellation_event` was set and returned `cancelled` only if it was

Current behavior produced:

```python
{'w0': 'failed', 'w1': 'completed'}
```

That confirms the review finding: ordinary failed branch results do not trigger sibling cancellation today.

## Architectural Fix Direction

Make the parent branch coordinator react to **terminal branch outcomes**, not only thrown exceptions.

The core rule should be:

> In a horizontal branch group, the first terminal non-recoverable branch failure must initiate sibling cancellation immediately, regardless of whether it arrived as an exception or as `BranchResult(status="failed")`.

This keeps the existing typed branch-state model intact and removes the mismatch between scheduler behavior and branch failure semantics.

## Proposed Code Changes

### 1. Replace one-shot `FIRST_EXCEPTION` orchestration with result-aware draining

File: `agintor/runner.py`

Change `_execute_horizontal_branches()` so it no longer relies on `wait(..., FIRST_EXCEPTION)` as the sole early-stop mechanism.

Recommended shape:

- submit all futures as today
- drain futures incrementally with either:
  - a loop around `wait(..., return_when=FIRST_COMPLETED)`, or
  - `as_completed(...)`
- inspect each finished future as soon as it completes

The scheduler must treat these as cancellation initiators:

- `ResumeRecoveryError` raised from the future
- `BranchResult.branch_state.status == "failed"`

The scheduler should treat these as non-initiators:

- `status == "completed"`
- `status == "cancelled"`

### 2. Introduce one explicit "branch-group cancellation requested" transition

Inside `_execute_horizontal_branches()`, add local state that records:

- whether sibling cancellation has already been initiated
- which branch initiated it
- whether the initiator was:
  - `ResumeRecoveryError`, or
  - a failed `BranchResult`
- the canonical cancellation reason/details to apply to siblings

The first initiator wins. Later failures are collected for accounting but must not rewrite the cancellation reason or create nondeterministic sibling cancellation metadata.

### 3. Map failed branch kinds onto cancellation reasons

Reuse the existing `CancellationRecord.reason` contract in `agintor/schemas.py`.

Suggested mapping:

- `verification_failure` -> `verification_failure`
- `reservation_exceeded` -> `budget_exhaustion`
- `protocol_failure` -> `fatal_branch_fault`
- `branch_execution_error` -> `fatal_branch_fault`
- `cleanup_failure` -> `fatal_branch_fault`
- `ResumeRecoveryError` path -> `fatal_branch_fault`

Add a small helper in `agintor/runner.py` for this mapping so the semantics stay centralized and traceable.

### 4. Cancel siblings cooperatively, then drain all futures

Once cancellation is initiated:

- call `cancellation_event.set()` immediately
- try `future.cancel()` on not-yet-started siblings
- for successfully cancelled not-started futures, synthesize `_cancelled_branch_result(...)`
- for already-running siblings, do not fabricate results immediately; let them return through the existing cooperative cancellation path

This is important because branch cleanup is already implemented in `_cancelled_branch_result()` and depends on the actual branch context when the branch has started.

### 5. Preserve existing cleanup and accounting paths

Do not redesign the cleanup machinery.

Keep using:

- `_cancelled_branch_result()` for terminal sibling cleanup
- `finalize_branch_result(...)` inside `_run_branch_plan()` for persisted branch state and checkpoint publication
- final parent-side accounting for:
  - branch states
  - branch publications
  - side-effect receipts
  - provider usage
  - budget usage

The fix should change **when cancellation starts**, not invent a new post-failure cleanup system.

### 6. Preserve fail-closed group semantics after draining

After all futures are drained:

- if a `ResumeRecoveryError` occurred, re-raise it as today
- else if any branch ended `failed`, keep the existing parent-side `HardInvalidation("branch group failed after accounting: ...")`

The difference is that siblings should now mostly arrive as `cancelled`, not `completed`, once the first failure makes merge impossible.

### 7. Improve trace clarity at the group coordinator level

Add one parent-side runtime event when cancellation is initiated by a failed branch result.

Suggested event shape:

- event: `branch_group_cancellation_requested`
- payload:
  - failing `branch_id`
  - `failure_kind`
  - mapped sibling cancellation reason
  - assigned node ids

This is not strictly required for correctness, but it materially improves the WS2 acceptance gate that traces explain failure behavior from structured events alone.

## Required Tests

Add focused tests in `C:\Users\yaros\Desktop\Agintor MVP\tests\test_runtime_execution.py`.

### 1. Ordinary failed branch result cancels running sibling

Create a regression test where:

- one branch returns `BranchResult(status="failed")`
- another branch waits for `cancellation_event` and only becomes `cancelled` if the parent sets it

Assert:

- failing branch status is `failed`
- sibling status is `cancelled`, not `completed`
- parent run ends hard-invalid
- checkpoint state at `after_branch_completion` shows failed + cancelled terminal states

This is the primary regression for the bug.

### 2. Failed branch kind maps to the correct sibling cancellation reason

At minimum, cover one semantically meaningful case:

- a branch fails with `failure_kind="verification_failure"`

Assert:

- sibling `cancellation_record.reason == "verification_failure"`

Optionally add a second case for:

- `reservation_exceeded` -> `budget_exhaustion`

### 3. Not-yet-started siblings become cancelled without running

Create a case where one branch fails immediately and another future can still be cancelled before start.

Assert:

- the synthesized sibling result is `status="cancelled"`
- it does not produce a completed artifact publication
- it does not contribute completed worker output

### 4. Raised `ResumeRecoveryError` path still behaves correctly

Add or extend coverage so the exceptional path still:

- sets sibling cancellation
- drains outstanding futures
- re-raises the recovery error after accounting

This protects the current behavior while expanding it to failed-result paths.

### 5. No accepted artifact publication from cancelled siblings

Assert that a sibling cancelled because of another branch failure contributes only allowed cleanup/reconciliation publications and does not get appended to the accepted worker artifact set or message board as a completed branch.

## Invariants To Preserve

- Horizontal branch groups still merge only deterministic completed outputs.
- Cancelled branches still use `_cancelled_branch_result()` cleanup and reconciliation rules.
- Failed branches still remain `status="failed"` with `failure_kind`, not rewritten to `cancelled`.
- Parent accounting still happens after all branch futures are drained.
- A failed branch group still fails closed at the parent level.
- Branch traces remain sufficient to explain which branch failed and why siblings were cancelled.

## Risks and Open Questions

### Cooperative cancellation only

The runtime’s cancellation model is cooperative:

- `PolicyContext.raise_if_cancelled()` is checked at safe boundaries
- in-flight provider/tool work may not stop instantaneously

So the correct target is:

- initiate sibling cancellation immediately on first non-recoverable failure
- ensure siblings cannot continue past the next safe cancellation boundary

Do not promise impossible preemption of an already-blocking provider/tool call.

### Branches that finish before seeing cancellation

There is a race where a sibling may become terminal just before it observes the newly set `cancellation_event`.

The implementation should aim for:

- immediate cancellation initiation
- deterministic draining

If a sibling has already reached terminal completion before cancellation is observable, that is acceptable as a race boundary. The bug to fix is the current design where cancellation is never initiated for ordinary failed results.

### Reason mapping policy

The mapping from `failure_kind` to `CancellationRecord.reason` should be explicit and centralized. If the team wants finer-grained reasons later, add them intentionally to `CancellationRecord`, not ad hoc in the scheduler.

## Implementation Order

1. Refactor `_execute_horizontal_branches()` to inspect finished future results incrementally.
2. Add a helper that maps failed branch results to sibling cancellation reasons/details.
3. Trigger `cancellation_event` on first failed `BranchResult` as well as raised `ResumeRecoveryError`.
4. Drain all futures while preserving existing cleanup/accounting behavior.
5. Add the focused regression tests above.
6. Run the horizontal-branch runtime test slice and then the broader WS2 runtime execution test file.

## Summary

The bug is not in branch cleanup itself. The bug is in the parent scheduler’s definition of "failure worth cancelling siblings for." Right now it listens only for thrown exceptions, while the branch runtime intentionally expresses most failures as typed failed results. The correct fix is to make the scheduler structured-concurrency-aware at the `BranchResult` level and reuse the existing cancellation cleanup path.
