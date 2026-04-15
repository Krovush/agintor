# WS2 Closeout Plan: Freeze a Truthful Runtime Contract Before WS3

## Summary
Close WS2 by **tightening and unifying** the runtime contract, not by expanding it further. The current code already has the right major building blocks, but the contract is split across plan schemas, runner control flow, branch publications, and durable run manifests. WS3 should not begin until those surfaces mean one thing everywhere.

This plan intentionally folds together:
- the three blockers from `WS2_GAPS_BLOCKING_WS3.md`
- the remaining contract-affecting gaps from the broader WS2 review that the blocker memo does not cover
- the earlier durability/accounting fixes from `WS2 Durable Runtime Execution Correction.md`

The guiding rule for this closeout is: **one source of truth per concern**.
- One authoritative execution-plan surface
- One canonical runtime event stream
- One centralized execution-unit finalization path
- One explicit evaluation-unit identity model
- One explicit selected-backend and provider-usage model

## Key Decisions
- **Do not broaden the `ExecutionPlan` surface further. Shrink it to a truthful v1.**
  - Keep these executable node kinds only: `builtin_op`, `memory_lookup`, `tool_call`, `tool_synthesis`, `direct_response`, `merge`, `verify`.
  - Remove `checkpoint`, `service_action`, and `repo_patch` from the v1 `PlanNode.node_kind` enum.
  - Represent bounded repo patch and bounded service actions as `tool_call` nodes with explicit metadata and tool/category constraints.
  - Treat checkpoint publication as runtime state-machine behavior, not as a plan node.
- **Make `merge` and `verify` explicit plan nodes.**
  - `merge` becomes the explicit convergence point for any branchable group.
  - `verify` becomes the explicit terminal verification step when verification is required or exact verification exists for an externally visible artifact.
- **Introduce one internal node descriptor registry and drive all plan behavior from it.**
  - The registry owns: executor mapping, validation rules, branch eligibility, provider requirement, and whether the node is value-producing.
  - Plan compilation, plan validation, runtime preflight, and runner dispatch must all read this registry.
  - Remove all permissive `else: output = resolved_args` behavior. Unsupported node kinds must fail validation, never degrade silently.
- **Promote runtime events to a first-class durable contract.**
  - `events/` becomes the canonical event store for a run.
  - `traces/*.json` remains a compatibility/derived view, not the source of truth.
  - Branch lifecycle is expressed through canonical `RuntimeEvent`s, not only through `BranchPublication`s.
- **Separate branch lifecycle from branch merge data.**
  - `BranchPublication` remains only for parent-consumable branch outputs, receipts, and reconciliation payloads.
  - `branch_started`, `branch_completed`, `branch_cancelled`, `branch_failed`, `merge_started`, `side_effect_recorded`, `side_effect_reconciled`, and `plan_validation_failed` are canonical runtime events.
- **Make evaluation-unit identity explicit.**
  - Add `evaluation_unit_id` to `RuntimeTaskInvocation`.
  - Add `episode_step_index` for transfer-scored episodes.
  - Non-transfer invocations always get unique `evaluation_unit_id`s, even when they share the same task and seed.
  - Transfer-scored invocations share one `evaluation_unit_id` and are ordered by `episode_step_index`.
- **Keep `request_id` unique per invocation.**
  - Single benchmark solve keeps `benchmark.<task_id>.seed_<seed>`.
  - Batch duplicates of the same `(task_id, seed)` that are not transfer-scored must receive deterministic suffixes such as `.dup_<ordinal>`.
  - Do not use `request_id` as the durable grouping key.
- **Centralize solve, batch, and resume finalization behind one execution-unit finalizer.**
  - Final lifecycle state, checkpoint refs, prune behavior, returned artifact refs, and resumability must all be derived by the same code path.
  - Host-side launch failures, runtime-side shaped failures, resume failures, and batch-launch failures all go through this same finalizer.
- **Stamp and persist the selected backend, not the first advertised backend.**
  - `PolicyContext.runtime_backend`, receipts, checkpoints, and events must reflect the actual backend used for the run.
- **Fix replay-backed branch concurrency correctly.**
  - Branch clones must not get copied replay cursors.
  - Implement a shared replay cursor/coordinator for replay-backed concurrent runs so the recorded transcript is consumed globally in order.
  - Do not ship with silent cursor cloning.

## Implementation Changes

### 1. Freeze a new explicit WS2 version line
- Bump to:
  - `runtime_abi = "agintor-runtime-abi-v5"`
  - `storage_schema_version = "agintor-storage-v3"`
- Treat this as an intentional contract break.
- Do not build compatibility shims for older WS2 artifacts.
- Any old runtime, checkpoint, or run-store artifact should fail fast with a clear expected-versus-actual version error.

### 2. Make `ExecutionPlan` authoritative and truthful
- Update `PlanNode.node_kind` and validation to the reduced v1 surface.
- Add a single internal node descriptor registry, likely in the runtime-contract layer, that contains for each node kind:
  - executor function name
  - whether the node is value-producing
  - whether the node can be branched
  - whether the node requires default provider credentials
  - whether the node is allowed in prompt-mode local-only solves
  - validation hooks for metadata and bindings
- Update plan compilation so:
  - benchmark operations compile only to supported node kinds
  - user-request templates never emit removed node kinds
  - any bounded repo patch or service-style action compiles to `tool_call` plus explicit metadata/tool hint
  - any branchable group emits one explicit `merge` node depending on all group members
  - any verification-required terminal path emits one explicit `verify` node depending on the final producer node
- Update runner dispatch so it is table-driven from the node registry.
- Add explicit executors:
  - `_execute_merge_node`
  - `_execute_verify_node`
- Keep current executors for:
  - memory lookup
  - tool-backed operations
  - direct response
- Remove all fallback execution paths for unknown node kinds.
- Update validation rules so:
  - removed node kinds fail validation
  - every `merge` node references exactly one declared branch group or one explicit set of upstream branch members
  - `verify` nodes consume explicit artifact-producing dependencies
  - `terminal_output_keys` may be produced by work or merge nodes, never by verify nodes
  - prompt-mode local-only checks are driven from node descriptors, not a hard-coded allowlist set

### 3. Normalize the runtime state machine and canonical event vocabulary
- Add a typed `RuntimeEvent` schema with at least:
  - `event_id`
  - `sequence_no`
  - `created_at`
  - `execution_state`
  - `request_id`
  - `plan_id`
  - `trace_context`
  - optional `frame_id`, `branch_id`, `node_id`
  - event payload
- Persist canonical events append-only under `run_root/events/`.
- Update `PolicyContext.record()` so every runtime event is appended both:
  - to the in-memory trace list for compatibility
  - to the durable canonical event stream
- Make these event names mandatory and canonical:
  - `run_started`
  - `plan_compiled`
  - `plan_loaded`
  - `plan_validation_failed`
  - `node_started`
  - `node_completed`
  - `node_failed`
  - `branch_started`
  - `branch_completed`
  - `branch_cancelled`
  - `branch_failed`
  - `side_effect_recorded`
  - `side_effect_reconciled`
  - `checkpoint_published`
  - `checkpoint_restored`
  - `merge_started`
  - `merge_completed`
  - `terminal_emitted`
  - `run_failed`
  - `run_cancelled`
- Remove root-only aliases like `agent_start`; always emit `node_started` with `frame_role="root"` when the root frame begins.
- Emit `merge_started` before any merge work starts.
- Emit `plan_validation_failed` from the runtime-owned validation step, not as a host-only interpretation.
- Keep `run_cancelled` only for actual top-level cancellation. Do not use it for “stop policy ended with unresolved work.”
- Update the execution-state transition contract to the real runtime model:
  - `idle -> compiling -> validating -> running`
  - `running -> branching -> merging`
  - `merging -> running` when the merge unblocks more work
  - `merging -> completing` when the merge yields the terminal candidate artifact
  - `running -> completing` when non-branch execution reaches terminalization
  - `completing -> completed | failed | cancelled`
- Make `cancelled` a real top-level terminal execution state, not just an event label.

### 4. Centralize durable execution-unit lifecycle, checkpoints, and resume behavior
- Introduce one internal execution-unit finalizer used by:
  - `solve`
  - `run_batch`
  - `resume`
  - host-side exception paths
  - runtime-side shaped-failure paths
- The finalizer must:
  - recompute the latest usable checkpoint from the run root or run store
  - recompute whether the run is resumable from that checkpoint, not from a response flag alone
  - clear trace and event refs if the run is pruned
  - preserve paused state when a checkpoint exists, even if the most recent launch failed
  - preserve already-published checkpoints when a subprocess dies after publishing but before writing the final response
- Update runtime failure shaping so:
  - compile failures and validation failures return typed `RuntimeSolveResponse` failures instead of generic launch crashes
  - shaped failures preserve `latest_checkpoint_ref` when the runtime already published one
  - batch-launch exceptions finalize grouped runs instead of leaving them `running`
- Keep the current request bundle tree, but make it execution-unit aware:
  - `request/request.json` becomes the execution-unit envelope
  - it may represent a single solve or a grouped transfer-scored episode
  - it must include the member invocation list when the unit is grouped
- Resume resolution rules:
  - `checkpoint_ref` wins if supplied
  - otherwise resume uses the latest checkpoint for the execution unit
  - the selected checkpoint envelope is the authoritative source for the task and plan being resumed
  - stored request payloads are supplemental and used only to reconstruct original prompt-mode `SolveRequest`s where needed
- Add `evaluation_unit_id` to run manifests so grouped runs are durable as execution units rather than “whatever first invocation happened to write the request bundle.”

### 5. Fix grouped batch semantics and duplicate invocation handling
- Replace `batch_evaluation_unit_key()`-style grouping by `request_id` with explicit evaluation-unit grouping.
- Host-side batch request construction must assign:
  - unique per-invocation `request_id`
  - explicit `evaluation_unit_id`
  - `episode_step_index` for transfer-scored episodes
- Runtime-side batch execution must:
  - reuse one `TaskRuntime` only across invocations that share the same `evaluation_unit_id`
  - stop executing later members of a transfer-scored episode after the first member returns `failed` or `paused`
  - synthesize terminal `RunResult`s for skipped later members so response cardinality stays aligned with the original invocation list
- Grouped run reduction must use:
  - evaluation order, not last-result wins
  - first failing `failure_kind` in execution order
  - latest usable checkpoint across executed group members
  - terminal lifecycle state rules:
    - `completed` only if every executed member is terminal and non-failing
    - `paused` if a failure occurred and a usable checkpoint exists
    - `failed` if a failure occurred and no usable checkpoint exists
- Non-transfer duplicates of the same benchmark task and seed must remain independent durable runs.

### 6. Unify provider requirement detection, backend stamping, and replay concurrency
- Replace prompt-mode provider preflight heuristics with plan-derived capability checks from the node descriptor registry.
- `builtin_op` must be treated as local-only.
- `tool_call` is local-only unless the compiled node metadata explicitly marks it as provider-backed.
- `direct_response` and provider-backed `tool_synthesis` require hosted/default provider support.
- Remove any preflight logic that infers provider requirement from `context_items` or other raw request-shape heuristics.
- Thread the selected backend from host request construction into:
  - `RuntimeSolveRequest`
  - `RuntimeTaskInvocation`
  - `PolicyContext`
  - receipts
  - checkpoints
  - runtime events
- Replace any use of `supported_backends[0]` as a runtime execution fact.
- Fix Docker inspect and similar command builders by constructing argv declaratively rather than inserting positional flags into partially-built lists.
- Implement a replay concurrency coordinator so concurrent branches share one replay cursor with synchronized consumption order.
- Keep per-branch usage deltas, but source actual replay rows from the shared coordinator.

### 7. Keep execution-scoped usage accounting authoritative
- Keep `TaskRuntime` as the owner of per-execution provider-usage accounting.
- Every `RunResult` must carry only the usage delta for that invocation.
- Every `BranchResult` must carry only the usage delta for that branch.
- Batch responses must sum `run_results[*].provider_usage`, not inspect provider instances.
- Grouped transfer-scored runs must aggregate only over executed episode members.

### 8. Finish branch cancellation and reconciliation semantics under the canonical event model
- Keep branch cleanup fail-closed.
- Branch cancellation must:
  - cancel running async handles
  - reconcile or abandon outstanding side effects
  - emit canonical `branch_cancelled`, `side_effect_recorded`, and `side_effect_reconciled` events
- Post-cancellation branch publications may still carry cleanup/reconciliation payloads if the parent needs them for state reconstruction, but lifecycle meaning comes from the event stream.
- If cleanup cannot make every branch-owned handle or side effect terminal, fail the run rather than returning a clean cancellation.

## Test Plan
- Add targeted plan-contract tests:
  - removed node kinds are rejected during validation
  - prompt-mode repo patch and service templates compile to supported node kinds only
  - merge and verify nodes are emitted deterministically when required
  - unsupported node kinds never execute and never degrade to `resolved_args`
- Add runtime event tests:
  - horizontal runs persist `branch_started`, `branch_completed`, `merge_started`, `merge_completed`
  - validation failures persist `plan_validation_failed`
  - `run_cancelled` appears only when the top-level execution state is actually `cancelled`
  - `events/` is populated and matches the in-memory trace ordering
- Add execution-unit lifecycle tests:
  - solve crash after checkpoint publication preserves resumability
  - resume launch failure preserves paused state when a checkpoint exists
  - shaped runtime failures preserve `latest_checkpoint_ref`
  - pruned failed solves clear dead trace/event refs from returned results
  - batch launcher exceptions finalize grouped runs instead of leaving them `running`
- Add grouped batch tests:
  - transfer-scored step 1 fails and step 2 is not executed
  - skipped later episode members return synthetic blocked results in-order
  - duplicate non-transfer `(task_id, seed)` invocations remain separate execution units
  - grouped reduction uses first failure kind and latest checkpoint across members
- Add backend/provider tests:
  - local-only builtin-op prompt requests pass preflight without hosted credentials
  - selected backend is the backend recorded in receipts, checkpoints, and events
  - docker inspect with `network none` succeeds
  - replay-backed horizontal runs consume a recorded transcript in one global order without branch duplication
- Run the focused WS2 suite and full `pytest -q` before declaring WS2 complete.
- Add one manual probe before handoff:
  - run a horizontal benchmark solve and inspect `events/`
  - run a transfer-scored grouped batch with an induced step-1 failure and inspect durable run state plus resume behavior

## Assumptions and Defaults
- This closeout intentionally **reduces** over-claimed WS2 surface area instead of implementing more aspirational node kinds immediately before WS3.
- `checkpoint` is a runtime boundary, not a plan node.
- `repo_patch` and `service_action` are represented as constrained `tool_call` templates in v1.
- `events/` is the canonical durable runtime event stream; trace JSON is compatibility output.
- ABI and storage versions are bumped now and older WS2 artifacts are not supported.
- WS3 must not start until the new plan contract, event model, and execution-unit lifecycle semantics are all green under targeted tests and full-suite validation.
