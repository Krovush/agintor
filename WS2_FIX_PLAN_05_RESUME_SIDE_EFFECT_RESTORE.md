# WS2 Fix Plan 05: Resume-Side-Effect Restoration

## Scope

This plan covers the resume-state reconstruction gap centered on:

- `agintor/runner.py`
  - `TaskRuntime._restore_from_checkpoint()`
  - `TaskRuntime._restore_runtime_state_snapshot()`
  - `TaskRuntime._reconcile_side_effect_receipts()`
  - `TaskRuntime._execute_operations()`
  - `TaskRuntime._execute_direct_response()`
  - `TaskRuntime._execute_tool_operation()`
- `agintor/runtime_api.py`
  - `PolicyContext.run_model_request()`
- `agintor/schemas.py`
  - `RuntimeStateSnapshot`
  - `SideEffectReceipt`
  - `CheckpointEnvelope`
- `tests/test_runtime_execution.py`

I also read the governing docs that define the contract this fix must satisfy:

- `implementation_workstreams/WORKSTREAM_2_RUNTIME_EXECUTION_AND_ORCHESTRATION.md`
- `TRACE_AND_PLANNING_IMPROVEMENTS_PLAN.md`
- `PROJECT TARGET SPEC.md`
- `PROJECT PAPER.md`

## Problem Statement

WS2 says resume must rebuild runtime state from checkpoint envelopes, per-node status, artifacts, receipts, branch state, and verifier/budget state, and that receipt-backed side effects must not be re-executed on resume.

The current implementation only partially satisfies that contract.

Checkpoint boundaries `after_provider_completion` and `after_tool_completion` are emitted inside the side-effect helpers:

- `PolicyContext.run_model_request()` publishes `after_provider_completion`
- `TaskRuntime._execute_tool_operation()` publishes `after_tool_completion`

Those boundaries happen before `_execute_operations()` writes the node output into `context.state.artifacts` and before it flips `context.state.plan_node_status[node_id]` to `"completed"`.

As a result, those checkpoints serialize a stale node snapshot:

- `plan_node_status[node_id] == "running"`
- the node output is absent from `runtime_state_snapshot.artifacts`
- the shell snapshot has no artifact node yet for that operation
- the receipt ledger does contain a terminal result that is sufficient to reconstruct the node

On resume, `_restore_from_checkpoint()`:

1. restores the stale runtime snapshot,
2. reconciles receipts,
3. writes the reconciled receipt list back to `context.state.side_effect_receipts`,
4. only marks unreconciled nodes as `"recovery_blocked"`,
5. never projects a successfully completed or reconciled receipt back into node status or artifact state.

The resumed run therefore re-enters the node. Today that usually does not reissue the provider/tool call because the receipt short-circuits inside `run_model_request()` / `_execute_tool_operation()`, but the runtime is still not actually restoring completed node state. The node restarts, emits fresh node-level events, and only then recreates the artifact/state that the checkpoint should already have reconstructed.

That is a real WS2 contract hole even though it no longer appears to blindly reissue the side effect in the root-path reproductions I ran.

## What I Verified

### Code behavior

I inspected these concrete paths:

- `agintor/runner.py:555-600`
- `agintor/runner.py:864-959`
- `agintor/runner.py:2632-2711`
- `agintor/runner.py:2955-3111`
- `agintor/runtime_api.py:290-409`

The critical flow is:

1. provider/tool helper records a terminal receipt and publishes a checkpoint boundary,
2. the checkpoint captures receipt completion but not node completion,
3. resume restores the stale node snapshot,
4. receipt reconciliation never promotes the node back to `"completed"`,
5. `_execute_operations()` runs the node again because it is still `"running"`, not `"completed"`.

### Local reproductions

I directly reproduced both root-level cases:

1. Resume from `after_provider_completion`
   - checkpoint had `artifacts == {}`
   - checkpoint had `plan_node_status == {"respond": "running"}`
   - receipt ledger already contained a terminal `provider_completion`
   - resume reused the receipt and avoided a provider call, but still re-entered the node

2. Resume from `after_tool_completion`
   - checkpoint had `artifacts == {}`
   - checkpoint had `plan_node_status == {"expr": "running"}`
   - receipt ledger already contained a terminal `tool_completion`
   - resume reused the receipt output, but still re-entered the node

### Existing tests

I inspected and ran the current resume/checkpoint coverage:

- `pytest tests/test_runtime_execution.py -k "resume or checkpoint" -q`
- result: all relevant current tests passed

That matches the code behavior: the system already prevents the obvious blind reissue in the tested paths, but it still does not restore the completed node state that WS2 says resume must rebuild.

There is currently no focused regression test that asserts:

- a checkpoint at `after_provider_completion` restores the node as already completed,
- a checkpoint at `after_tool_completion` restores the node as already completed,
- resume skips node restart entirely rather than merely short-circuiting the inner side effect,
- restored node outputs are re-materialized consistently with the normal execution path.

## Root Cause

The root cause is architectural, not a one-line conditional.

The code currently has two separate notions of "completed":

1. side-effect completion
   - stored in `SideEffectReceipt`
2. node completion
   - stored in `RuntimeState.plan_node_status`
   - plus `RuntimeState.artifacts`
   - plus shell short-term artifact nodes

The checkpoint boundaries `after_provider_completion` and `after_tool_completion` intentionally capture the first notion before the second notion has been committed.

That is acceptable only if resume has a deterministic projection step that converts terminal receipts back into completed node state during restoration.

That projection step does not exist today.

## Required Invariants After the Fix

The fix should establish these invariants:

1. If resume sees a terminal receipt with enough information to reconstruct a root-owned node output, resume must restore the node to `"completed"` without re-entering node execution.
2. Restored node outputs must match the normal execution-path output shape.
   - direct response: same parsed JSON-or-string result as `_execute_direct_response()`
   - tool nodes: same `output` value that `_execute_tool_operation()` would have returned
3. Restoring a node from a receipt must update all runtime state surfaces that WS2 treats as canonical for completed work:
   - `plan_node_status`
   - `artifacts`
   - `unresolved_goals`
   - short-term artifact graph when that node’s artifact had not yet been materialized at checkpoint time
4. Resume must preserve fail-closed behavior for unresolved receipts.
   - strict: fail
   - best effort: mark node `recovery_blocked`
5. The fix must not leak branch-local outputs into parent artifact state before branch publication/merge.

## Proposed Architecture

### 1. Add an explicit receipt-to-node restoration pass

Add a dedicated helper in `TaskRuntime` that runs immediately after `_reconcile_side_effect_receipts()` in `_restore_from_checkpoint()`.

Suggested shape:

- `_restore_completed_nodes_from_receipts(context, receipts)`

Responsibilities:

- inspect the already reconciled terminal receipt set,
- identify which receipts are sufficient to reconstruct node output,
- project those receipts back into runtime node/artifact state,
- no-op for nodes already restored in the snapshot,
- never restore unreconciled / failed / abandoned receipts as completed nodes.

This is the missing architectural step between receipt reconciliation and resumed execution.

### 2. Restore by node semantics, not by action kind alone

Do not hard-code restore logic as "provider_completion means X" and "tool_completion means Y" at the top level.

Instead:

1. resolve the owning `PlanNode` by `receipt.node_id`,
2. inspect `PlanNode.node_kind`,
3. use a small output-restoration adapter per supported node kind.

That keeps the restore logic aligned with execution semantics rather than with receipt naming.

Initial supported restore set:

- `direct_response`
- `tool_call`
- `tool_synthesis`

This covers the actual current receipt-producing node kinds on the root path.

### 3. Extract shared output decoding helpers

The restore path must produce the same artifact shape as the normal execution path. Do not duplicate JSON parsing or tool-output normalization ad hoc.

Refactor output decoding into shared helpers used by both normal execution and restore projection.

Suggested helpers:

- `_coerce_direct_response_output(text: str) -> Any`
  - current behavior in `_execute_direct_response()` is `json.loads(response.text)` with string fallback
- `_restore_output_from_receipt(node: PlanNode, receipt: SideEffectReceipt) -> tuple[bool, Any]`
  - returns whether the receipt is sufficient to restore the node and, if so, the normalized output

This keeps the restore path behaviorally identical to the non-resume path.

### 4. Restrict automatic projection to root-owned nodes

Do not blindly project every receipt with a `node_id` back into `context.state.artifacts`.

Branch receipts carry `branch_id` and belong to branch-isolated state until publication/merge. Parent-state restoration must not materialize branch outputs early and violate WS2 copy-in/publication-out semantics.

For this fix, automatic receipt-to-node projection should apply only when the receipt belongs to the parent/root execution path:

- `receipt.branch_id` is empty/null

Branch-local resume remains governed by branch state/publication semantics and should not be collapsed into parent artifacts by this fix.

If later work needs full branch-local node restoration, that should be designed explicitly through `BranchState` rather than smuggled through parent artifact restoration.

### 5. Re-materialize the missing artifact node in short-term memory

When a receipt restores a node that had not yet reached `_record_artifact_node()` at checkpoint time, resume should also recreate the corresponding short-term artifact node and `PRODUCES` edge if the producing run node is known.

Without this, runtime state and short-term graph would diverge:

- `context.state.artifacts` would claim the node completed,
- but the shell graph would still look as if no artifact was ever produced.

This matters for memory/retrieval consistency and keeps resume semantics faithful to the execution path that would have existed had the process not been interrupted.

### 6. Let `_execute_operations()` skip restored nodes naturally

Do not add special-case resume conditionals deep inside execution if they can be avoided.

If restoration correctly sets:

- `plan_node_status[node_id] = "completed"`
- `artifacts[output_key] = restored_output`

then the existing early exit in `_execute_operations()` will do the right thing:

- emit `node_reused_from_checkpoint`
- skip node execution entirely

That is the correct steady-state behavior after the fix.

## Concrete Code Changes

### `agintor/runner.py`

#### `TaskRuntime._restore_from_checkpoint()`

After:

- `_restore_runtime_state_snapshot(...)`
- `_reconcile_side_effect_receipts(...)`

add:

- `_restore_completed_nodes_from_receipts(context, receipts)`

Then:

- write restored receipts to `context.state.side_effect_receipts`
- apply `recovery_blocked` only after the completion-restoration pass
- recompute `context.state.unresolved_goals`

#### New helper: `_restore_completed_nodes_from_receipts()`

Implementation plan:

1. Build a `node_map` from `context.plan.nodes`.
2. Build a best-terminal-receipt map keyed by `node_id`.
   - prefer receipts that can actually restore output
   - prefer `completed`/`reconciled` over anything else
3. For each candidate node:
   - skip if `branch_id` is set
   - skip if node already has `plan_node_status == "completed"` and artifact exists
   - resolve restored output through shared helper
   - write output into `context.state.artifacts[node.output_key]`
   - set `context.state.plan_node_status[node.node_id] = "completed"`
   - remove stale `"running"` meaning
   - create short-term artifact node if missing from checkpoint-era graph
4. Update unresolved goals after the pass.

#### New helper: `_restore_output_from_receipt()`

Responsibilities:

- `direct_response`
  - restore from `receipt.result_ref["text"]`
  - decode exactly like `_execute_direct_response()`
- `tool_call` / `tool_synthesis`
  - restore from `receipt.result_ref["output"]`
- otherwise return "not restorable"

This helper should accept reconciled launch receipts too when reconciliation filled in the terminal output.

#### Refactor `_execute_direct_response()`

Move the JSON-or-string parsing into a shared helper so the normal path and restore path cannot drift.

#### Optional trace refinement

No new event is strictly required if the restored node is later skipped through the existing `node_reused_from_checkpoint` path.

If extra trace clarity is desired, add one bounded runtime event such as `node_restored_from_receipt`, but this is optional. The primary correctness win comes from restoring state before execution, not from new logging.

### `agintor/runtime_api.py`

#### `PolicyContext.run_model_request()`

No schema redesign is required here.

Keep the current terminal receipt shape, but treat it as part of the restoration contract:

- `result_ref["text"]`
- token/model metadata
- `node_id`
- `branch_id`
- trace context

The only code change likely needed here is extracting the direct-response output coercion into a shared helper location if that helper lives outside `runner.py`.

### `agintor/schemas.py`

No checkpoint-envelope schema change is required for the fix as planned.

Current receipt/result fields are already sufficient:

- provider receipts carry `text`
- tool receipts carry `output`
- receipts already store `node_id`, `branch_id`, and `status`

Avoid a schema bump unless implementation reveals a genuine missing field.

That keeps the fix inside the frozen WS2 ABI/storage line instead of turning it into another protocol migration.

## Test Plan

### 1. Root direct-response restore from `after_provider_completion`

Add a targeted test that:

1. runs a one-node `direct_response` task,
2. loads the checkpoint at `after_provider_completion`,
3. resumes with an empty `ReplayProvider`,
4. asserts:
   - provider `generate()` is not called during resume,
   - final artifact matches the original artifact,
   - the resumed trace does not restart that node,
   - the resumed path uses checkpoint reuse semantics instead of fresh node execution.

The strongest trace assertion is:

- `node_reused_from_checkpoint` is present for the node
- `node_started` is absent for that node after restore

### 2. Root tool restore from `after_tool_completion`

Add a targeted test for a deterministic tool-backed node, for example a `generated_expression` / `tool_synthesis` case that records `tool_completion`.

Resume from `after_tool_completion` and assert:

- tool executor is not called again,
- output matches the original output,
- node is reused from checkpoint rather than re-executed,
- artifact/state are already restored before the resumed queue advances.

### 3. Reconciled-launch restore path

Add a test that covers the case where resume begins from a non-terminal launch receipt but reconciliation produces terminal output:

- provider launch reconciled by `provider.reconcile_request(...)`
- optional async tool launch reconciled from handle state if practical

If reconciliation yields a reusable terminal result, the node should be projected to completed rather than resumed as running.

### 4. Recovery-blocked still wins when no reusable result exists

Preserve the current fail-closed behavior:

- strict unresolved receipt -> runtime failure
- best-effort unresolved receipt -> `recovery_blocked`

Add/assert that the new restoration pass does not accidentally mark such nodes completed.

### 5. Branch isolation guard

Add a focused test that branch-owned receipts do not get restored into parent `context.state.artifacts` merely because they carry a terminal result.

This is important to prevent the fix from breaking WS2 branch isolation while correcting root resume behavior.

## Risks and Open Questions

### 1. Branch-local restoration is intentionally not solved here

This fix should not infer parent-visible artifacts from branch receipts.

If the team later wants fully resumable mid-branch node state without re-entering branch node execution, that requires an explicit `BranchState` design expansion. It should not be mixed into this targeted fix by mutating parent artifact state.

### 2. Short-term graph duplication must be avoided

If a later checkpoint already captured the artifact node in the shell snapshot, the restore pass must not append a duplicate artifact node. The helper should only materialize the artifact node when the restored node had not yet completed at snapshot time.

### 3. Output restoration must exactly mirror execution semantics

If parsing/normalization differs between:

- `_execute_direct_response()`
- receipt restoration

resume can become semantically non-equivalent even when it avoids reissue. That is why the decode helper should be shared, not duplicated.

## Recommended Order of Implementation

1. Extract shared direct-response output normalization.
2. Add `_restore_completed_nodes_from_receipts()` and wire it into `_restore_from_checkpoint()`.
3. Re-materialize missing short-term artifact nodes for restored root nodes.
4. Add focused root-level regression tests for `after_provider_completion` and `after_tool_completion`.
5. Add the branch-isolation guard test.

## Expected Outcome

After this fix:

- checkpoints taken immediately after provider/tool completion become semantically restartable, not merely receipt-aware,
- resume reconstructs completed node state rather than replaying node logic to rediscover it,
- root-level completed side effects remain non-reissued,
- short-term graph and runtime artifact state stay aligned after resume,
- WS2’s resume contract becomes truthful for this boundary instead of only approximately safe.
