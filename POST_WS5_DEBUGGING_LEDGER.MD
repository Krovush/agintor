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

### Docker durable-run indexes need explicit host projection semantics

- Area:
  - `agintor/container_runtime.py`
  - `DockerRuntimeExecutor._rewrite_durable_run_paths()`
  - `agintor/state_store.py`
  - `StateStore.rebuild_from_canonical()`
- Current behavior:
  - Docker finalization preserves canonical replay payloads such as standalone `side_effects/*.json`, `events/*.json`, trace payloads, and long-term memory shards in container coordinates so resume replay can still see the same `/mnt/...` values the runtime recorded
  - the SQLite state index is then rebuilt directly from those canonical payloads
  - host-facing index queries such as receipt artifact rows, runtime-event payload rows, and long-term write rows can therefore expose `/mnt/request-files`, `/mnt/runtime`, or `/mnt/runs` values after the container mount no longer exists
- Why not fixed in this pass:
  - rewriting the canonical files would reintroduce the resume/replay corruption this pass was fixing
  - rewriting the current index ad hoc would make `state_store` ambiguously mix canonical replay state with host-view projections
  - the durable fix needs an explicit dual-coordinate contract for state-store query projections rather than another path mutation pass
- Follow-up target:
  - define which state-store tables are canonical replay indexes and which are host-readable projections
  - add a projection layer or separate projected columns for host path views while keeping canonical JSON payloads unchanged for resume
  - cover Docker finalized receipts, events, and long-term memory writes with tests that assert both replay fidelity and host usability

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

### Repeating the same transfer-scored episode with the same seed collapses batch identity

- Area:
  - `agintor/runtime_api.py`
  - `runtime_batch_request_for_tasks()`
  - `agintor/runtime_host.py`
  - `RuntimeHost.run_batch()`
- Current behavior:
  - transfer-scored invocations currently key grouped execution only by `episode_id` plus `seed`
  - if the same transfer episode is intentionally scheduled twice with the same seed, both copies get the same grouped `evaluation_unit_id`
  - the member `request_id` values also repeat, so host-side batch grouping and response validation collapse what should be two repeated evaluation units into one durable run/request namespace
- Why not fixed in this WS2 closure pass:
  - this only appears when an identical transfer-scored episode is deliberately rerun with the same seed for repeatability or comparison
  - current WS2 closure and WS3 handoff work do not rely on repeated transfer-episode duplicates in the active shipped suite
  - the fix should be deliberate because it needs to preserve stable within-episode grouping semantics while adding a higher-level duplicate ordinal for repeated episode runs
- Follow-up target:
  - disambiguate repeated transfer-episode invocations at the evaluation-unit layer without breaking grouped episode semantics
  - keep per-member `request_id`, grouped `evaluation_unit_id`, and durable run identity aligned when the same episode is scheduled more than once with the same seed

### Shaped batch failures currently erase real usage and latency

- Area:
  - `agintor/runtime_sdk/runtime_entry.py`
  - `_shape_batch_failure_run()`
- Current behavior:
  - when one batch invocation raises and gets converted into a shaped `RunResult`, the fallback result hardcodes `cost=0`, `latency=0`, and empty `provider_usage`
  - if the failing invocation already spent model calls before the exception, both the per-run metrics and the aggregated `RuntimeBatchResponse.provider_usage` under-report real usage
- Why not fixed in this WS2 closure pass:
  - this is a real evaluation-accounting bug, but it does not break the runtime state machine, checkpoint envelope contract, branch semantics, or resume semantics that WS3 is about to inherit
  - the common successful solve and resume paths still preserve usage correctly
- Follow-up target:
  - preserve provider-usage delta and elapsed latency when shaping failed batch invocations so evaluation artifacts stay truthful even on failure paths

### Resumed runs under-report end-to-end latency

- Area:
  - `agintor/runner.py`
  - `TaskRuntime._restore_runtime_state_snapshot()`
  - `TaskRuntime._build_run_result()`
- Current behavior:
  - checkpoint restore repopulates `RuntimeBudget.latency` from the checkpoint snapshot
  - the final `RunResult` still reports `latency=time.perf_counter() - start`, which only measures the resumed segment
  - resumed solve and batch runs therefore under-report total latency relative to uninterrupted runs
- Why not fixed in this WS2 closure pass:
  - this does not change checkpoint safety, receipt replay rules, branch execution semantics, or runtime isolation guarantees
  - the gap is in evaluation/accounting fidelity, not in the core solve-time contract WS3 is about to inherit
- Follow-up target:
  - include pre-resume latency in the final `RunResult` latency field or persist an explicit end-to-end elapsed-time counter across checkpoints
  - recheck solve/batch aggregation so resumed runs compare fairly against uninterrupted runs

### Post-launch provider and tool failures leave launched-only receipts

- Area:
  - `agintor/runtime_api.py`
  - `PolicyContext.run_model_request()`
  - `agintor/runner.py`
  - `_execute_tool_operation()`
- Current behavior:
  - provider requests and tool executions publish launch receipts and checkpoint boundaries before the side effect finishes
  - if `provider.generate()` throws, or if a launched tool fails before its completion receipt is recorded, the checkpoint ledger is left with a `launched` receipt only
  - strict resume then fails closed with `receipt_reconciliation_failed` even though the original action has already reached a local terminal failure
- Why not fixed in this WS2 closure pass:
  - the runtime still fails closed and avoids blind reissue, so this does not undermine the receipt, checkpoint, or persistence contract WS3 is about to inherit
  - the gap is in recovery fidelity for failure paths, not in the core solve-time meaning of the persisted artifacts
- Follow-up target:
  - emit terminal failure receipts for provider and tool paths that throw after launch persistence
  - keep resume behavior fail-closed when terminal status still cannot be proven, but distinguish known local failure from unknown in-flight state

### Prompt-mode file inspection can compile into repo patch plans

- Area:
  - `agintor/runtime_api.py`
  - `solve_request_to_task()`
  - `_prompt_requests_repo_patch()`
  - `_prompt_requests_file_inspection()`
- Current behavior:
  - prompt-mode request adaptation checks the repo-patch template before the file-inspection template
  - broad tokens such as `fix`, `change`, or `update` therefore push read-only prompts like `review the fix in foo.py` into the write-capable repo-patch path
- Why not fixed before WS3 handoff:
  - this is a user-request adaptation bug, not a checkpoint, receipt, branch, or persistence-contract break
  - WS3 can proceed on top of the frozen WS2 state and durability contracts without depending on this prompt-template ordering
- Follow-up target:
  - make file-inspection intent outrank repo-patch intent when the prompt is clearly read-only
  - tighten repo-patch triggering so broad review/debug language does not cross into write semantics

### Prompt-mode file path extraction ignores repo-relative paths

- Area:
  - `agintor/runtime_api.py`
  - `_PROMPT_PATH_RE`
  - `_request_file_paths()`
- Current behavior:
  - plain prompt-mode adaptation only extracts absolute paths from free-text prompts
  - common repo-relative requests such as `review src/app.py` or `fix tests/test_runtime.py` therefore miss the file-aware templates and fall back to generic direct-response behavior
- Why not fixed before WS3 handoff:
  - this hurts prompt-mode ergonomics and correctness, but it does not undermine the runtime-state, checkpoint, or persistence surfaces that WS3 inherits
  - the bug is local to prompt adaptation rather than to the WS2 execution-state contract
- Follow-up target:
  - recognize bounded repo-relative paths during prompt adaptation
  - keep the extraction tied to the runtime workspace so file-aware prompt templates work without requiring absolute paths

### Docker solve and batch containers do not mount prompt-mode request files

- Area:
  - `agintor/container_runtime.py`
  - `DockerRuntimeExecutor.solve_protocol()`
  - `DockerRuntimeExecutor.run_batch_protocol()`
- Current behavior:
  - the Docker execution path mounts the runtime bundle, transport JSON, provider payloads, workspace, output directory, and durable run roots
  - it does not mount host-side `request.file_paths` / `task.file_paths` that the new prompt-mode `file_inspection` and `repo_patch` templates compile into `filesystem/read_text_file` nodes
  - file-backed prompt solves therefore work locally but fail inside Docker because the referenced files are absent from the container filesystem
- Why not fixed before WS3 handoff:
  - this is a backend-specific prompt-execution gap, not a failure of the checkpoint, branch, receipt, or runtime-event contracts that WS3 is about to persist
  - WS3 can still safely build on the frozen WS2 solve-state and durability surfaces without depending on Docker prompt-mode file inspection or patching
- Follow-up target:
  - mount validated request/task input files into Docker solve and batch containers with stable container-side rewrites
  - keep the mounted-path rewrite aligned with the execution-plan file bindings so file-backed prompt templates behave the same across `local` and `docker`

### Service-action wall time is not charged into runtime budget accounting

- Area:
  - `agintor/runner.py`
  - `TaskRuntime._execute_service_action_node()`
- Current behavior:
  - the `service_action` path performs `urllib_request.urlopen(...)` and returns an output artifact, but never charges the elapsed call time into `RuntimeBudget.latency`
  - final run latency still reflects total wall-clock runtime, but branch reservations and stop-policy heuristics read `RuntimeBudget.latency`, not the final `RunResult.latency`
  - long or hanging HTTP calls can therefore overspend the reserved branch latency slice without being reflected in the runtime-side budget state
- Why not fixed before WS3 handoff:
  - this is evaluation and control-accounting drift on one operation family, not a break in the persisted checkpoint, receipt, recovery, or trace contracts that WS3 consumes
  - the service-action side-effect protocol itself is the WS3-critical part; the latency-charge omission weakens solve-time budgeting but does not invalidate the durability model
- Follow-up target:
  - charge service-action elapsed wall time into `RuntimeBudget.latency` just like tool and checker latency
  - recheck branch reservation enforcement and stop-policy behavior for `service_action` nodes once that accounting path is wired up

### No explicit preview surface for private evolution candidates

- Area:
  - `agintor/cli.py`
  - `agintor/runtime_builder.py`
  - `agintor/evolution.py`
  - `agintor/evaluator.py`
  - `agintor/runtime_host.py`
- Current behavior:
  - `build-runtime` treats candidate runtimes as private factory artifacts while Agintor mutates and evaluates them
  - `solve --prompt` targets the current released runtime snapshot in the project/runtime directory, not an in-progress candidate
  - there is no public command or API for selecting a candidate runtime from an active evolution workspace and manually prompting it as a preview
  - manually pointing `solve` at internal candidate directories would blur private factory evaluation state with released-runtime user sessions
- Why not fixed before the snapshot-continuity pass:
  - the current continuity work should keep the factory/runtime boundary strict: factory follow-ups evolve from released snapshots, and runtime chats attach to released snapshots
  - candidate preview is a separate product surface with its own identity, session, cleanup, and feedback semantics
  - adding it inside the continuity fix would risk making user prompts accidentally affect evolution state or making private candidates look like released runtimes
- Follow-up target:
  - design an explicit read-only preview command/API for manually testing a selected candidate runtime
  - define how candidates are selected, named, retained, and cleaned up after preview
  - keep preview sessions separate from released-runtime sessions unless a candidate is explicitly promoted
  - decide whether preview feedback can become factory evidence, and require an explicit user action if it can
