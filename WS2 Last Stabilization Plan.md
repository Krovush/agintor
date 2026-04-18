You’re right. Here is the corrected plan.

**WS2 Last Stabilization Plan**

**Summary**  
Implement three fixes in the current change:
- fix root execution so an empty runnable frontier is treated as terminal/blocked state, not as permission to re-feed the full plan
- fix grouped run reduction so `paused` is preserved as resumable, not collapsed into `completed`
- fix host finalization so runtime-reported checkpoint refs remain authoritative, including external checkpoint refs

No ABI, CLI, schema, or export-bundle changes.

**Implementation Changes**

**1. Fix empty-frontier handling in the root runtime loop**  
Target: [agintor/runner.py](C:\Users\yaros\Desktop\Agintor MVP\agintor\runner.py)

Update `TaskRuntime._run_root_frame()` so completed plan nodes are never reintroduced into topology selection.

Make these changes:
- compute `frontier_nodes = self._active_runnable_frontier(...)` at the top of `_run_root_frame()`
- delete the fallback `candidate_nodes = frontier_nodes or execution_nodes`
- before `select_mode()`, add explicit empty-frontier handling:
  - if `frontier_nodes` is empty and all terminal outputs are already present:
    - build `artifact = self._terminal_artifact(plan, context.state.artifacts)`
    - read explicit verify output from `_resolved_verify_status(...)`
    - if verify output exists, use it as the authoritative `verifier_score` / `verified_terminal`
    - if verify output does not exist and terminal verification is still required, call `_maybe_verify(...)`
    - return immediately without calling topology selection, child proposal, or root continuation scheduling
  - if `frontier_nodes` is empty and terminal outputs are incomplete:
    - return immediately with `artifact=None`
    - do not call topology selection
    - do not propose children
    - do not queue another root continuation
- after that branch, use only `candidate_nodes = frontier_nodes`
- in the `len(frontier_nodes) < 2` path, execute only `frontier_nodes`; remove the fallback `frontier_nodes or self._ordered_execution_nodes(plan)`
- do not change `_active_runnable_frontier()`
- do not change queue structure, plan schema, or runtime result schema

Required outcome:
- completed `builtin`, `merge`, and `verify` nodes may still be skipped by `_execute_operations()`, but they must never re-enter topology selection as fresh work
- multi-node plans with completed merge/verify state must terminate correctly instead of drifting into `node_reused_from_checkpoint` churn and `controlled_failure`

**2. Treat only `completed` as a non-failing terminal grouped run**  
Target: [agintor/runtime_api.py](C:\Users\yaros\Desktop\Agintor MVP\agintor\runtime_api.py)

Update `_run_result_is_non_failing_terminal()` to use an allowlist instead of an exclusion list.

Make these changes:
- return `False` if `run.hard_invalid`
- normalize `run.run_lifecycle_state or run.lifecycle_state`
- return `True` only when the normalized lifecycle state is exactly `"completed"`
- return `False` for `"paused"`, `"running"`, `"failed"`, `"cancelled"`, `"pruned"`, empty, and unknown values
- leave `reduce_grouped_run_results()` output shape unchanged

Required outcome:
- paused resumable runs stay paused through grouped reduction
- grouped lifecycle semantics remain:
  - `cancelled` if any executed member is cancelled
  - `completed` only if all executed members truly completed
  - otherwise `paused` if any checkpoint ref exists
  - otherwise `failed`

**3. Preserve runtime-reported checkpoint refs during host finalization**  
Target: [agintor/runtime_host.py](C:\Users\yaros\Desktop\Agintor MVP\agintor\runtime_host.py)

Update `RuntimeHost._finalize_execution_unit()` so runtime-reported checkpoint refs are preserved, including refs outside the durable run root.

Make these changes:
- replace the current one-source checkpoint resolution with this precedence:
  - `reported_checkpoint_ref = str(reduction.get("latest_checkpoint_ref") or "").strip() or None`
  - `durable_checkpoint_ref = self.run_store.latest_usable_checkpoint_ref(manifest.run_root)`
  - `latest_checkpoint_ref = reported_checkpoint_ref or durable_checkpoint_ref`
- use that resolved `latest_checkpoint_ref` for:
  - lifecycle derivation
  - `finish_attempt(...)`
  - `finish_run(...)`
  - `response.solve_result.latest_checkpoint_ref`
  - `response.solve_result.checkpoint_ref`
  - each `RunResult.latest_checkpoint_ref`
  - each `RunResult.checkpoint_ref`
- do not add file-existence checks here
- preserve current prune behavior exactly

Required outcome:
- if the runtime already returned a checkpoint ref, host finalization must not erase it just because `RunStore.latest_usable_checkpoint_ref(manifest.run_root)` returns `None`
- external checkpoint refs remain resumability proofs
- durable run-store lookup remains valid fallback behavior

**Test Plan**

Target files:
- [tests/test_runtime_execution.py](C:\Users\yaros\Desktop\Agintor MVP\tests\test_runtime_execution.py)
- [tests/test_runtime_host.py](C:\Users\yaros\Desktop\Agintor MVP\tests\test_runtime_host.py)

Add or extend these tests:

1. `test_root_empty_frontier_does_not_reschedule_completed_plan_nodes`
- use a multi-node plan with explicit merge and verify nodes
- force vertical execution so the runtime reaches the continuation path
- arrange for terminal outputs and verify output to already exist before the final root continuation
- assert:
  - root does not respawn children for already-completed nodes
  - merge/verify nodes complete once
  - no repeated `node_reused_from_checkpoint` loop appears for completed merge/verify nodes
  - the run exits through the intended terminal path instead of drifting into `controlled_failure`

2. `test_reduce_grouped_run_results_treats_paused_run_as_non_terminal`
- construct a `RunResult` with:
  - `run_lifecycle_state="paused"`
  - non-empty checkpoint ref
  - `hard_invalid=False`
- assert:
  - reduced lifecycle is `"paused"`
  - `latest_checkpoint_ref` is preserved
  - `resumable` is `True`
  - result is not reduced to `"completed"`

3. keep the existing grouped-success control case
- preserve the existing payload-error/non-failure grouped reduction test
- it should still reduce to `"completed"` when the run is truly terminal

4. `test_runtime_host_solve_finalization_preserves_external_checkpoint_ref`
- stub solve execution to return a checkpoint ref outside `manifest.run_root`
- make `host.run_store.latest_usable_checkpoint_ref(...)` return `None`
- assert:
  - manifest lifecycle is `"paused"`
  - manifest `latest_checkpoint_ref` equals the runtime-reported external ref
  - solve result keeps that ref
  - solve result remains resumable

5. `test_runtime_host_batch_finalization_preserves_external_checkpoint_ref`
- stub batch execution to return `RunResult.checkpoint_ref` outside `manifest.run_root`
- make `host.run_store.latest_usable_checkpoint_ref(...)` return `None`
- assert:
  - grouped manifest lifecycle is `"paused"`
  - all returned runs keep the external checkpoint ref
  - all returned runs remain resumable

6. keep the existing durable-run-root fallback case
- preserve the current in-run-root checkpoint batch pause test
- it should continue to pass unchanged

**Acceptance Criteria**
- an empty runnable frontier no longer causes completed plan nodes to be reintroduced into topology selection
- multi-node continuation paths with completed merge/verify state terminate correctly
- paused runs remain paused through grouped reduction
- runtime-reported external checkpoint refs survive host finalization unchanged
- existing durable-run-root checkpoint behavior remains correct