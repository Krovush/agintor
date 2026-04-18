# Post-WS5 Debugging Ledger

## Deferred From WS2 Closure

### Paused run reduction currently collapses resumable runs into `completed`

- Area:
  - `agintor/runtime_api.py`
  - `reduce_grouped_run_results()`
  - `_run_result_is_non_failing_terminal()`
- Current behavior:
  - a `RunResult` with `run_lifecycle_state="paused"` and a valid checkpoint is still treated as a non-failing terminal run
  - grouped reduction can therefore report the evaluation unit as `completed` instead of `paused`
  - host-side finalization then records the unit as complete even though the runtime explicitly paused at a resumable boundary
- Why not fixed in this WS2 closure pass:
  - the dominant happy-path local solve flow still completes cleanly, so this does not block the common smoke path
  - but it is a real semantic mismatch at the host/runtime contract boundary, not just a cosmetic lifecycle label issue
- Follow-up target:
  - make grouped reduction treat `paused` as non-terminal unless/until the run actually reaches a completed terminal state
  - recheck host finalization and grouped episode reduction together so resumable failures do not get flattened into successful completion records

### External checkpoint resume should fall back from stale `run_root` to `run_id`

- Area:
  - `agintor/run_store.py`
  - `RunStore.resolve_resume_target()`
- Current behavior:
  - when resume is driven by an explicit `checkpoint_ref` outside the durable run directory, resolution prefers `CheckpointEnvelope.run_root` whenever that field is non-empty
  - if that embedded `run_root` is stale or container-only (for example `/mnt/runs/...`), resume fails even when `CheckpointEnvelope.run_id` still points to a valid local durable run manifest
- Why not fixed in this WS2 closure pass:
  - normal same-workspace checkpoint publication paths are already host-rewritten and continue to work
  - the failure shows up on externalized, copied, or container-origin checkpoints rather than the dominant local resume path
- Follow-up target:
  - make `resolve_resume_target()` fall back from `run_root` to `run_id` when the stored root cannot be resolved locally
  - keep this covered when WS3 introduces the durable indexed checkpoint store and cross-run recovery surfaces

### ReplayProvider branch-clone determinism under concurrent horizontal execution

- Area:
  - `agintor/providers.py`
  - `ReplayProvider.shared_clone()`
  - `clone_provider()`
- Current behavior:
  - horizontal branches that clone a `ReplayProvider` share one mutable coordinator cursor
  - replay row assignment is therefore scheduler-dependent under concurrent branches
- Why not fixed in this WS2 closure pass:
  - a correct fix needs branch/request-aware replay row routing or a revised replay recording format
  - simply giving each clone its own cursor snapshot would create a different correctness bug, where multiple branches can consume the same replay rows
  - this is a real determinism gap, but the durable solution is larger than a safe WS2 patch
- Follow-up target:
  - define a replay format or lookup policy keyed by request/trace identity before changing clone semantics

### Resume restore should preserve the checkpoint actually selected by the operator

- Area:
  - `agintor/runner.py`
  - `TaskRuntime._restore_from_checkpoint()`
- Current behavior:
  - after loading and reconciling a checkpoint envelope, restore overwrites `RuntimeState.latest_checkpoint_ref` by asking the shell for the latest published checkpoint under the run
  - if resume was intentionally started from an older checkpoint or from an external checkpoint store, the active baseline can silently drift to a newer checkpoint or to `None`
- Why not fixed in this WS2 closure pass:
  - the dominant same-run resume path still works when the latest published checkpoint is also the intended resume point
  - the bug shows up when resume provenance matters, not in the basic solve path
- Why it matters for later work:
  - WS3 durability and recovery lineage should build on the exact checkpoint the runtime resumed from
  - leaving this as-is risks making persisted recovery history truthful at the envelope level but wrong at the live runtime-state level
- Follow-up target:
  - preserve the selected checkpoint ref as the active resume baseline unless and until a newer checkpoint is published during the resumed run
  - keep explicit coverage for older-checkpoint resume and external-store resume flows

### Host finalization should honor runtime-returned checkpoint refs

- Area:
  - `agintor/runtime_host.py`
  - `RuntimeHost._finalize_execution_unit()`
- Current behavior:
  - host finalization recomputes `latest_checkpoint_ref` from `RunStore.latest_usable_checkpoint_ref(manifest.run_root)` instead of first honoring the checkpoint ref returned by the runtime result
  - this can erase resumability when the runtime used an external checkpoint store or when the resumed run intentionally came from an older checkpoint outside the default latest-in-run-root path
  - this also weakens the meaning of `run_lifecycle_state="paused"` because the resumability proof can be stripped before the run is finalized
- Why not fixed in this WS2 closure pass:
  - same-run checkpoints emitted under the durable run root already survive finalization, so the main local path still works
  - the mismatch appears on cross-store or explicit-rollback resume flows rather than the dominant local happy path
- Why it matters for later work:
  - WS3 will make run state and checkpoint lineage more durable and queryable, so host-side loss of the runtime's chosen checkpoint ref becomes more expensive and harder to reason about
  - this is unlikely to disappear organically in later workstreams because it sits in host finalization semantics
- Follow-up target:
  - treat runtime-returned checkpoint refs as authoritative for finalization, with run-store lookup used as fallback rather than replacement
  - preserve resumability for external checkpoint-store and explicit older-checkpoint resume flows

### Transfer-scored episode execution still resets runtime budget and solve loop per member

- Area:
  - `agintor/runtime_sdk/runtime_entry.py`
  - `_run_batch()`
  - transfer-scored `RuntimeTaskInvocation` groups
- Current behavior:
  - grouped transfer episodes reuse one `TaskRuntime` instance, but still execute each member via a separate `runner.run_task(...)`
  - each member therefore gets a fresh `RuntimeBudget` and a fresh top-level runtime state machine
  - long-term episode memory can still carry forward, but per-episode solve budgeting and single-run orchestration semantics do not
- Why not fixed in this WS2 closure pass:
  - current WS2 and early-WS3 critical paths do not yet depend on real transfer-scored benchmark episodes in the shipped suite
  - a correct fix is not a one-line patch; it needs an explicit episode-run contract rather than repeated single-task solves under one host grouping key
- Follow-up target:
  - introduce an explicit runtime-level episode execution path before transfer-scored benchmarks become part of active evaluation pressure
  - make budget, checkpointing, and solve-state semantics unambiguous for grouped episode execution

### Transfer-scored episode resume drops the remaining invocation set

- Area:
  - `agintor/runtime_api.py`
  - `solve_request_from_resume_checkpoint()`
  - grouped durable request envelopes with `request_kind="runtime_task_invocation_group"`
- Current behavior:
  - benchmark resume rebuilds only a single `SolveRequest` from the checkpoint task payload
  - if a grouped transfer episode pauses mid-episode, resume can continue the checkpointed task state but does not reconstruct the remaining invocation members of that evaluation unit
- Why not fixed in this WS2 closure pass:
  - same-workspace single-task resume is the dominant WS2 resume path and is already covered
  - the missing piece is specific to grouped transfer episodes, which are not yet a shipping benchmark dependency for WS3 persistence work
  - WS3 will consume the frozen checkpoint and resume contracts, so changing this safely is better done as an explicit episode-resume follow-up rather than as an unscoped closure patch
- Follow-up target:
  - persist and restore the remaining grouped invocation set as part of transfer-episode resume
  - keep the resumed evaluation unit lineage coherent across checkpoint restore, trace identity, and host reduction

### Transfer-scored episodes can collide on repeated task IDs

- Area:
  - `agintor/runtime_api.py`
  - `runtime_batch_request_for_tasks()`
  - transfer-scored invocation request-ID normalization
- Current behavior:
  - transfer-scored invocations currently derive `request_id` as `benchmark.<task_id>.seed_<seed>` without an episode-step or duplicate suffix
  - if an episode legitimately revisits the same `task_id`, host-side batch result validation sees duplicate `request_id` values and rejects the run
  - per-request traces and checkpoints for those members can also collide
- Why not fixed in this WS2 closure pass:
  - the issue only appears when one transfer-scored episode intentionally repeats the same task ID
  - current repo usage does not appear to rely on that pattern yet
- Follow-up target:
  - include episode-step or duplicate disambiguation in transfer-scored invocation request identity while preserving stable evaluation-unit identity for the grouped episode

### Root execution can respawn already-completed plan nodes when the runnable frontier is empty

- Area:
  - `agintor/runner.py`
  - `TaskRuntime._run_root_frame()`
  - `_active_runnable_frontier()` fallback handling
- Current behavior:
  - `_run_root_frame()` uses `frontier_nodes or execution_nodes` when choosing candidate work
  - once the runnable frontier is empty but the plan already contains completed nodes and queued continuations, root execution can feed the entire plan back into topology selection
  - on multi-node verified plans this can respawn child work for already-completed `builtin`, `merge`, or `verify` nodes, producing `node_reused_from_checkpoint` loops and eventually exhausting steps into `controlled_failure`
- Why not fixed in this WS2 closure pass:
  - the clean fix needs a tighter distinction between “no runnable work remains” and “root should continue scheduling”, not just a local guard
  - the issue is real, but the surrounding runtime loop is already in a fragile state and this is better handled deliberately than patched under closure pressure
- Why it matters for later work:
  - WS3 persistence and recovery work assumes plan-node completion state is trustworthy across checkpoint restore and continuation
  - this bug corrupts that assumption because completed nodes can be rescheduled instead of treated as terminalized solve-time history
- Follow-up target:
  - make an empty runnable frontier a first-class terminal or blocked condition instead of falling back to the full execution plan
  - recheck root continuation scheduling, failed-verification behavior, and restored-checkpoint continuation together

### Sync tool calls are not receipt-safe across crash and resume boundaries

- Area:
  - `agintor/runner.py`
  - `_execute_tool_operation()`
  - synchronous tool dispatch receipt publication
- Current behavior:
  - sync tool execution calls `run_tool()` before any launch receipt or checkpoint boundary is published
  - only a terminal `tool_completion` receipt is written after the tool returns
  - if a non-idempotent sync tool succeeds and the runtime crashes before the completion receipt is persisted, resume has no durable proof that the side effect already happened and can reissue the tool blindly
- Why not fixed in this WS2 closure pass:
  - a correct fix needs the sync path to adopt the same launch/completion receipt discipline as provider requests and async tools
  - that is a real contract adjustment, not a small closure-only patch
- Why it matters for later work:
  - WS2 acceptance explicitly requires that receipt-backed side effects are not re-executed on resume
  - WS3 durability work will build on that assumption, so leaving sync calls outside the receipt protocol creates a latent contract hole rather than something that will disappear organically
- Follow-up target:
  - emit a durable launch receipt and checkpoint boundary before executing any sync tool with externally meaningful side effects
  - reuse the existing reconciliation model so sync, async, and provider-backed actions obey one side-effect contract

### Non-hard terminal failures lose structured `failure_kind` in run results

- Area:
  - `agintor/runner.py`
  - `TaskRuntime._build_run_result()`
- Current behavior:
  - `_build_run_result()` only writes `RunResult.failure_kind` when `hard_invalid=True`
  - controlled failures, cancellations, and other non-hard terminal failures therefore lose their structured failure class before `solve_result_from_run_result_with_context()` and host finalization run
  - downstream summaries such as `faults.failure_kind` and durable `last_failure_kind` can come back empty even when the trace recorded a specific runtime failure reason
- Why not fixed in this WS2 closure pass:
  - the runtime still emits the detailed failure semantics in structured events, so WS3 persistence is not blocked on inventing a new contract here
  - this weakens summary fidelity more than it breaks core solve/resume/checkpoint behavior
  - fixing it cleanly should be done alongside a pass over host reduction/finalization so failure summaries stay internally consistent
- Follow-up target:
  - preserve `failure_kind` for non-hard terminal failures in `RunResult`
  - recheck host-side reduction and manifest finalization so durable failure summaries agree with runtime events

### Memory lookup downstream coercion still checks `output_key` instead of upstream `node_id`

- Area:
  - `agintor/runner.py`
  - `TaskRuntime._execute_memory_lookup()`
- Current behavior:
  - plan-node dependencies are stored as upstream `node_id` values
  - the `feeds_downstream` check still compares `operation.output_key` against `candidate.dependencies`
  - chained plans that feed a memory lookup into a later builtin/tool node therefore skip `_coerce(...)` and pass raw string/JSON content downstream
- Why not fixed in this WS2 closure pass:
  - the dominant shipped solve templates still use memory lookup as a terminal answer path rather than as an intermediate typed input
  - this is a real execution bug, but it does not undermine the checkpoint, receipt, branch, or isolation contracts WS3 is about to build on
- Follow-up target:
  - detect downstream consumers via upstream `node_id` rather than `output_key`
  - recheck any chained memory-lookup plan templates so downstream nodes receive the coerced value shape they were compiled for

### Duplicate benchmark invocations still collide on persisted trace filenames

- Area:
  - `agintor/shell.py`
  - `FixedShell.save_trace()`
- Current behavior:
  - WS2 now keeps benchmark duplicates as separate invocations, but persisted trace filenames still key only on `task_id` and `seed`
  - two duplicate invocations inside the same attempt therefore write the same trace JSON path
  - both `RunResult.trace_path` values can end up pointing at the last writer, silently dropping one invocation's trace
- Why not fixed in this WS2 closure pass:
  - this is a real trace-integrity issue, but it does not change runtime execution, checkpoint recovery, or the branch/resume contract WS3 needs to inherit first
  - the common solve and resume paths still execute correctly even though duplicate trace artifacts are overwritten
- Follow-up target:
  - include stable invocation identity such as `request_id` or evaluation-unit/member identity in persisted trace filenames
  - keep duplicate benchmark invocations covered when trace grouping and durable trace persistence tighten in later workstreams

### Branch admission can discard feasible worker sets under tight budgets

- Area:
  - `agintor/runner.py`
  - `TaskRuntime._launchable_branch_plans()`
- Current behavior:
  - branch admission trims over-budget worker sets by repeatedly popping only the last-ranked worker
  - with heterogeneous workers, a cheaper later worker can be discarded before an expensive higher-ranked worker
  - horizontal execution can therefore be skipped even when a feasible worker set still exists under the remaining budget
- Why not fixed in this WS2 closure pass:
  - this is a real runtime-quality and concurrency regression, but it does not corrupt the checkpoint/resume/event contract that WS3 depends on
  - the dominant shipped baseline currently proposes near-homogeneous workers, so the bug is more about future policy headroom than immediate WS3 persistence safety
- Follow-up target:
  - make branch admission feasibility-driven instead of tail-pop-driven
  - filter or reorder individually infeasible workers before aggregate trimming, and add coverage for heterogeneous worker budgets

### Default `agintor solve` workspace no longer isolates each invocation

- Area:
  - `agintor/cli.py`
  - `solve_cmd()`
  - `agintor/runtime_host.py`
  - `_run_local_inspect()`
  - `_run_local_solve()`
- Current behavior:
  - when `--workspace` is omitted, `solve_cmd()` now pins execution to the shared `solve` purpose root instead of allocating an implicit per-run workspace
  - the runtime host creates deterministic `inspect_...` and `solve_...` subdirectories under that shared root from the request payload
  - repeated or concurrent identical solve requests can therefore target the same host-side working directory and clobber each other's transport files
  - the outer solve workspace also stops honoring the previous artifact-mode cleanup behavior because it is now an explicit lease
- Why not fixed in this WS2 closure pass:
  - this is a CLI workspace regression, not a runtime-contract or persistence-contract break
  - the main WS2 runtime, checkpoint, resume, and isolation handoff to WS3 does not depend on the default solve workspace being unique per invocation
- Follow-up target:
  - restore implicit per-invocation workspace allocation for the default `solve` path when `--workspace` is not provided
  - keep explicit shared-root behavior only when the operator actually passes a workspace path

### Docker batch convenience wrapper bypasses WS2 invocation normalization

- Area:
  - `agintor/container_runtime.py`
  - `DockerRuntimeExecutor.run_batch()`
  - `agintor/runtime_api.py`
  - `runtime_batch_request_for_tasks()`
- Current behavior:
  - the Docker batch convenience wrapper hand-builds `RuntimeBatchRequest` and `RuntimeTaskInvocation` objects instead of using the WS2 request builder
  - duplicate `(task_id, seed)` pairs therefore receive the same `request_id`
  - `evaluation_unit_id`, `episode_kind`, `episode_step_index`, and per-invocation `trace_context` are also left unset
  - direct callers of this wrapper therefore miss the stable per-invocation identity and grouping semantics that the rest of the WS2 batch path now assumes
- Why not fixed in this WS2 closure pass:
  - the dominant batch path for WS2 and the WS3 handoff is `RuntimeHost.run_batch()`, which already uses the normalized request builder
  - this bug is real, but it sits in a secondary convenience wrapper rather than the core host/runtime batch contract
- Follow-up target:
  - route `DockerRuntimeExecutor.run_batch()` through `runtime_batch_request_for_tasks()` so duplicate-task handling, grouped episodes, and trace stamping stay aligned with the main batch protocol
