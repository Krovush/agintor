# WS2 Final Final Fix Plan

## Purpose

This document consolidates and reconciles the six WS2 blocker plans:

- `WS2_FIX_PLAN_01_FRONTIER_ORDER.md`
- `WS2_FIX_PLAN_02_BACKEND_DISPATCH.md`
- `WS2_FIX_PLAN_03_SINGLE_OUTPUT_VERIFY.md`
- `WS2_FIX_PLAN_04_BRANCH_CANCELLATION.md`
- `WS2_FIX_PLAN_05_RESUME_SIDE_EFFECT_RESTORE.md`
- `WS2_FIX_PLAN_06_DOCKER_FAIL_CLOSED.md`

The goal is one sequential implementation plan that:

- keeps the fixes compatible with each other
- fixes the actual WS2 contract violations
- avoids unnecessary schema churn, large file rewrites, or speculative architecture work

## Overall Assessment

All six plans are directionally correct. None of them needs to be discarded. The main adjustment is scope control.

The right implementation strategy is:

- keep `runner.py` fixes local to scheduler and restore behavior
- keep `runtime_host.py` changes centered on explicit backend selection, not a host redesign
- keep `container_runtime.py` changes centered on strict pre-launch contract resolution, not a transport rewrite
- share only the helpers that remove duplicated semantics
- avoid ABI, storage-schema, or execution-plan schema changes unless implementation proves one is strictly required

## Global Constraints

These constraints apply to all six fixes.

- Do not bump `runtime_abi` or `storage_schema_version`.
- Do not redesign `ExecutionPlan`, `BranchPlan`, `BranchState`, `CheckpointEnvelope`, or verifier schemas.
- Do not refactor plan compilation unless a specific fix strictly requires it. None of these six fixes does.
- Do not loosen verifiers to compensate for runner bugs.
- Do not introduce a second host/runtime transport path.
- Do not make branch-state restoration broader than root-owned state for the resume fix.

## Cross-Fix Compatibility Rules

### 1. Backend selection and Docker policy must agree

The backend selected for an execution unit must be the same backend used for:

- inspect
- runtime guarantee validation
- transport dispatch
- `AGINTOR_RUNTIME_BACKEND`
- run manifest persistence

The Docker fail-closed fix must land before request-selected backend dispatch is fully honored, otherwise backend dispatch will correctly route more requests to Docker while Docker still has a contract-read fail-open path.

### 2. Terminal artifact shaping must become canonical before resume restoration uses it

The single-output verify fix should establish one canonical runner helper for output-key-based artifact shaping. The resume restoration fix should reuse the same artifact/output semantics where applicable instead of inventing a second output interpretation path.

### 3. Branch cancellation must stay inside the current cleanup model

The sibling-cancellation fix must not redesign branch cleanup. It should only change when cancellation is triggered. The existing `_cancelled_branch_result()` and persisted branch accounting remain the terminal cleanup mechanism.

### 4. Resume restoration must preserve branch isolation

The resume restoration fix must not project branch-owned receipts directly into parent artifacts. Root-owned completed work can be restored from receipts. Branch-local state remains governed by branch publication and merge semantics.

## Optimality Decisions By Issue

### Issue 06: Docker isolation fail-closed

The source plan is correct. The only adjustment is to keep the shared logic minimal:

- add one strict runtime-loader-owned helper for loading/resolving the deployment contract for Docker launch policy
- replace `_requires_network_none()` with a strict pre-launch resolver
- do not broaden this into a larger container hardening pass

### Issue 02: Request-selected backend dispatch

The source plan is correct. The main scope trim is:

- use a small set of backend normalization helpers
- allow `inspect()` to accept an optional requested backend
- add lazy Docker executor acquisition
- thread explicit backend into local env/process launch

Avoid a broader `RuntimeHost` redesign or any new user-facing batch API unless implementation proves it is needed. It is not needed for WS2.

### Issue 01: Frontier order before branch fanout

The source plan is already near-optimal:

- fix `_active_runnable_frontier()` locally
- derive implicit grouped frontier selection from the first runnable node only
- add focused regression coverage

Do not touch plan validation or branch-group compilation here.

### Issue 03: Single-output verify artifact shape

The source plan is correct and should remain narrow:

- add one canonical runner helper for artifact shaping by output keys
- use it in `_execute_verify_node()` and the other terminal verification callsites
- keep verifiers unchanged

Do not encode extra artifact-shape schema unless implementation proves it is required. It is not required for WS2.

### Issue 04: Ordinary branch failure must cancel siblings

The source plan is correct. The scope trim is:

- replace the current `FIRST_EXCEPTION`-only logic with result-aware draining
- react to terminal failed `BranchResult` as well as raised `ResumeRecoveryError`
- reuse existing cleanup/accounting paths

Do not add a new branch state machine or new cleanup subsystem.

### Issue 05: Resume-side-effect restoration

The source plan is correct. The only required guardrail is:

- restrict automatic receipt-to-node restoration to root-owned receipts
- restore completed node state after receipt reconciliation and before resumed execution
- reuse shared output decoding/shaping helpers instead of duplicating parsing logic

Do not attempt full branch-local node restoration in this pass.

## Sequential Implementation Order

## Step 1: Docker Launch Policy Must Fail Closed

### Why first

This removes the unsafe fail-open path before backend dispatch starts honoring Docker selections more consistently.

### Files

- `agintor/runtime_loader.py`
- `agintor/container_runtime.py`
- `tests/test_container_runtime.py`

### Changes

- Add one shared strict deployment-contract/isolation-policy resolver in `runtime_loader.py`.
- Replace `DockerRuntimeExecutor._requires_network_none()` with a strict launch-policy helper.
- Make `inspect`, `run_batch_protocol`, `solve_protocol`, and `resume_protocol` resolve Docker launch policy before building argv.
- Raise a contract/preflight error if the deployment contract is missing, unreadable, corrupt, or schema-invalid.

### Scope control

- Keep `_docker_run_argv()` pure.
- Do not redesign container mounts, response rewriting, or durable run rewriting here.

### Tests

- helper-level tests for valid restricted and unrestricted contracts
- failure-path tests for missing/corrupt/schema-invalid contracts
- one entrypoint-level test proving no `subprocess.run` occurs on contract failure

## Step 2: RuntimeHost Must Dispatch on the Effective Backend

### Why second

Once Docker policy is fail-closed, the host can safely honor per-request backend selection without silently weakening isolation.

### Files

- `agintor/runtime_host.py`
- `tests/test_runtime_host.py`

### Changes

- Introduce small backend normalization helpers.
- Change `inspect()` to accept an optional requested backend and dispatch on that backend.
- Add lazy Docker executor acquisition so a local-default host can still honor Docker requests.
- In `solve()`, inspect, preflight, manifest persistence, transport selection, and local env setup must all use the selected backend.
- In `resume()`, resolve the runtime resume request before backend-sensitive inspect.
- In `run_batch()`, normalize one batch backend, reject mixed backends early, and dispatch on the effective backend.
- Thread explicit backend into `_runtime_env()` and all local launch helpers.

### Scope control

- Do not add a new public batch override surface.
- Do not change runtime entry contracts.

### Tests

- solve backend override on local-default host
- solve backend override on docker-default host
- resume backend derived before inspect
- batch dispatch honors effective backend
- batch rejects mixed invocation backends

## Step 3: Fix Runnable Frontier Selection Order

### Why third

This is the smallest isolated runner fix and does not depend on the later runner changes.

### Files

- `agintor/runner.py`
- `tests/test_runtime_execution.py`

### Changes

- Update `_active_runnable_frontier()` so implicit grouped frontier selection is based on `runnable[0]`.
- Preserve the explicit `branch_group_id` override path.
- Add a short comment/docstring clarifying that the active frontier is the leading runnable unit in deterministic order.

### Scope control

- No compiler, schema, or validation changes.

### Tests

- mixed frontier returns earlier singleton first
- grouped frontier becomes active after the singleton completes
- explicit override still returns the requested group
- one integration test showing branch fanout occurs only after earlier singleton work

## Step 4: Canonicalize Terminal Verification Artifact Shape

### Why fourth

This establishes the canonical artifact-shaping helper that the resume restoration fix can reuse for consistent semantics.

### Files

- `agintor/runner.py`
- `tests/test_runtime_execution.py`

### Changes

- Add one runner helper for shaping artifacts from a set of output keys:
  - one key -> raw value
  - multiple keys -> keyed mapping
- Update `_execute_verify_node()` to use that helper.
- Update the other terminal verification callsites in `runner.py` to use the same helper.
- Extract direct-response output decoding into a shared helper rather than keeping it inline in `_execute_direct_response()`.

### Scope control

- Keep verifiers unchanged.
- Keep verify-node result payload unchanged.
- No ABI/schema changes.

### Tests

- single-output `number_exact`
- single-output `string_exact`
- multi-output regression guard
- optional helper-level unit coverage

## Step 5: Cancel Siblings on Ordinary Branch Failure

### Why fifth

This is the next runner-level behavioral fix. It should land after the smaller scheduling and verification-shape fixes, but before resume restoration because it changes branch terminal-state behavior and trace semantics.

### Files

- `agintor/runner.py`
- `tests/test_runtime_execution.py`

### Changes

- Replace the current `wait(..., FIRST_EXCEPTION)`-only decision model with result-aware future draining.
- Trigger sibling cancellation when the first completed future either:
  - raises `ResumeRecoveryError`, or
  - returns `BranchResult` with `branch_state.status == "failed"`
- Map failed-branch kinds to cancellation reasons with one small helper.
- Set `cancellation_event` immediately on first non-recoverable failure.
- Cancel not-yet-started siblings when possible and let already-started siblings exit through the existing cooperative cancellation path.
- Preserve final fail-closed parent behavior after all futures are drained.

### Scope control

- Keep `_cancelled_branch_result()` as the cleanup mechanism.
- Keep final parent accounting after future draining.
- Do not redesign branch publication or budget accounting.

### Tests

- failed branch result cancels a sibling that is still running
- failed branch kind maps to correct sibling cancellation reason
- not-yet-started sibling is cancelled without producing completed output
- exceptional `ResumeRecoveryError` path still works

## Step 6: Restore Completed Root-Owned Nodes From Terminal Receipts on Resume

### Why last

This depends on the shared output decoding/shaping semantics established earlier and should be implemented once the surrounding runner semantics are stabilized.

### Files

- `agintor/runner.py`
- `agintor/runtime_api.py`
- `tests/test_runtime_execution.py`

### Changes

- After `_reconcile_side_effect_receipts()` in `_restore_from_checkpoint()`, add a dedicated restoration pass that projects terminal reconciled receipts back into completed node state.
- Restore only root-owned nodes:
  - no `branch_id`
- For restored nodes, update:
  - `plan_node_status[node_id] = "completed"`
  - `artifacts[node.output_key]`
  - `unresolved_goals`
  - missing short-term artifact node if the checkpoint boundary occurred before artifact materialization
- Reuse shared direct-response decode logic and canonical output semantics.
- Let `_execute_operations()` skip these nodes through the existing completed-node fast path.

### Scope control

- Do not restore branch-local node outputs into parent artifacts.
- Do not widen this into a full branch-state restoration redesign.
- No schema/ABI changes unless implementation proves a missing field. That is not expected.

### Tests

- resume from `after_provider_completion` restores node as already completed
- resume from `after_tool_completion` restores node as already completed
- reconciled launch receipt can restore completed root-owned node state
- unresolved receipt still yields strict failure or best-effort `recovery_blocked`
- branch-owned receipt is not projected into parent artifacts

## Shared Helper Policy

Only add helpers where they remove duplicated semantics that already caused bugs.

Helpers that are justified:

- backend normalization / effective backend selection in `runtime_host.py`
- Docker launch-policy resolution in `runtime_loader.py` or one shared loader-owned path
- artifact shaping by output keys in `runner.py`
- direct-response output decoding shared by execution and resume restoration
- failed-branch-kind to cancellation-reason mapping

Helpers that are not justified in this pass:

- new schema-layer abstractions for frontier selection
- new verifier schema fields
- new branch cleanup subsystem
- new checkpoint schema version line

## Validation Sequence

Run validation after each step, not only at the end.

### After Step 1

- `pytest tests/test_container_runtime.py -q`

### After Step 2

- `pytest tests/test_runtime_host.py -q`
- targeted Docker/local override tests

### After Steps 3 and 4

- targeted `tests/test_runtime_execution.py` slices for frontier ordering and exact verification

### After Step 5

- targeted horizontal branch tests in `tests/test_runtime_execution.py`

### After Step 6

- `pytest tests/test_runtime_execution.py -k "resume or checkpoint" -q`

### Final pass

- `pytest tests/test_runtime_execution.py tests/test_runtime_host.py tests/test_container_runtime.py -q`

## Definition of Done

WS2 is ready to move on when all of the following are true:

- Docker launch policy fails closed on contract-read/parse/validation errors.
- `RuntimeHost` honors the effective backend end to end for inspect, solve, batch, and resume.
- Mixed runnable frontiers respect declaration order before branch fanout.
- Single-output exact verification uses the raw scalar/string artifact.
- Ordinary failed branch results cancel siblings promptly through the existing cleanup model.
- Resume reconstructs completed root-owned node state from terminal receipts instead of re-entering those nodes.
- No ABI/storage schema bump was required.
- The targeted runtime/host/container test suites pass together.

## Final Recommendation

Implement these fixes exactly in the order above.

That order gives the cleanest dependency chain:

- secure Docker policy first
- then correct backend dispatch
- then fix the runner's local scheduling and verification semantics
- then fix branch concurrency behavior
- then fix receipt-backed resume reconstruction on top of the canonicalized runner semantics

This sequence fixes the six WS2 blockers without turning them into a broader rewrite of the runtime host, runner, or container transport.
