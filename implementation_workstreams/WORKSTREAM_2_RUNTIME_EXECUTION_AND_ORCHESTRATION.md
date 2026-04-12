# Workstream 2: Runtime Execution, Orchestration, and Isolation

## Outcome

- The exported runtime keeps the existing bundled solve-time kernel path, but all solve, batch, and resume execution semantics are consolidated behind that runtime-owned entrypoint.
- Benchmark tasks and bounded user requests execute through one runtime state machine built around a shared `ExecutionPlan` contract.
- Runtime requests, execution plans, frames, and branch publications carry a typed trace-correlation contract so every provider call can be attributed to one CLI session, runtime, task, request, frame, and operation.
- Horizontal mode becomes true concurrent branch execution with deterministic merge, explicit cancellation, and branch-level publication semantics.
- Checkpoints become restartable runtime artifacts with defined side-effect and recovery behavior.
- Runtime backend preflight and runtime-side enforcement share one explicit isolation contract for the guarantees this workstream actually uses.

## Prerequisites

- Workstream 1 exit gates are complete.
- The exported runtime already carries a bundled kernel, a versioned protocol, split profiles, and a validated deployment contract.
- Host responsibilities are limited to launch, request transport, capability inspection, and result collection.

## Sequence Position

- This workstream starts only after Workstream 1 freezes the host/runtime boundary, export contract, and bundled runtime kernel.
- Workstream 3 assumes that checkpoint boundaries, branch semantics, and side-effect semantics are already defined here.
- Workstreams 4 and 5 depend on the runtime entrypoint and orchestration semantics created here.

## Boundaries

- Own the solve-time runtime kernel, runtime state machine, request hydration, execution-plan loading, branch orchestration, checkpoint publication semantics, side-effect receipt semantics, runtime event model, and runtime-wide isolation policy.
- Own runtime-native trace-correlation fields on solve-time requests, execution plans, policy context, frames, and branch publications.
- Own runtime-side compilation from `RuntimeSolveRequest` and `RuntimeTaskInvocation` into `ExecutionPlan`. The host adapts external CLI inputs only as far as runtime request envelopes and transport.
- Keep archive scheduling, objective selection, phase control, benchmark planning, verifier definition, and leader selection outside this workstream.
- Keep durable storage implementation details outside this workstream. This workstream defines what must be serializable and when checkpoints must be emitted, not how long-term storage is indexed.
- Keep per-tool asset packaging and provider feature completion outside this workstream. This workstream provides orchestration contracts those later systems consume.
- Keep canonical raw call persistence, scoped transcript finalization, and provider-wire transcript rendering outside this workstream.

## Non-Goals

- Distributed multi-host orchestration
- Open-ended interactive replanning outside bounded request adaptation
- Remote job queues or cloud schedulers
- Provider and tool lifecycle unification beyond the receipt and checkpoint semantics introduced here

## Baseline

- Already implemented and should not be rebuilt here:
  - `agintor/runtime_host.py`, `agintor/runtime_sdk/runtime_entry.py`, and `agintor/container_runtime.py` already provide inspect, solve, and batch execution paths for bundled runtimes.
  - `agintor/runner.py` already runs a queue-driven solve loop with `single`, `vertical`, and `horizontal` modes.
  - `agintor/runtime_api.py` already defines `SolveRequest`, `SolveResult`, `RuntimeSolveRequest`, `RuntimeBatchRequest`, `RuntimeTaskInvocation`, `RuntimeState`, `RuntimeBudget`, and `PolicyContext`.
  - `agintor/shell.py` already owns the solve-time substrate: message board, handles, memory, tools, predictors, and shell invariants.
  - `agintor/runtime_loader.py` already validates runtime ABI, bundled kernel version, storage schema version, supported backends, and required runtime environment variables.
  - The mutator-visible control surface is already narrowed to `assign_model`, `request_checks`, and `stop_policy`.
- Implemented only partially and therefore still in scope:
  - Prompt-mode adaptation still compiles through `solve_request_to_task()` and `BenchmarkTask`, so user-request solving does not yet pass through a runtime-native execution-plan layer.
  - Batch transport exists, but per-invocation `request_id` and typed trace context are still missing.
  - Checkpoint files and `CheckpointReference` exist, but they are summary-only artifacts and are not wired into runtime resume semantics.
  - Horizontal mode still executes isolated workers sequentially with deep-copied state.
  - Async handles exist, but solve flow usually waits immediately instead of treating them as resumable side effects.
  - Docker execution exists, but isolation is still mostly packaging- and mount-oriented rather than guarantee-driven.

## Core Decisions

- Keep the fixed shell plus four mutable policy files as the solve-time architecture. The remaining change is to finish consolidating solve semantics under the bundled runtime boundary that already exists.
- Freeze the next runtime protocol and storage versions at the start of this workstream:
  - `runtime_abi = agintor-runtime-abi-v4`
  - `storage_schema_version = agintor-storage-v2`
- Use one runtime-native `ExecutionPlan` contract for both benchmark tasks and user requests.
- Adopt the canonical `OpenAITraceContext` contract defined in `TRACE_AND_PLANNING_IMPROVEMENTS_PLAN.md`. Workstream 2 owns runtime-side propagation of that full contract through request, execution-plan, policy-context, frame, and branch objects. Do not redefine a runtime-only subset here. Despite the historical name, this contract is provider-agnostic correlation metadata, not an OpenAI-only feature.
- Normalize benchmark request identity as `benchmark.<task_id>.seed_<seed>` so benchmark-mode traces and runtime artifacts have stable request keys.
- `RuntimeBatchRequest` stays transport-only. Each `RuntimeTaskInvocation` must become an independent evaluation unit with its own normalized `request_id` and invocation-level `trace_context` unless the benchmark explicitly declares the invocation set to be one transfer-scored episode.
- Preserve the existing host/runtime boundary:
  - the host continues to own CLI parsing, benchmark lookup, prompt-file loading, and request-envelope construction
  - the runtime kernel owns `RuntimeSolveRequest -> ExecutionPlan`, `RuntimeTaskInvocation -> ExecutionPlan`, plan validation, and plan execution
- Separate static plan structure from mutable solve-time topology:
  - plan compilation freezes semantic work units, dependency edges, terminal outputs, verification boundaries, and which nodes are branchable
  - topology policy may choose execution mode, agent assignment, and whether to instantiate a branchable group
  - topology policy may not invent new semantic work outside compiler-emitted plan nodes
  - `branch_group_id` marks nodes that may be fanned out together, but it does not force branching
- Use structured concurrency for horizontal work. Branch groups launch, cancel, and join as one unit.
- Use copy-in, publication-out semantics for branch isolation:
  - each branch starts from an isolated branch state snapshot
  - branches do not mutate parent shell state directly
  - branches may publish only typed outputs
  - parent merge happens once and in deterministic order
- Define side-effect receipts before the durable store lands. Every non-deterministic or externally meaningful action must have replay or reconciliation semantics.
- Make Docker contract enforcement fail closed. The runtime must not silently downgrade to weaker isolation than the deployment contract allows.

## Relationship to Existing Execution Path

- The current runtime loop in `agintor/runner.py` remains the execution foundation, but it stops executing raw benchmark-task operations directly.
- `ExecutionPlan` becomes the runtime-internal execution contract. The runner executes plans, not raw `BenchmarkTask` objects.
- `BenchmarkTask` remains a host-side and planning-side input contract. In benchmark mode, its operations compile into `PlanNode` objects with a 1:1 correspondence unless the runtime inserts explicit `verify`, `checkpoint`, or `merge` nodes required by the frozen plan contract.
- User-request adaptation stays bounded and template-driven. The existing structured request heuristics and templates may be reused, but they must feed plan compilation rather than continuing as a separate task-only shortcut path.
- `AgentFrame` remains the per-agent execution context. `ExecutionPlan` defines what must happen; frames define who is currently executing a runnable portion of the plan.
- Phase 4 concurrency does not replace the plan model. It expands plan execution so runnable nodes that share a `branch_group_id` may execute concurrently while preserving deterministic merge and publication rules.

## Phase 1: Complete Runtime Entrypoint Consolidation

- Preserve the existing `inspect`, `solve`, and `run-batch` protocol surfaces in `agintor/runtime_host.py`, `agintor/runtime_sdk/runtime_entry.py`, and `agintor/container_runtime.py`. Do not replace them with a second host/runtime transport path.
- Add the missing `resume` runtime-entry command and route it through the same bundled runtime entrypoint rather than inventing host-side restore logic.
- Keep direct `TaskRuntime` usage as an internal implementation detail. External solve-time execution should flow through the runtime entrypoint and host transport surfaces only.
- Bump ABI or storage schema only where the new WS2 contracts require it. Do not introduce a version bump for surfaces that already exist and remain semantically unchanged.

## Phase 3: Introduce a Runtime-Native Execution Plan

- Keep existing request transport objects and add an execution-plan layer above them. Do not redesign `SolveRequest`, `RuntimeSolveRequest`, or `RuntimeBatchRequest` from scratch.
- Add explicit runtime plan objects such as:
  - `ExecutionPlan`
  - `PlanNode`
  - `VerificationPlan`
  - `PlanOrigin`
  - `ExecutionFlags`
- Preserve the existing host-side external input -> runtime-request flow. The new work here is deterministic runtime-side compilation from:
  - `RuntimeSolveRequest -> ExecutionPlan`
  - `RuntimeTaskInvocation -> ExecutionPlan`
- Freeze the exact `ExecutionPlan` v1 shape. It must include at least:
  - `plan_schema_version`
  - `plan_digest`
  - `plan_id`
  - `request_id`
  - `origin`
  - `objective`
  - `context_refs`
  - `file_refs`
  - `nodes`
  - `root_node_ids`
  - `terminal_output_keys`
  - `verification_plan`
  - `execution_flags`
  - `allowed_tool_categories`
  - `budget_overrides`
  - `externally_visible`
  - `trace_context`
- Freeze the exact `PlanOrigin` v1 shape. It must include:
  - `origin_kind` with allowed values `benchmark` or `user_request`
  - `source_task_id`
  - `source_request_id`
  - `source_suite`
  - `adapter_kind`
  - `adaptation_assumptions`
- Freeze the exact `PlanNode` v1 shape. It must include:
  - `node_id`
  - `node_kind`
  - `instruction`
  - `output_key`
  - `dependencies`
  - `tool_hint`
  - `allowed_tool_categories`
  - `static_args`
  - `input_bindings`
  - `verification_required`
  - `externally_visible`
  - `frame_role`
  - `branch_group_id`
  - `metadata`
- `PlanNode.node_kind` is restricted to:
  - `builtin_op`
  - `memory_lookup`
  - `tool_call`
  - `tool_synthesis`
  - `direct_response`
  - `repo_patch`
  - `service_action`
  - `checkpoint`
  - `merge`
  - `verify`
- `dependencies` is an ordered list of upstream `node_id` values. No other dependency encoding is allowed in v1.
- `input_bindings` is the only legal way to bind request context, request files, upstream outputs, or plan constants into node arguments.
- `InputBinding` is fixed in v1. It includes:
  - `target_arg`
  - `source_kind`
  - `source_ref`
  - `required`
- `InputBinding.source_kind` is restricted to:
  - `request_context`
  - `request_file`
  - `upstream_output`
  - `plan_constant`
- Freeze the exact `VerificationPlan` v1 shape. It must include:
  - `mode`
  - `required`
  - `checker_ladder`
  - `exact_verifier_required`
  - `artifact_contract`
  - `terminal_nodes`
- Freeze the exact `ExecutionFlags` v1 shape. It must include:
  - `allow_best_effort`
  - `allow_resume`
  - `allow_branching`
  - `allow_tool_synthesis`
  - `allow_async_handles`
  - `requires_terminal_verification`
- Execution-plan lifecycle is fixed in v1:
  - `compiled`
  - `validated`
  - `loaded`
  - `running`
  - `completed`
  - `cancelled`
  - `failed`
- Validation rules are fixed in v1:
  - every `node_id` must be unique
  - the plan graph must be acyclic
  - every non-root node dependency must reference an existing `node_id`
  - every `root_node_id` must exist in `nodes`
  - `VerificationPlan.terminal_nodes` must reference existing `node_id` values
  - every `terminal_output_key` must be produced by a reachable node
  - every node in a `branch_group_id` must be reachable from the same live frontier
  - every merge node must consume exactly one declared branch group
  - `node_kind` values outside the allowed set are invalid
  - plan compilation may not create nodes that imply capabilities outside deployment-contract and runtime-plan bounds
- Extend runtime-facing request and execution contracts with a typed `OpenAITraceContext`.
- Carry trace context through at least:
  - `RuntimeSolveRequest`
  - `RuntimeBatchRequest`
  - `RuntimeTaskInvocation`
  - `ExecutionPlan`
  - `PolicyContext`
  - `AgentFrame`
  - `BranchPlan`
  - `BranchPublication`
- Runtime-side executions use `provider_role="runtime"`. This workstream only needs compatibility with the canonical trace contract; it does not define factory-side tracing behavior.
- Add helper builders that derive child, branch, and operation-level trace context from the parent request or frame instead of assembling dictionaries ad hoc.
- `RuntimeTaskInvocation.request_id` is mandatory for eval and batch execution. For benchmark invocations the host must set it to `benchmark.<task_id>.seed_<seed>`.
- For user requests, keep plan compilation bounded to validated templates such as:
  - direct answer
  - structured computation
  - file inspection
  - bounded repo patch
  - bounded service action
- The plan compiler is deterministic given a runtime request, invocation payload, runtime profile, and benchmark task payload.
- In benchmark mode:
  - `BenchmarkTask.operations` compile into `PlanNode` objects with a 1:1 mapping
  - task verifier fields drive `VerificationPlan`
  - the benchmark task remains the provenance input, not the execution unit
- In user-request mode:
  - bounded adaptation templates compile into fixed `ExecutionPlan` shapes
  - the compiler may reuse existing request-pattern logic, but the output must be a plan rather than a second task-only shortcut path
- Compilation failure is typed. The allowed failure classes in v1 are:
  - `unsupported_operation`
  - `missing_capability`
  - `budget_exceeded`
  - `template_mismatch`
- Compilation failure produces a controlled runtime failure before the execution state machine enters `running`.
- The runner executes plans in dependency order:
  - nodes whose dependencies are satisfied become runnable
  - runnable nodes execute sequentially in v1 unless they are explicitly assigned to the same `branch_group_id`
  - Phase 4 concurrency applies only to runnable nodes whose branch grouping has already been frozen by the plan
- Runnable-node ordering is deterministic:
  - outside a branch group, runnable nodes execute in plan declaration order and then `node_id`
  - inside a branch group, branches execute concurrently but merge order remains deterministic
- If a model helps compile an execution plan, require schema validation before the plan enters the runtime state machine.

## Phase 4: Replace Sequential Horizontal Mode with Real Branch Concurrency

- Add branch-level runtime objects such as:
  - `BranchPlan`
  - `BranchState`
  - `BranchBudget`
  - `BranchResult`
  - `BranchPublication`
  - `CancellationRecord`
- Refactor horizontal mode so branches run concurrently rather than as a sequential loop over isolated frames.
- Use `concurrent.futures.ThreadPoolExecutor` or an equivalent synchronous-friendly structured concurrency primitive for the MVP. Do not require an `asyncio` rewrite of the provider and tool stack in this workstream.
- Use the current isolated-state approach as the MVP branch-isolation baseline. If small runtime-owned snapshot helpers are needed for correctness, add only those helpers here. Do not pull Workstream 3 durability or generalized persistence work into this phase.
- The parent runtime creates the branch executor, submits branch work, collects futures, and joins all branches before merge. A fatal unrecoverable branch exception triggers sibling cancellation before merge.
- `BranchPlan` must include:
  - `branch_id`
  - `parent_frame_id`
  - `request_id`
  - `trace_context`
  - `assigned_node_ids`
  - `merge_priority`
  - `reserved_budget`
  - `cancel_on_parent_stop`
- `BranchBudget` must be reservation-based, not purely observational. At branch-launch time the parent allocates:
  - model-call budget slice
  - checker budget slice
  - latency slice
  - optional tool-synthesis allowance
- `BranchState` must include:
  - `branch_id`
  - `status`
  - `assigned_node_ids`
  - `publications`
  - `budget_consumed`
  - `cancellation_record`
  - `error`
- `BranchState.status` is restricted to:
  - `pending`
  - `running`
  - `completed`
  - `cancelled`
  - `failed`
- Branches may publish only at these boundaries:
  - after branch-plan validation
  - after completion of a `PlanNode`
  - after verifier completion
  - after tool or provider launch
  - after tool or provider completion
  - at terminal branch completion
- Parent state may consume only typed `BranchPublication` objects. Branch-local shell mutations remain invisible until publication is accepted.
- `BranchPublication` must include:
  - `publication_id`
  - `publication_kind`
  - `logical_key`
  - `sequence_no`
  - `accepted`
- Define publication types branches may emit:
  - candidate artifact
  - verifier evidence
  - trace rows
  - budget usage
  - handle or job references
  - cleanup or reconciliation records
- Preserve deterministic merge by sorting publications on stable keys such as:
  - explicit merge priority
  - verifier support
  - unresolved critical count
  - branch rank
  - branch ID
- Sibling visibility is fixed:
  - branches never see sibling-local shell state
  - branches may read only the parent snapshot plus accepted append-only board publications
- Conflict resolution is fixed in v1:
  - parent merge consumes only the latest accepted publication for a given logical key
  - artifact conflicts are resolved by deterministic sort order only
  - losing publications remain in trace history and checkpoint state but do not mutate the merged result
  - cancelled branches may publish cleanup and receipt-reconciliation records only
- Add explicit cancellation reasons:
  - fatal branch fault
  - budget exhaustion
  - superior branch dominance
  - verification failure
  - parent stop policy
  - external interrupt
- Cancellation cleanup is mandatory:
  - branch-owned async handles must be cancelled or reconciled before branch finalization
  - post-cancellation publications are ignored except cleanup, receipt, and reconciliation records
  - parent completion must fail closed if branch cleanup cannot establish a terminal branch state
- Branch-budget semantics are fixed:
  - reservations are hard ceilings unless the parent explicitly grants an escalation
  - unused reservation returns to the parent only at branch finalization
  - branch overspend without escalation is a typed runtime failure

## Phase 5: Complete Checkpoint and Resume Semantics

- Extend the existing `Checkpoint`, `CheckpointReference`, and file-backed checkpoint publication flow into restartable `CheckpointEnvelope` artifacts. Do not add a durable indexed store in this workstream.
- Expand checkpoints from summary objects into restartable `CheckpointEnvelope` contracts that cover:
  - `checkpoint_id`
  - `runtime_abi`
  - `storage_schema_version`
  - `runtime_hash`
  - `request_id`
  - `plan_id`
  - `task_id`
  - `seed`
  - queued frames
  - branch state
  - branch publications
  - unresolved goals
  - artifact refs
  - handle or job refs
  - budget state
  - verifier state
  - working state summary
  - trace cursor
  - side-effect receipts
- The canonical `CheckpointEnvelope` must carry stable semantic sections for identity, state, receipts, and trace cursor. JSON key ordering is not part of Workstream 2.
- Emit checkpoints at deterministic boundaries:
  - before branch fan-out
  - after branch-plan creation
  - after tool or provider launch
  - after tool or provider completion
  - before merge
  - after merge
  - before terminal result
- Define side-effect receipts for every non-deterministic or externally meaningful action:
  - `side_effect_id`
  - `action_fingerprint`
  - `idempotency_key`
  - action kind
  - branch ID
  - request digest
  - backend
  - status
  - `result_ref`
  - replay policy
  - reconciliation policy
  - created-at timestamp
- `SideEffectReceipt.action_kind` is restricted in v1 to:
  - `tool_launch`
  - `tool_completion`
  - `provider_request`
  - `provider_completion`
  - `service_action`
  - `filesystem_write`
- Upgrade the existing resume surface into a first-class runtime command implemented at `agintor/runtime_sdk/runtime_entry.py`. Do not keep a parallel host-only restore path.
- Checkpoint selection is fixed in v1:
  - if the user supplies `checkpoint_ref`, resume uses that checkpoint only
  - otherwise resume selects the latest checkpoint published for the request
- Resume request fields are fixed in v1:
  - `checkpoint_ref`
  - `request_id`
  - `trace_context`
  - `reconciliation_policy`
- `reconciliation_policy` is restricted to:
  - `strict`
  - `best_effort`
- On resume, the runtime must:
  - load the `CheckpointEnvelope`
  - validate `runtime_abi` and `storage_schema_version`
  - rebuild runtime state from queued frames, per-node status, branch state, accepted publications, artifacts, budget state, verifier state, and working-state summary
  - reuse the recorded receipt or result
  - reconcile via handle or job status
  - or fail closed with a typed recovery reason
- Receipts govern reissue:
  - if a receipt proves completion, reuse it and do not reissue
  - if a receipt proves launch without completion, reconcile before any reissue
  - if neither proof nor reconciliation is available, fail closed in `strict` mode
- Resume re-enters the runtime state machine at `running` with reconstructed state.
- Workstream 2 defines the resume contract and same-run file-backed checkpoint publication. Workstream 3 adds the durable indexed store, retention policy, and long-lived recovery surfaces.

## Phase 6: Harden Runtime-Wide Isolation

- Do not rebuild the existing ABI, kernel-version, backend, or required-env preflight that already exists in `agintor/runtime_loader.py` and `agintor/runtime_host.py`.
- Add only the missing `RuntimeIsolationPolicy` layer to the existing runtime plan, deployment contract, and capability exchange. Runtime execution must either satisfy it or fail before solve begins.
- Runtime-wide isolation is an upper bound on solve-time power. Later per-tool sandbox policies may only tighten it, never relax it.
- The policy contract must cover at least:
  - `timeout_envelope`
  - `workspace_root`
  - `environment_allowlist`
  - `network_policy` with `none` as the default
  - `filesystem_policy`
  - `required_guarantees`
  - `desired_guarantees`
- `RuntimeIsolationPolicy` must distinguish declarative intent from enforceable guarantees. The runtime host must reject a backend that cannot satisfy every required guarantee.
- Known guarantee keys for the MVP are:
  - `timeout_enforcement`
  - `workspace_isolation`
  - `environment_filtering`
  - `process_cleanup`
  - `network_disablement`
- Backend evaluation is two-step:
  - the host rejects unsupported required guarantees before launch
  - the runtime rechecks effective guarantees after launch and fails closed on mismatch
- `local` is the development backend and may claim only:
  - timeout envelope
  - explicit workspace root
  - environment allowlist filtering
  - best-effort process cleanup
- `local` may not claim:
  - guaranteed network disablement
- `docker` is the bounded backend whenever the deployment contract demands guarantees forbidden to `local`.
- Docker hardening beyond the guarantees above is deferred. Do not turn Workstream 2 into a broader container-hardening program.
- If a backend cannot honor the declared isolation policy, the runtime must reject execution with a contract error.

## Phase 7: Freeze Runtime Event and Failure Semantics

- The runtime state machine is fixed in v1:
  - `idle`
  - `compiling`
  - `validating`
  - `running`
  - `branching`
  - `merging`
  - `completing`
  - `completed`
  - `failed`
  - `cancelled`
- State transitions are fixed in v1:
  - `idle -> compiling` when a request or resume command is accepted
  - `compiling -> validating` when plan compilation succeeds
  - `validating -> running` when plan validation succeeds
  - `running -> branching` when runnable nodes fan out into a branch group
  - `branching -> merging` when all required branch results are terminal or cancelled
  - `running -> completing` when terminal nodes are reached without branching
  - `merging -> completing` when merge output becomes the terminal candidate artifact
  - `completing -> completed` when final verification or allowed best-effort completion succeeds
  - any active state -> `failed` on hard invalidation, unrecoverable receipt failure, or typed runtime failure
  - any active state -> `cancelled` on external interrupt or explicit parent-policy cancellation
- Resume enters at `idle`, loads a checkpoint during `compiling`, validates during `validating`, and then re-enters `running`.
- Add stable runtime events for:
  - `run_started`
  - `plan_loaded`
  - `plan_compiled`
  - `plan_validation_failed`
  - `node_started`
  - `node_completed`
  - `node_failed`
  - `branch_started`
  - `branch_cancelled`
  - `branch_completed`
  - `branch_failed`
  - `side_effect_recorded`
  - `checkpoint_published`
  - `checkpoint_restored`
  - `side_effect_reconciled`
  - `merge_started`
  - `merge_completed`
  - `terminal_emitted`
  - `run_failed`
  - `run_cancelled`
- Every event must carry the active plan's `plan_id` plus the propagated `trace_context`.
- Distinguish failure classes in runtime results and traces:
  - branch fault
  - controlled failure
  - verification failure
  - checkpoint failure
  - isolation failure
  - receipt reconciliation failure
  - recovery incompatibility
  - recovery failure
  - protocol failure
  - external interrupt
- Do not invent new user-facing terminal result classes here. Runtime terminal outcomes must map onto the `SolveResult` status and verification fields frozen in Workstream 1.
- Keep events bounded and structured so later workstreams can persist and analyze them without relying on raw logs.

## Regression Gates

- Add runtime tests that prove:
  - benchmark and prompt mode use the same state machine
  - eval and batch invocations carry stable per-invocation request IDs and trace context
  - concurrent branch execution is real
  - merge results are deterministic across completion-order variation
  - sibling cancellation works
  - checkpoints can be resumed after interruption
  - receipt-backed side effects are not re-executed on resume
  - Docker policy violations fail clearly
- Extend runtime-loader and protocol tests so isolation and capability mismatches fail before execution starts.

## Handoff to Workstream 3

- Workstream 3 receives:
  - a runtime-owned solve kernel
  - typed execution and branch contracts
  - typed runtime request and execution trace-correlation contracts
  - checkpoint envelopes
  - side-effect receipts
  - structured runtime events
  - a fail-closed isolation contract
- Workstream 3 must persist these objects without changing their solve-time meaning.

## Acceptance Gates

1. Benchmark-mode and prompt-mode execution flow through the same runtime state machine and entrypoint.
2. All external solve-time execution flows through the bundled runtime entrypoint instead of bypassing it.
3. Horizontal mode runs concurrent branches and yields deterministic merge outputs across repeated runs.
4. Branch cancellation is explicit and sibling cleanup leaves no orphaned runtime state.
5. Checkpoints are restartable artifacts rather than summary-only objects.
6. Side-effect receipts prevent blind re-execution of non-deterministic actions on resume.
7. Docker execution enforces explicit runtime-wide policy and fails closed on unsupported guarantees.
8. Runtime traces explain branching, checkpointing, merge, and failure behavior from structured events alone.
9. Benchmark-mode, prompt-mode, and batch-eval invocations materialize stable trace-correlation context with normalized request IDs and runtime identity before provider dispatch.

## File Ownership

- `agintor/runner.py`: runtime state machine, branch scheduler, checkpoint boundaries, receipt integration
- `agintor/schemas.py`: canonical typed contracts for execution plans, branching, checkpoints, isolation, receipts, and trace context
- `agintor/runtime_api.py`: runtime-local builders, adapters, request compilation helpers, `PolicyContext`, and `AgentFrame`
- `agintor/runtime_host.py`: request construction, normalized benchmark request identity, trace-context transport, and resume transport
- `agintor/shell.py`: solve-time state integration points and invariant checks
- `agintor/runtime_sdk/runtime_entry.py`: canonical runtime entrypoint and resume entrypoint
- `agintor/runtime_loader.py`: capability exchange, backend guarantee reporting, and preflight enforcement hooks
- `agintor/container_runtime.py`: runtime-wide backend isolation policy and backend preflight
- `templates/baseline_runtime/topology_policy.py`: solve-mode selection, branch proposal, deterministic merge hooks
- `tests/test_runtime_host.py`: extend protocol, request identity, resume, and backend-preflight coverage
- focused new runtime tests as needed for execution-plan validation, branch concurrency, checkpoint resume, and isolation policy

## Deferred

- Multi-host orchestration
- Process-image checkpointing
- Per-branch container orchestration beyond the runtime-wide boundary
- Service-style long-lived orchestration control planes
