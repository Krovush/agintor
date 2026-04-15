# WS2 Validation Review: Remaining Gaps Blocking Workstream 3

## Verdict

Workstream 2 is **not ready to hand off to Workstream 3 yet**.

The recent fixes look real and valuable: the bundled runtime now includes `run_store.py`, durable run roots are canonicalized, batch resume no longer hard-rejects `runtime_task_invocation`, prompt-mode provider preflight is plan-based instead of `context_items`-based, provider usage is aggregated across branch clones, and branch cancellation now performs real cleanup for the currently emitted receipt kinds. The focused WS2 tests pass, and the full test suite also passes.

That said, the implementation still has **three material WS2 contract gaps** that would force WS3 to persist or build on incomplete solve-time semantics:

1. `ExecutionPlan` still overstates what the runtime can actually execute.
2. Runtime traces still do not fully express branch and reconciliation behavior from structured events alone.
3. The runtime state machine and event vocabulary still diverge from the fixed WS2 contract.

These are not just style issues. They are the remaining places where WS2 still says "this is the canonical runtime contract" while the code either does something weaker or leaves critical semantics implicit.

## Validation performed

- Reviewed the current WS2 implementation in:
  - `agintor/runtime_api.py`
  - `agintor/runtime_host.py`
  - `agintor/runtime_sdk/runtime_entry.py`
  - `agintor/runner.py`
  - `agintor/run_store.py`
  - `agintor/shell.py`
  - `agintor/tool_runtime.py`
  - `agintor/container_runtime.py`
- Ran the focused WS2 test suite:

```text
pytest -q tests/test_runtime_host.py tests/test_runtime_execution.py tests/test_container_runtime.py
```

- Ran the full test suite:

```text
pytest -q
```

- Ran targeted runtime probes to validate behavior not covered by tests:
  - a horizontal run to inspect the persisted trace event stream
  - a synthetic `service_action` plan node to verify whether the runner actually executes the advertised node kind

## Blocker 1: `ExecutionPlan` is still not the authoritative execution contract

### What is still wrong

The code now has a typed `ExecutionPlan`, but the implementation still treats it as a thin projection of task operations rather than the full runtime contract WS2 described.

There are two concrete problems here:

1. **The plan advertises node kinds the runner does not actually implement.**
   - `agintor/runtime_api.py:870-880` maps unknown operation kinds to `service_action`.
   - `agintor/runner.py:2268-2283` only has real execution branches for:
     - `memory_lookup`
     - `builtin_op`
     - `tool_call`
     - `tool_synthesis`
     - `direct_response`
   - Every other node kind falls through the final `else` branch and simply returns `resolved_args` as the output.

2. **Checkpoint, merge, and verification boundaries are still implicit runner control flow, not explicit plan nodes.**
   - `agintor/runtime_api.py:947-1018` compiles one `PlanNode` per task operation.
   - It does not emit explicit `checkpoint`, `merge`, or `verify` nodes.
   - `agintor/runner.py:1014-1016` explicitly filters `verify`, `checkpoint`, and `merge` nodes out of executable plan nodes.
   - The runner still performs checkpoint publication, branching, merge, and verification as side control flow around the plan rather than as plan-owned nodes.

### Why this blocks WS3

WS3 is supposed to persist and index solve-time objects **without changing their meaning**. That only works if the WS2 `ExecutionPlan` is already the authoritative execution contract.

Right now it is not. It claims a broader node surface than the runtime can execute, and it still leaves important execution boundaries outside the plan itself. If WS3 starts persisting this as canonical, it will lock in a contract that is partially aspirational and partially implicit.

That is especially risky because the failure mode is not clean rejection. Unhandled node kinds currently degrade into silent pass-through behavior instead of a typed runtime failure. A persistence layer should not be built on top of a contract that can silently succeed with the wrong semantics.

### Proof from runtime behavior

A targeted synthetic task compiled to a `service_action` node and the runner completed it by returning the raw resolved args as the artifact:

```text
plan_nodes ['service_action']
artifact {'x': 1, 'y': 2}
```

That is a direct confirmation that the advertised node kind exists in the contract but not in the executor.

### What needs to happen before WS3

Pick one of these paths and finish it before moving on:

- **Preferred:** make `ExecutionPlan` truthful and authoritative.
  - Implement explicit execution semantics for every node kind the contract advertises.
  - Emit explicit `checkpoint`, `merge`, and `verify` nodes when those boundaries exist.
  - Make the runner dispatch directly from node kind to explicit executor logic.
  - Convert unsupported node kinds into typed runtime failures, never silent pass-through behavior.

- **Fallback if you want to reduce scope first:** shrink the contract to what is really implemented.
  - Remove unimplemented node kinds from the WS2 v1 contract.
  - Do not let schemas, validation, and docs advertise node kinds that the runner cannot execute.
  - Keep `checkpoint`, `merge`, and `verify` implicit only if you also explicitly declare that they are not part of `ExecutionPlan` v1.

Until one of those is done, WS2 is still handing WS3 an overclaimed execution model.

## Blocker 2: runtime traces still do not fully express branch and reconciliation behavior from structured events alone

### What is still wrong

WS2 says runtime behavior should be explainable from structured events alone. The code is not there yet.

Important branch lifecycle and reconciliation markers still never reach the persisted trace:

- Branch lifecycle markers are emitted as `BranchPublication` payloads:
  - `agintor/runner.py:1604-1609` publishes `branch_started`
  - `agintor/runner.py:1679-1681` publishes `branch_completed`
  - `agintor/runner.py:1842-1868` publishes cleanup and reconciliation records for cancelled branches
- Those publications are appended to `context.state.branch_publications`, not folded into the main trace list.
- `agintor/shell.py:168-175` persists only the top-level `trace` list to `traces/*.json`.

### Proof from runtime behavior

A targeted horizontal run produced this persisted event list:

```text
['run_started', 'plan_compiled', 'plan_loaded', 'agent_start', 'mode_selected',
 'checkpoint_published', ..., 'node_started', 'checks_trimmed', 'checks_requested',
 'check_result', 'merge_completed', 'terminal_emitted']
```

Notably absent:

- `branch_started`
- `branch_completed`
- `branch_cancelled`
- `branch_failed`
- `merge_started`
- `side_effect_recorded`
- `plan_validation_failed`

So the runtime can execute horizontal work, but the persisted trace does not actually explain the branch lifecycle from structured events alone.

### Why this blocks WS3

Workstream 3 is supposed to persist and expose solve-time runtime objects, including structured runtime events. Right now the only persisted event stream is still a partial projection:

- the trace file omits branch lifecycle and reconciliation behavior
- branch lifecycle is split between trace rows, checkpoint envelopes, and branch publications

If WS3 moves forward now, it will have to invent its own event projection rules by reverse-engineering mixed trace rows plus checkpoint publications. That would mean WS3 is defining solve-time meaning that WS2 was supposed to freeze.

### What needs to happen before WS3

Finish one canonical event model before moving on.

Recommended direction:

- Make one explicit runtime event stream the source of truth.
- Project branch publications that matter to user-visible runtime behavior back into that canonical event stream.
- Add the missing stable event types WS2 already promised:
  - `branch_started`
  - `branch_completed`
  - `branch_cancelled`
  - `branch_failed`
  - `merge_started`
  - `side_effect_recorded`
  - `plan_validation_failed`
- Keep additional events if useful, but make the promised core event set complete and durable.

Until that exists, WS2 still has an incomplete event contract for WS3 to persist.

## Blocker 3: the runtime state machine and event vocabulary still diverge from the fixed WS2 contract

### What is still wrong

The runner now has a richer internal state machine, but it still does not match the fixed WS2 contract closely enough to freeze it for downstream persistence and diagnostics.

Concrete mismatches:

1. **The persisted event vocabulary still diverges from the WS2 event list.**
   - The root frame records `agent_start` instead of the stable `node_started` event for root execution (`agintor/runner.py:349-356`).
   - `merge_started` is never emitted before merge work begins.
   - `plan_validation_failed` is never emitted on plan validation failures.
   - `branch_started`, `branch_completed`, and branch cancellation cleanup exist only as branch publication payloads rather than canonical trace events.

2. **The execution-state progression still diverges from the fixed transition contract.**
   - The horizontal path goes `running -> branching -> merging -> running` by queueing a merge frame and immediately returning the state to `running` (`agintor/runner.py:972-1011`), rather than treating merge as a terminal phase transition into completion.
   - When stop policy exits with unresolved work, the code records `run_cancelled` but still transitions through `completing` and then ends the run in `completed` state (`agintor/runner.py:422-447`).
   - The runtime never actually adopts `cancelled` as a top-level `execution_state`, even though the WS2 contract declares it as a first-class terminal state.

### Why this blocks WS3

Workstream 3 is supposed to persist checkpoint lineage, structured runtime events, and recovery diagnostics without redefining solve-time meaning. That depends on WS2 already freezing a truthful runtime state machine and stable event vocabulary.

Right now the implementation still mixes:

- canonical trace rows
- branch publication payloads
- implicit phase changes
- event names that do not match the published WS2 contract

If WS3 moves forward on that basis, it will have to choose which one is authoritative. That would make WS3 the workstream that defines solve-time semantics, which is outside its role.

### What needs to happen before WS3

Normalize the runtime state machine and event vocabulary to the fixed WS2 contract before downstream persistence begins.

Minimum required corrections:

- emit the promised stable event names directly into the canonical runtime trace
- eliminate one-off aliases like `agent_start` in favor of the frozen event vocabulary
- emit `merge_started` before merge work begins
- emit `plan_validation_failed` on plan validation failures
- ensure `run_cancelled` corresponds to an actual `cancelled` terminal execution state
- stop using `running` as an intermediate fallback state after entering `merging`
- make one event/state sequence authoritative enough that downstream persistence can store it without interpretation

## What looks solid

These fixes appear to be in place and working:

- bundled runtime includes `run_store.py`
- durable `run_root` values are canonicalized to absolute paths
- batch resume accepts `runtime_task_invocation` runs instead of rejecting them as solve-only
- resumed benchmark and prompt-mode requests preserve request ID overrides
- grouped batch lifecycle no longer derives solely from `runs[-1]`
- prompt-mode provider preflight is based on compiled execution semantics, not raw `context_items`
- execution-scoped provider usage is tracked across branch clones and batch totals
- branch cancellation now performs real cleanup for the side-effect kinds the runtime currently emits

The new tests cover those areas reasonably well, and the suite is green.

## Non-blocking cleanup and line-count reduction opportunities

These are worth doing, but they are not the reasons I would stop WS3.

### 1. Remove or implement dead event surfaces

- `run_root/events/` is currently created but unused.
- Either persist the canonical event stream there or stop creating it.

### 2. Remove or implement dead checkpoint surfaces

- `agintor/shell.py:177-196` still has the older summary-style `save_checkpoints()` path.
- Current WS2 behavior uses `CheckpointEnvelope` via `save_checkpoint_envelope()`.
- If the summary-only path is no longer part of the product contract, delete it.

### 3. Stop over-advertising unused plan-node surface area

- If `service_action`, `repo_patch`, `checkpoint`, `merge`, and `verify` are not yet executable plan nodes, they should not remain in the declared v1 surface.
- Right now this is both line-count overhead and contract risk.

### 4. Narrow the broad exception handling in prompt-mode preflight

- `agintor/runtime_api.py:1074-1083` catches all exceptions and returns `False`.
- That is acceptable as a short-term conservative choice, but it can hide real compiler bugs.
- Catch only the expected typed compilation failures if you keep this helper.

### 5. Revisit `BranchPublication.accepted`

- The field exists in the schema, but current publication creation always sets `accepted=True`.
- If there is no real acceptance/rejection phase, the field is dead complexity.
- If there is supposed to be one, implement it explicitly instead of carrying a permanently-true flag.

### 6. Centralize node execution dispatch

- The runner currently uses an `if/elif/else` chain and a permissive fallback.
- Replace that with an explicit dispatch table and a hard error for unsupported node kinds.
- That will both reduce line count and remove a dangerous class of silent semantic failures.

## Recommended order before starting WS3

1. **Fix the `ExecutionPlan` contract first**
   - either implement the promised node kinds and plan-owned boundaries
   - or reduce the contract to what is truly implemented

2. **Finish the runtime event model**
   - make one canonical structured event stream
   - ensure branch lifecycle and reconciliation events are durably present

3. **Normalize the runtime state machine and event vocabulary**
   - state transitions and event names must match the fixed WS2 contract closely enough for WS3 to persist them without interpretation

4. **Then clean up the dead surfaces**
   - unused `events/`
   - unused summary checkpoints
   - permanently-true `accepted`
   - broad exception swallowing

## Bottom line

WS2 is much closer than it was, and the recent fixes solved several real defects. But I would still stop before WS3 for these reasons:

- the plan contract is still partially aspirational
- the runtime event model is still partially implicit
- the runtime state-machine contract is still not fully frozen in code

Those are exactly the sorts of semantics WS3 would otherwise have to guess, persist, or paper over. WS2 should freeze them first.
