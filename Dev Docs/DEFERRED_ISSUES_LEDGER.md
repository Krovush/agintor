# Deferred Issues Ledger

Verified against the live codebase on 2026-07-09. This file contains unresolved implementation issues only; completed and superseded entries have been removed.

## Runtime, Host, and Durability Backlog

### Transfer-scored episode execution still resets runtime budget and solve loop per member

- Area:
  - `agintor/runtime/sdk/entrypoint.py`
  - `_run_batch()`
  - transfer-scored `RuntimeTaskInvocation` groups
- Current behavior:
  - grouped transfer episodes reuse one `TaskRuntime` instance, but still execute each member via a separate `runner.run_task(...)`
  - each member therefore gets a fresh `RuntimeBudget` and a fresh top-level runtime state machine
  - long-term episode memory can still carry forward, but per-episode solve budgeting and single-run orchestration semantics do not
- Follow-up target:
  - introduce an explicit runtime-level episode execution path before transfer-scored benchmarks become part of active evaluation pressure
  - make budget, checkpointing, and solve-state semantics unambiguous for grouped episode execution

### Docker durable-run indexes need explicit host projection semantics

- Area:
  - `agintor/runtime/host/backends/docker/run_rewrite.py`
  - `DockerRuntimeExecutor._rewrite_durable_run_paths()`
  - `agintor/storage/state_store/rebuild.py`
  - `agintor/storage/state_store/queries.py`
  - `StateStore.rebuild_from_canonical()`
- Current behavior:
  - Docker finalization preserves canonical replay payloads such as standalone `side_effects/*.json`, `events/*.json`, trace payloads, and long-term memory shards in container coordinates so resume replay can still see the same `/mnt/...` values the runtime recorded
  - the SQLite state index is then rebuilt directly from those canonical payloads
  - host-facing index queries such as receipt artifact rows, runtime-event payload rows, and long-term write rows can therefore expose `/mnt/request-files`, `/mnt/runtime`, or `/mnt/runs` values after the container mount no longer exists
- Follow-up target:
  - define which state-store tables are canonical replay indexes and which are host-readable projections
  - add a projection layer or separate projected columns for host path views while keeping canonical JSON payloads unchanged for resume
  - cover Docker finalized receipts, events, and long-term memory writes with tests that assert both replay fidelity and host usability

### Transfer-scored episode resume drops the remaining invocation set

- Area:
  - `agintor/runtime/api/resume.py`
  - `solve_request_from_resume_checkpoint()`
  - grouped durable request envelopes with `request_kind="runtime_task_invocation_group"`
- Current behavior:
  - benchmark resume rebuilds only a single `SolveRequest` from the checkpoint task payload
  - if a grouped transfer episode pauses mid-episode, resume can continue the checkpointed task state but does not reconstruct the remaining invocation members of that evaluation unit
- Follow-up target:
  - persist and restore the remaining grouped invocation set as part of transfer-episode resume
  - keep the resumed evaluation unit lineage coherent across checkpoint restore, trace identity, and host reduction

### Transfer-scored episodes can collide on repeated task IDs

- Area:
  - `agintor/runtime/api/protocol.py`
  - `agintor/runtime/api/tracing.py`
  - `runtime_batch_request_for_tasks()`
  - transfer-scored invocation request-ID normalization
- Current behavior:
  - transfer-scored invocations currently derive `request_id` as `benchmark.<task_id>.seed_<seed>` without an episode-step or duplicate suffix
  - if an episode legitimately revisits the same `task_id`, host-side batch result validation sees duplicate `request_id` values and rejects the run
  - per-request traces and checkpoints for those members can also collide
- Follow-up target:
  - include episode-step or duplicate disambiguation in transfer-scored invocation request identity while preserving stable evaluation-unit identity for the grouped episode

### Non-hard terminal failures lose structured `failure_kind` in run results

- Area:
  - `agintor/runtime/kernel/checkpointing/results.py`
  - `TaskRuntime._build_run_result()`
- Current behavior:
  - `_build_run_result()` only writes `RunResult.failure_kind` when `hard_invalid=True`
  - controlled failures, cancellations, and other non-hard terminal failures therefore lose their structured failure class before `solve_result_from_run_result_with_context()` and host finalization run
  - downstream summaries such as `faults.failure_kind` and durable `last_failure_kind` can come back empty even when the trace recorded a specific runtime failure reason
- Follow-up target:
  - preserve `failure_kind` for non-hard terminal failures in `RunResult`
  - recheck host-side reduction and manifest finalization so durable failure summaries agree with runtime events

### Memory lookup downstream coercion still checks `output_key` instead of upstream `node_id`

- Area:
  - `agintor/runtime/kernel/memory.py`
  - `TaskRuntime._execute_memory_lookup()`
- Current behavior:
  - plan-node dependencies are stored as upstream `node_id` values
  - the `feeds_downstream` check still compares `operation.output_key` against `candidate.dependencies`
  - chained plans that feed a memory lookup into a later builtin/tool node therefore skip `_coerce(...)` and pass raw string/JSON content downstream
- Follow-up target:
  - detect downstream consumers via upstream `node_id` rather than `output_key`
  - recheck any chained memory-lookup plan templates so downstream nodes receive the coerced value shape they were compiled for

### Branch admission can discard feasible worker sets under tight budgets

- Area:
  - `agintor/runtime/kernel/branches/budget.py`
  - `TaskRuntime._launchable_branch_plans()`
- Current behavior:
  - branch admission trims over-budget worker sets by repeatedly popping only the last-ranked worker
  - with heterogeneous workers, a cheaper later worker can be discarded before an expensive higher-ranked worker
  - horizontal execution can therefore be skipped even when a feasible worker set still exists under the remaining budget
- Follow-up target:
  - make branch admission feasibility-driven instead of tail-pop-driven
  - filter or reorder individually infeasible workers before aggregate trimming, and add coverage for heterogeneous worker budgets

### Default `agintor solve` workspace no longer isolates each invocation

- Area:
  - `agintor/cli.py`
  - `solve_cmd()`
  - `agintor/runtime/host/local_process.py`
  - `_run_local_inspect()`
  - `_run_local_solve()`
- Current behavior:
  - when `--workspace` is omitted, `solve_cmd()` now pins execution to the shared `solve` purpose root instead of allocating an implicit per-run workspace
  - the runtime host creates deterministic `inspect_...` and `solve_...` subdirectories under that shared root from the request payload
  - repeated or concurrent identical solve requests can therefore target the same host-side working directory and clobber each other's transport files
  - the outer solve workspace also stops honoring the previous artifact-mode cleanup behavior because it is now an explicit lease
- Follow-up target:
  - restore implicit per-invocation workspace allocation for the default `solve` path when `--workspace` is not provided
  - keep explicit shared-root behavior only when the operator actually passes a workspace path

### Repeating the same transfer-scored episode with the same seed collapses batch identity

- Area:
  - `agintor/runtime/api/protocol.py`
  - `agintor/runtime/api/tracing.py`
  - `runtime_batch_request_for_tasks()`
  - `agintor/runtime/host/host.py`
  - `RuntimeHost.run_batch()`
- Current behavior:
  - transfer-scored invocations currently key grouped execution only by `episode_id` plus `seed`
  - if the same transfer episode is intentionally scheduled twice with the same seed, both copies get the same grouped `evaluation_unit_id`
  - the member `request_id` values also repeat, so host-side batch grouping and response validation collapse what should be two repeated evaluation units into one durable run/request namespace
- Follow-up target:
  - disambiguate repeated transfer-episode invocations at the evaluation-unit layer without breaking grouped episode semantics
  - keep per-member `request_id`, grouped `evaluation_unit_id`, and durable run identity aligned when the same episode is scheduled more than once with the same seed

### Shaped batch failures currently erase real usage and latency

- Area:
  - `agintor/runtime/sdk/entrypoint.py`
  - `_shape_batch_failure_run()`
- Current behavior:
  - when one batch invocation raises and gets converted into a shaped `RunResult`, the fallback result hardcodes `cost=0`, `latency=0`, and empty `provider_usage`
  - if the failing invocation already spent model calls before the exception, both the per-run metrics and the aggregated `RuntimeBatchResponse.provider_usage` under-report real usage
- Follow-up target:
  - preserve provider-usage delta and elapsed latency when shaping failed batch invocations so evaluation artifacts stay truthful even on failure paths

### Resumed runs under-report end-to-end latency

- Area:
  - `agintor/runtime/kernel/checkpointing/restore.py`
  - `agintor/runtime/kernel/checkpointing/results.py`
  - `TaskRuntime._restore_runtime_state_snapshot()`
  - `TaskRuntime._build_run_result()`
- Current behavior:
  - checkpoint restore repopulates `RuntimeBudget.latency` from the checkpoint snapshot
  - the final `RunResult` still reports `latency=time.perf_counter() - start`, which only measures the resumed segment
  - resumed solve and batch runs therefore under-report total latency relative to uninterrupted runs
- Follow-up target:
  - include pre-resume latency in the final `RunResult` latency field or persist an explicit end-to-end elapsed-time counter across checkpoints
  - recheck solve/batch aggregation so resumed runs compare fairly against uninterrupted runs

### Post-launch provider and tool failures leave launched-only receipts

- Area:
  - `agintor/runtime/api/context.py`
  - `PolicyContext.run_model_request()`
  - `agintor/runtime/kernel/tooling.py`
  - `_execute_tool_operation()`
- Current behavior:
  - provider requests and tool executions publish launch receipts and checkpoint boundaries before the side effect finishes
  - if `provider.generate()` throws, or if a launched tool fails before its completion receipt is recorded, the checkpoint ledger is left with a `launched` receipt only
  - strict resume then fails closed with `receipt_reconciliation_failed` even though the original action has already reached a local terminal failure
- Follow-up target:
  - emit terminal failure receipts for provider and tool paths that throw after launch persistence
  - keep resume behavior fail-closed when terminal status still cannot be proven, but distinguish known local failure from unknown in-flight state

### Prompt-mode file inspection can compile into repo patch plans

- Area:
  - `agintor/runtime/api/plan_compiler.py`
  - `agintor/runtime/api/prompt_intent.py`
  - `solve_request_to_task()`
  - `_prompt_requests_repo_patch()`
  - `_prompt_requests_file_inspection()`
- Current behavior:
  - prompt-mode request adaptation checks the repo-patch template before the file-inspection template
  - broad tokens such as `fix`, `change`, or `update` therefore push read-only prompts like `review the fix in foo.py` into the write-capable repo-patch path
- Follow-up target:
  - make file-inspection intent outrank repo-patch intent when the prompt is clearly read-only
  - tighten repo-patch triggering so broad review/debug language does not cross into write semantics

### Prompt-mode file path extraction ignores repo-relative paths

- Area:
  - `agintor/runtime/api/request_loading.py`
  - `_PROMPT_ABSOLUTE_PATH_RE`
  - `_request_file_paths()`
- Current behavior:
  - plain prompt-mode adaptation only extracts absolute paths from free-text prompts
  - common repo-relative requests such as `review src/app.py` or `fix tests/test_runtime.py` therefore miss the file-aware templates and fall back to generic direct-response behavior
- Follow-up target:
  - recognize bounded repo-relative paths during prompt adaptation
  - keep the extraction tied to the runtime workspace so file-aware prompt templates work without requiring absolute paths

### Service-action wall time is not charged into runtime budget accounting

- Area:
  - `agintor/runtime/kernel/io/service_action.py`
  - `TaskRuntime._execute_service_action_node()`
- Current behavior:
  - the `service_action` path performs `urllib_request.urlopen(...)` and returns an output artifact, but never charges the elapsed call time into `RuntimeBudget.latency`
  - final run latency still reflects total wall-clock runtime, but branch reservations and stop-policy heuristics read `RuntimeBudget.latency`, not the final `RunResult.latency`
  - long or hanging HTTP calls can therefore overspend the reserved branch latency slice without being reflected in the runtime-side budget state
- Follow-up target:
  - charge service-action elapsed wall time into `RuntimeBudget.latency` just like tool and checker latency
  - recheck branch reservation enforcement and stop-policy behavior for `service_action` nodes once that accounting path is wired up

### No explicit preview surface for private evolution candidates

- Area:
  - `agintor/cli.py`
  - `agintor/factory/service.py`
  - `agintor/factory/pipeline.py`
  - `agintor/search/engine.py`
  - `agintor/evaluation/evaluator.py`
  - `agintor/runtime/host/host.py`
- Current behavior:
  - `build-runtime` treats candidate runtimes as private factory artifacts while Agintor mutates and evaluates them
  - `solve --prompt` targets the current released runtime snapshot in the project/runtime directory, not an in-progress candidate
  - there is no public command or API for selecting a candidate runtime from an active evolution workspace and manually prompting it as a preview
  - manually pointing `solve` at internal candidate directories would blur private factory evaluation state with released-runtime user sessions
- Follow-up target:
  - design an explicit read-only preview command/API for manually testing a selected candidate runtime
  - define how candidates are selected, named, retained, and cleaned up after preview
  - keep preview sessions separate from released-runtime sessions unless a candidate is explicitly promoted
  - decide whether preview feedback can become factory evidence, and require an explicit user action if it can

## Evaluation and Promotion Backlog

### Pairwise preference, defect-search, and metamorphic comparator surfaces are declared but not production-backed

- Area:
  - `agintor/contracts/evidence.py`
  - `agintor/evaluation/pairwise_comparator.py`
  - `agintor/evaluation/challenge_generators.py`
  - `agintor/evaluation/progress_oracle.py`
- Current behavior:
  - contract literals still allow `pairwise_preference`, `defect_search`, and `metamorphic` comparator types
  - `PairwiseArtifactComparator` is standalone and not wired into Stage 4 promotion decisions
  - generated workflow challenges emit `metamorphic_tags`, but the oracle does not consume metamorphic evidence
  - `ProgressOracle` explicitly rejects all three comparator kinds as unsupported, while their contract and schema surfaces remain declared
- Follow-up target:
  - either remove the unbacked comparator literals and preference route from the active contract surface, or implement a real calibrated comparator path end to end
  - if kept, wire comparator outputs into `ProgressOracle.decide()` with authority levels, leakage/health checks, and focused regression tests
