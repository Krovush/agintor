# WS2 Resume/Run-Root Reconstruction Plan

## Summary

Rebuild solve/resume around a **durable run root** and an **exact runtime snapshot** contract. The implementation target is:

- every solve or batch invocation creates one durable run folder,
- every resume continues that same run lineage instead of creating a throwaway temp workspace,
- resuming must restore the same solve-time state that uninterrupted execution would have seen,
- successful and paused runs are never auto-deleted,
- failed runs are auto-pruned only when they are proven non-resumable,
- no provider/tool side effect is ever blindly reissued on resume.

This plan fixes all 10 non-overlapping issues by making the run folder authoritative, the checkpoint envelope complete, and the host/runtime protocol explicit about run identity and restartability.

Recommended durable layout per user-visible run:

```text
workspace/runs/<run_id>/
  run_manifest.json
  request/
    request.json
    plan.json
    task.json
    runtime_identity.json
  attempts/
    attempt_0001/
    attempt_0002/
  checkpoints/
    index.json
    LATEST.json
    checkpoint.<seq>.json
  traces/
  events/
  artifacts/
  side_effects/
```

Primary implementation surfaces:
`[runtime_host.py](<C:/Users/yaros/Desktop/Agintor MVP/agintor/runtime_host.py>)`, `[runtime_sdk/runtime_entry.py](<C:/Users/yaros/Desktop/Agintor MVP/agintor/runtime_sdk/runtime_entry.py>)`, `[runner.py](<C:/Users/yaros/Desktop/Agintor MVP/agintor/runner.py>)`, `[schemas.py](<C:/Users/yaros/Desktop/Agintor MVP/agintor/schemas.py>)`, `[runtime_api.py](<C:/Users/yaros/Desktop/Agintor MVP/agintor/runtime_api.py>)`, `[shell.py](<C:/Users/yaros/Desktop/Agintor MVP/agintor/shell.py>)`, `[tests/test_runtime_host.py](<C:/Users/yaros/Desktop/Agintor MVP/tests/test_runtime_host.py>)`, `[tests/test_runtime_execution.py](<C:/Users/yaros/Desktop/Agintor MVP/tests/test_runtime_execution.py>)`.

## Public Interface and Contract Changes

- Introduce a runtime-owned **run identity** separate from `request_id`.
  - Add `run_id`, `run_root`, `attempt_id`, and `latest_checkpoint_ref` to the runtime result surface (`SolveResult` and `RunResult`).
  - Keep `request_id` as task/request provenance only; it is not the primary resume lookup key anymore.

- Replace request-id-only resume with **run-root resume**.
  - `ResumeRequest` and `RuntimeResumeRequest` should accept:
    - `checkpoint_ref` for exact checkpoint resume,
    - `run_ref` for “resume latest checkpoint in this durable run,”
    - `request_id` only as metadata/audit context.
  - Remove global “scan the whole workspace for newest matching request_id” behavior.
  - If backward-compatible request-id lookup is temporarily kept, it must be restricted to the selected runtime hash and must fail on ambiguity instead of auto-picking.

- Add a typed **run manifest** contract in `schemas.py`.
  - `RunManifest` should include run identity, runtime identity, request metadata, lifecycle status, current attempt, latest checkpoint, resumability flag, created/updated timestamps, and prune eligibility.
  - `AttemptManifest` should include process-start metadata for each launch/resume attempt, including whether it exited as completed, paused, failed, or crashed.

- Split checkpoint payload into typed snapshot sections instead of free-form summaries.
  - Keep `CheckpointEnvelope`, but replace “summary-only restore” with typed sections such as:
    - `runtime_state_snapshot`
    - `shell_state_snapshot`
    - `side_effect_ledger`
    - `attempt_snapshot`
  - Keep human-readable summaries only as diagnostic metadata; they must not be the source of truth for restore.

- Add a separate **run lifecycle** status in the run manifest.
  - Allowed manifest lifecycle: `running`, `paused`, `completed`, `failed`, `pruned`.
  - This lifecycle is separate from `SolveResult.status`; it exists to make retention and resume rules explicit without overloading benchmark/user-result semantics.

## Implementation Changes

- **Run-root ownership and host transport** — `[runtime_host.py](<C:/Users/yaros/Desktop/Agintor MVP/agintor/runtime_host.py>)`, `[runtime_sdk/runtime_entry.py](<C:/Users/yaros/Desktop/Agintor MVP/agintor/runtime_sdk/runtime_entry.py>)`
  - Stop using temp per-call folders like `solve_<hash>` and `resume_<hash>` as the authoritative execution store.
  - Create one durable run root at solve start, persist the initial request/plan/runtime identity there, and pass that run root into the runtime entrypoint.
  - On resume, always target an existing run root or exact checkpoint inside it.
  - Successful and paused runs are never pruned automatically.
  - Failed runs are pruned only if `RunManifest.resumable == false` and no valid checkpoint remains.
  - Solve and resume must share one preflight path for isolation guarantees and runtime credential requirements; `resume()` must call the same runtime-provider credential preflight that `solve()` already performs.

- **Dedicated run-store helper** — add `agintor/run_store.py`
  - Own run-root creation, manifest I/O, checkpoint indexing, attempt numbering, latest-checkpoint resolution, and pruning.
  - Remove workspace-wide checkpoint discovery from `RuntimeHost`; all such logic moves into this helper and is scoped by `run_id`/`run_root`.
  - `FixedShell.save_checkpoint_envelope()` should write into the run root’s `checkpoints/` directory, not a request-id bucket detached from the run lineage.

- **Complete runtime snapshot contract** — `[runner.py](<C:/Users/yaros/Desktop/Agintor MVP/agintor/runner.py>)`, `[shell.py](<C:/Users/yaros/Desktop/Agintor MVP/agintor/shell.py>)`, `[schemas.py](<C:/Users/yaros/Desktop/Agintor MVP/agintor/schemas.py>)`
  - Snapshot all solve-time state that can influence future decisions:
    - full `RuntimeState`, not just selected counters,
    - queued frames and active frame,
    - `visible_tool_names`,
    - `plan_node_status`,
    - artifacts,
    - unresolved goals,
    - branch state and accepted publications,
    - message board contents and cursors,
    - open handles,
    - full short-term memory graph,
    - full long-term memory graph for the active evaluation unit,
    - task-local tool registry contents and metadata,
    - current task/memory scope markers,
    - budget totals and verifier state.
  - Add explicit shell snapshot/restore helpers rather than rebuilding these pieces ad hoc inside `_restore_from_checkpoint()`.
  - Keep `working_state_summary` only as human-readable diagnostics; stop using it as the restore source for mutable state.

- **Exact side-effect ledger semantics** — `[runner.py](<C:/Users/yaros/Desktop/Agintor MVP/agintor/runner.py>)`, `[runtime_api.py](<C:/Users/yaros/Desktop/Agintor MVP/agintor/runtime_api.py>)`
  - Treat both `completed` and `reconciled` receipts as terminal reusable receipts in:
    - `PolicyContext.run_model_request()`
    - `_execute_tool_operation()`
    - `_reconcile_side_effect_receipts()`
  - In exact-snapshot mode, resume may do only three things for a previously launched side effect:
    - reuse a terminal receipt,
    - reconcile the in-flight launch via handle/job/provider status,
    - stop and mark the run as paused/failed-resumable if the state cannot be proven.
  - Resume must never blindly reissue a provider/tool request if the ledger says that request was already launched.
  - Persist side-effect records inside the run root under `side_effects/` in addition to checkpoint copies so reconciliation is not dependent on a single later checkpoint surviving.

- **Branch persistence and cancellation** — `[runner.py](<C:/Users/yaros/Desktop/Agintor MVP/agintor/runner.py>)`, `[runtime_api.py](<C:/Users/yaros/Desktop/Agintor MVP/agintor/runtime_api.py>)`
  - Branch `PolicyContext` must inherit branch-aware checkpoint and side-effect callbacks so branch-side provider/tool launches are persisted immediately, not only after `_run_branch_plan()` returns.
  - Persist branch publications and receipts at these boundaries:
    - after branch-plan validation,
    - after each node completion,
    - after tool/provider launch,
    - after tool/provider completion,
    - after verifier completion,
    - after branch cancellation cleanup,
    - at terminal branch completion.
  - On fatal sibling fault, set cancellation before collecting remaining branch futures and disallow further normal publications from cancelled branches.
  - Running branches may publish only cleanup/reconciliation records after cancellation.
  - Add `context.raise_if_cancelled()` before every irreversible action boundary and after every node boundary inside branch execution, not just at branch start/end.

- **Frontier-only horizontal branching** — `[runner.py](<C:/Users/yaros/Desktop/Agintor MVP/agintor/runner.py>)`, `[templates/baseline_runtime/topology_policy.py](<C:/Users/yaros/Desktop/Agintor MVP/agintor/templates/baseline_runtime/topology_policy.py>)`
  - Compute the active runnable frontier for the current `branch_group_id` and pass only those runnable nodes to `select_workers()`.
  - Never pass the full execution plan into worker selection in horizontal mode.
  - Branch execution must not include downstream dependent nodes until their dependencies are satisfied in the parent plan state.

- **Compiler/runtime argument separation** — `[runtime_api.py](<C:/Users/yaros/Desktop/Agintor MVP/agintor/runtime_api.py>)`, `[runner.py](<C:/Users/yaros/Desktop/Agintor MVP/agintor/runner.py>)`
  - Stop copying `OperationSpec.expression` into `PlanNode.static_args`.
  - Preserve expression text only in a dedicated field or metadata used by generated-expression synthesis.
  - `_resolve_plan_node_args()` must construct only actual runtime tool arguments; compiler metadata is not forwarded to reusable tools.
  - `_generated_expression_spec()` should read the dedicated expression field/metadata directly.

- **Batch transfer ordering** — `[runtime_sdk/runtime_entry.py](<C:/Users/yaros/Desktop/Agintor MVP/agintor/runtime_sdk/runtime_entry.py>)`, `[runtime_api.py](<C:/Users/yaros/Desktop/Agintor MVP/agintor/runtime_api.py>)`
  - Keep sharing a `TaskRuntime` only for grouped transfer-scored episode keys.
  - Before executing invocations inside a shared episode group, sort them by `(episode_order, task_id, request_id)` so long-term memory evolves in the declared episode sequence rather than transport order.

## Test Plan

- Add host-level tests in `[tests/test_runtime_host.py](<C:/Users/yaros/Desktop/Agintor MVP/tests/test_runtime_host.py>)`
  - solve creates a durable run root and returns `run_id`, `run_root`, and `latest_checkpoint_ref`.
  - resume requires `run_ref` or `checkpoint_ref` and does not rely on global request-id scanning.
  - successful runs remain resumable and are not pruned.
  - resume runs the same runtime credential preflight as solve.
  - failed runs are pruned only when manifest says non-resumable and no valid checkpoint exists.

- Add execution/resume tests in `[tests/test_runtime_execution.py](<C:/Users/yaros/Desktop/Agintor MVP/tests/test_runtime_execution.py>)`
  - completed and reconciled provider receipts are both reused without new provider calls.
  - completed and reconciled tool receipts are both reused without rerunning the tool.
  - launched-without-completion receipts reconcile without blind reissue.
  - branch-side provider/tool launches are checkpointed before branch completion.
  - exact snapshot restore reproduces visible tools, task-local tools, long-term memory, short-term graph, message board, open handles, branch state, and budget totals.
  - horizontal mode passes only the active frontier to `select_workers`.
  - a dependent node is never offered to horizontal workers before its prerequisites complete.
  - fatal sibling fault prevents any new side effects from sibling branches after cancellation is set.
  - cancelled branches can publish cleanup/reconciliation records but not winning artifacts.
  - batch execution reuses a runtime across transfer episodes only after sorting by `episode_order`.

- Add new focused snapshot tests if needed
  - `tests/test_run_store.py` for run-root creation, attempt numbering, checkpoint indexing, and prune rules.
  - `tests/test_resume_snapshot.py` for full checkpoint round-trip and run-manifest lifecycle transitions.

## Assumptions and Defaults Locked In

- **Exact snapshot contract**: resume must restore the same effective runtime state an uninterrupted run would have had; no best-effort recomputation path is acceptable for normal resume.
- **One durable run root**: each user-visible run has one durable root folder; each process start/resume becomes a new attempt inside that root.
- **Retention**: completed and paused runs are retained until user deletion; failed runs are auto-pruned only when they have no valid resume point.
- **No backward-compat preservation**: request-id-only resume may be removed or reduced to a strict, non-default fallback; the new primary contract is `run_ref` or `checkpoint_ref`.
- **No blind side-effect replay**: if a prior provider/tool action was already launched, resume may reconcile it or stop; it may not silently issue that action again.
