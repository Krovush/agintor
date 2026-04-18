# WS2 Fix Plan 01: Frontier Order Before Branch Fanout

## Scope

This plan covers only the WS2 frontier-order defect in `agintor/runner.py` around `_active_runnable_frontier()`. It does not implement the fix, and it does not expand into broader branch-group/compiler refactors.

## Files And Functions Inspected

- `implementation_workstreams/WORKSTREAM_2_RUNTIME_EXECUTION_AND_ORCHESTRATION.md`
  - Phase 3 ordering contract:
    - runnable nodes execute sequentially unless explicitly grouped
    - outside a branch group, runnable nodes execute in plan declaration order and then `node_id`
    - inside a branch group, branches execute concurrently but merge order remains deterministic
- `PROJECT TARGET SPEC.md`
  - fixed-shell invariants around deterministic merge and horizontal isolation
- `PROJECT PAPER.md`
  - deterministic runtime topology / horizontal-worker invariants
- `agintor/runner.py`
  - `_run_root_frame()`
  - `_ordered_execution_nodes()`
  - `_active_runnable_frontier()`
- `agintor/runtime_api.py`
  - `_attach_branch_groups()`
  - `_append_merge_nodes()`
  - `compile_execution_plan_from_task()`
- `agintor/schemas.py`
  - `ExecutionPlan.validate_execution_plan()`
- `tests/test_runtime_execution.py`
  - existing vertical/horizontal runtime tests near explicit merge, branch reservations, branch resume, and cancellation

## Problem Statement

WS2 requires the runtime to honor deterministic runnable order outside branch groups. Today `_active_runnable_frontier()` violates that contract when the current runnable set mixes:

- an earlier singleton node with no `branch_group_id`, and
- a later grouped frontier whose nodes do have a `branch_group_id`.

Instead of returning the earliest runnable singleton first, the runtime scans the full runnable set, grabs the first non-empty `branch_group_id` it can find anywhere, and returns that group. This allows later branch fanout to jump ahead of earlier non-group work.

## Confirmed Failure Mode

I reproduced the defect with a real compiled plan shape derived from a benchmark-style task:

1. Declare operations in this order:
   - `dep_a`
   - `dep_b`
   - `a <- dep_a`
   - `b <- dep_b`
   - `c <- dep_b`
2. Compile with `compile_execution_plan_from_task()`.
3. Mark `dep_a`, `dep_b`, and the compiler-inserted merge for the initial root group as completed.
4. Call `_active_runnable_frontier()`.

Observed result:

```text
['b', 'c']
```

Expected result:

```text
['a']
```

Why this plan shape is valid:

- `_attach_branch_groups()` groups `b` and `c` together because they share identical dependencies.
- `_append_merge_nodes()` rewrites downstream dependencies through merge nodes, so the mixed frontier naturally appears after earlier work has completed.
- At that point the ordered runnable list is effectively `a, b, c`, but the current frontier selector still chooses the later group.

## Root Cause

The defect is localized to `TaskRuntime._active_runnable_frontier()` in `agintor/runner.py`.

Current behavior:

```python
active_group = branch_group_id or next((node.branch_group_id for node in runnable if node.branch_group_id), None)
if active_group is None:
    return [runnable[0]]
return [node for node in runnable if node.branch_group_id == active_group]
```

The bug is the `next(...)` scan across the entire runnable list. That logic ignores the identity of the first runnable node, which is the thing WS2 says must define execution order outside branch groups.

## Required Contract Behavior

The active frontier must be chosen from the leading runnable unit in deterministic runnable order:

- If `branch_group_id` is explicitly provided, return the currently runnable members of that exact group.
- Otherwise:
  - compute runnable nodes in deterministic order
  - inspect only the first runnable node
  - if the first runnable node is ungrouped, the active frontier is that single node
  - if the first runnable node belongs to a branch group, the active frontier is the full currently runnable set for that same group

This preserves both halves of the WS2 contract:

- declaration order outside groups
- grouped fanout as a unit once the group itself becomes the leading runnable unit

## Proposed Code Changes

### 1. Narrow the frontier-selection rule to the first runnable node

In `agintor/runner.py`:

- update `_active_runnable_frontier()` so the implicit group selection is derived from `runnable[0]`, not from the first grouped node found anywhere in `runnable`
- preserve the explicit `branch_group_id` override path for contexts that are already scoped to a branch group

Intended logic:

```python
runnable = ordered currently-runnable nodes
if not runnable:
    return []
if branch_group_id is not None:
    return [node for node in runnable if node.branch_group_id == branch_group_id]
first = runnable[0]
if not first.branch_group_id:
    return [first]
return [node for node in runnable if node.branch_group_id == first.branch_group_id]
```

### 2. Make the contract explicit in code

Add a short docstring or nearby comment explaining:

- `_ordered_execution_nodes()` is the source of deterministic runnable order
- `_active_runnable_frontier()` selects the leading runnable unit
- a branch group may fan out only when one of its members is itself the first runnable node

### 3. Keep the fix local to the runtime scheduler

Do not change:

- `ExecutionPlan` schema
- `branch_group_id` compilation rules
- merge-node insertion
- topology policy interfaces

This blocker is scheduler behavior, not a schema or compiler contract gap.

## Test Plan

Add focused regression coverage in `tests/test_runtime_execution.py`.

### Test 1: Mixed frontier returns earlier singleton first

Build a task through `compile_execution_plan_from_task()` with operations:

- `dep_a`
- `dep_b`
- `a <- dep_a`
- `b <- dep_b`
- `c <- dep_b`

Then:

- mark `dep_a`, `dep_b`, and the compiler-inserted root merge as completed
- call `_active_runnable_frontier()`
- assert the returned node IDs are `['a']`

This is the direct regression for the confirmed failure.

### Test 2: After singleton completion, grouped frontier becomes active

Using the same compiled plan:

- additionally mark `a` as completed
- call `_active_runnable_frontier()`
- assert the returned node IDs are `['b', 'c']`

This proves the fix does not suppress valid group fanout; it only delays it until the declaration-ordered singleton is consumed.

### Test 3: Root-frame integration respects frontier order before branching

Run the same task end-to-end with topology patched so:

- `select_mode()` returns `horizontal` only when the candidate frontier has more than one node
- otherwise it returns `single`

Assert from trace order that:

- node `a` completes before any branch-start event for the `b/c` group
- the later grouped frontier still branches and merges normally

This covers the actual WS2 runtime path, not just the helper.

### Test 4: Explicit branch-group override path still works

Directly call `_active_runnable_frontier(..., branch_group_id=<group id>)` on a state where that group is runnable and assert it still returns only that group's members in deterministic order.

This protects resume/continuation behavior from accidental regression while changing the implicit-selection path.

## Risks And Open Questions

### Low risk: explicit override path

The only behavior change should be the implicit selection path. The explicit `branch_group_id` override should remain semantically unchanged.

### Do not widen scope into branch-group validation

It may be tempting to add new validation rules such as branch-group contiguity in declaration order. That is not required to fix the blocker and would expand WS2 scope unnecessarily.

### Do not refactor plan compilation here

`_attach_branch_groups()` and `_append_merge_nodes()` already produce a valid plan shape that exposes the bug. The issue is that the runner ignores the leading runnable singleton, not that the plan compiler cannot represent the frontier correctly.

## Recommended Implementation Order

1. Update `_active_runnable_frontier()` in `agintor/runner.py`.
2. Add the direct mixed-frontier regression test.
3. Add the follow-on frontier progression test.
4. Add the root-frame integration test only if needed for confidence beyond the direct regression.

## Expected Outcome

After the fix:

- mixed runnable frontiers honor declaration order before branch fanout
- later grouped work no longer jumps ahead of earlier singleton work
- horizontal mode still fans out valid groups once that group becomes the leading runnable frontier
- WS2 execution order aligns with the workstream contract instead of with incidental branch-group discovery order
