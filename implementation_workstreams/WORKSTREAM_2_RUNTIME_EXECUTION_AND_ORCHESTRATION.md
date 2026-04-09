# Workstream 2: Runtime Execution, Orchestration, and Isolation

## Outcome

- The exported runtime owns a real solve-time kernel instead of borrowing host-package internals.
- Benchmark tasks and bounded user requests execute through one runtime state machine built around a shared `ExecutionPlan` contract.
- Runtime requests, execution plans, frames, and branch publications carry a typed trace-correlation contract so every provider call can be attributed to one CLI session, runtime, task, request, frame, and operation.
- Horizontal mode becomes true concurrent branch execution with deterministic merge, explicit cancellation, and branch-level publication semantics.
- Checkpoints become restartable runtime artifacts with defined side-effect and recovery behavior.
- Docker becomes an enforceable runtime boundary with explicit resource, privilege, filesystem, and network policy.

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

- `agintor/runner.py` already runs a queue-driven solve loop with `single`, `vertical`, and `horizontal` modes.
- `agintor/runtime_api.py` already defines core runtime objects such as `RuntimeState`, `RuntimeBudget`, and `PolicyContext`.
- `agintor/runtime_api.py` already defines `SolveRequest` and `SolveResult`, but prompt-mode adaptation is still too close to benchmark-task compilation details.
- Runtime request schemas and `PolicyContext` do not yet carry a typed trace-correlation object, and benchmark-mode request identity is still too loosely assembled.
- `agintor/shell.py` already owns the canonical solve-time substrate: message board, handles, memory, tools, predictors, and invariants.
- Horizontal execution is still effectively sequential because isolated workers run one after another with deep-copied state.
- Async handles exist, but most runtime flows immediately wait on them instead of exploiting overlap.
- Runtime-wide Docker execution exists, but it behaves more like packaging than like a strict execution policy.
- The mutator-visible control contract is already narrowed to solve-time methods, but runtime-side contracts and traces do not yet make that ownership boundary explicit end to end.

## Core Decisions

- Keep the fixed shell plus four mutable policy files as the solve-time architecture. The change is to move the kernel under the bundled runtime boundary and make its semantics explicit.
- Freeze the next runtime protocol and storage versions at the start of this workstream:
  - `runtime_abi = agintor-runtime-abi-v4`
  - `storage_schema_version = agintor-storage-v2`
- Use one runtime-native `ExecutionPlan` contract for both benchmark tasks and user requests.
- Treat `OpenAITraceContext` as a runtime-native contract that travels with request and execution state. Provider metadata projections happen later and must not become the source of truth.
- Adopt the canonical `OpenAITraceContext` field set from `TRACE_AND_PLANNING_IMPROVEMENTS_PLAN.md`. Workstream 2 owns runtime-side propagation of:
  - `session_id`
  - `provider_role`
  - `build_id`
  - `runtime_hash`
  - `runtime_dir`
  - `task_id`
  - `seed`
  - `request_id`
  - `iteration`
  - `objective`
  - `touched_scope`
  - `agent_id`
  - `frame_role`
  - `worker_id`
  - `op_id`
  - `run_node_id`
- Normalize benchmark request identity as `benchmark.<task_id>.seed_<seed>` so benchmark-mode traces and runtime artifacts have stable request keys.
- `RuntimeBatchRequest` remains a transport envelope. Each `RuntimeTaskInvocation` inside it must carry its own normalized `request_id` plus invocation-level `trace_context`.
- The host owns:
  - CLI parsing
  - benchmark task lookup
  - prompt-file loading
  - `SolveRequest` construction
  - `RuntimeSolveRequest` and `RuntimeBatchRequest` construction
- The runtime kernel owns:
  - `RuntimeSolveRequest -> ExecutionPlan`
  - `RuntimeTaskInvocation -> ExecutionPlan`
  - plan validation
  - plan execution
- Use structured concurrency for horizontal work. Branch groups launch, cancel, and join as one unit.
- Use copy-in, publication-out semantics for branch isolation:
  - each branch starts from an isolated branch state snapshot
  - branches do not mutate parent shell state directly
  - branches may publish only typed outputs
  - parent merge happens once and in deterministic order
- Define side-effect receipts before the durable store lands. Every non-deterministic or externally meaningful action must have replay or reconciliation semantics.
- Make Docker contract enforcement fail closed. The runtime must not silently downgrade to weaker isolation than the deployment contract allows.

## Phase 1: Move Solve-Time Execution Behind the Runtime Kernel

- Move solve-time implementation ownership under the bundled runtime boundary, including:
  - runtime entrypoint
  - runner
  - shell
  - memory-graph integration points
  - runtime-side verifier execution hooks
  - tool and provider bridge hooks required for solve-time execution
- Keep the host responsible only for:
  - runtime launch
  - capability inspection
  - request transport
  - result collection
  - backend preflight
- Make the runtime entrypoint the only legal solve-time execution path for:
  - benchmark mode
  - prompt mode
  - resume mode
- Raise the runtime contract version here and carry that same version through the later trace-storage and provider-capture work. Do not introduce another ABI or storage bump for this feature line in later workstreams.

## Phase 2: Remove Factory Leakage from Runtime Control

- Keep `templates/baseline_runtime/control_policy.py` and `agintor/prompt_builder.py` aligned on the solve-time-only `ctl` contract:
  - `assign_model`
  - `request_checks`
  - `stop_policy`
- Do not reintroduce any factory-owned scope-scheduler or archive-credit methods into runtime control.
- Ensure solve-time runtime code no longer imports or depends on:
  - archive state
  - scope scheduler state
  - leader-selection logic
  - counterfactual credit logic
- Keep runtime telemetry rich enough that the factory can still compute those quantities outside the runtime boundary.

## Phase 3: Introduce a Runtime-Native Execution Plan

- Replace benchmark-task-first execution with explicit runtime plan objects such as:
  - `ExecutionPlan`
  - `PlanNode`
  - `VerificationPlan`
  - `PlanOrigin`
  - `ExecutionFlags`
- Compile both entry modes into the same contract:
  - host-side external input -> `RuntimeSolveRequest`
  - `RuntimeSolveRequest -> ExecutionPlan`
  - `RuntimeTaskInvocation -> ExecutionPlan`
- Freeze the exact `ExecutionPlan` v1 shape. It must include at least:
  - `plan_id`
  - `request_id`
  - `origin`
  - `objective`
  - `context_refs`
  - `file_refs`
  - `nodes`
  - `root_node_ids`
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
  - `args`
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
  - every non-root node dependency must reference an existing `node_id`
  - every `root_node_id` must exist in `nodes`
  - `VerificationPlan.terminal_nodes` must reference existing `node_id` values
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
- Runtime-side executions use `provider_role="runtime"`. Factory-side planning and search calls use `provider_role="factory"` in Workstream 4.
- Add helper builders that derive child, branch, and operation-level trace context from the parent request or frame instead of assembling dictionaries ad hoc.
- `RuntimeTaskInvocation.request_id` is mandatory for eval and batch execution. For benchmark invocations the host must set it to `benchmark.<task_id>.seed_<seed>`.
- For user requests, keep plan compilation bounded to validated templates such as:
  - direct answer
  - structured computation
  - file inspection
  - bounded repo patch
  - bounded service action
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
- Branches may publish only at these boundaries:
  - after branch-plan validation
  - after completion of a `PlanNode`
  - after verifier completion
  - after tool or provider launch
  - after tool or provider completion
  - at terminal branch completion
- Parent state may consume only typed `BranchPublication` objects. Branch-local shell mutations remain invisible until publication is accepted.
- Define publication types branches may emit:
  - candidate artifact
  - verifier evidence
  - proposed long-term-memory writes
  - proposed promoted-tool records
  - trace rows
  - budget usage
  - handle or job references
- Preserve deterministic merge by sorting publications on stable keys such as:
  - explicit merge priority
  - verifier support
  - unresolved critical count
  - branch rank
  - branch ID
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

## Phase 5: Define Checkpoints and Side-Effect Receipts

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
  - working memory summary
  - trace cursor
  - side-effect receipts
- The canonical `CheckpointEnvelope` JSON shape is:
  - top-level identity and compatibility fields first
  - one `state` object containing queued frames, branch state, unresolved goals, artifact refs, budget state, verifier state, and working-memory summary
  - one `receipts` array containing `SideEffectReceipt` rows
  - one `trace_cursor` object containing last persisted event offsets
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
  - action kind
  - branch ID
  - request digest
  - backend
  - status
  - success receipt
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
- `resume` is a first-class runtime command implemented at `agintor/runtime_sdk/runtime_entry.py` and consumes checkpoint references instead of relying on ad hoc host-side restore behavior.
- On resume, the runtime must:
  - reuse the recorded receipt or result
  - reconcile via handle or job status
  - or fail closed with a typed recovery reason

## Phase 6: Harden Runtime-Wide Isolation

- Add a `RuntimeIsolationPolicy` to the runtime plan and deployment contract. Runtime execution must either satisfy it or fail before solve begins.
- The runtime-wide contract must cover:
  - pinned image or base digest
  - CPU ceiling
  - memory ceiling
  - timeout envelope
  - PID limit
  - read-only root filesystem
  - explicit writable mounts only
  - environment allowlist
  - `no-new-privileges`
  - capability dropping
  - seccomp profile
  - non-root execution
  - network policy with `none` as the default
- `RuntimeIsolationPolicy` must distinguish declarative intent from enforceable guarantees. The runtime host must reject a backend that cannot satisfy every required guarantee.
- `local` is the development backend and may claim only:
  - timeout envelope
  - explicit workspace root
  - environment allowlist filtering
  - best-effort process cleanup
- `local` may not claim:
  - read-only root filesystem
  - seccomp enforcement
  - capability dropping
  - `no-new-privileges`
  - non-root container-style isolation
  - PID ceilings with kernel enforcement
  - guaranteed network disablement
- `docker` is the auditable bounded backend and is required whenever the deployment contract demands any of the guarantees forbidden to `local`.
- If a backend cannot honor the declared isolation policy, the runtime must reject execution with a contract error.

## Phase 7: Freeze Runtime Event and Failure Semantics

- Add stable runtime events for:
  - `run_started`
  - `plan_loaded`
  - `branch_started`
  - `branch_cancelled`
  - `branch_completed`
  - `side_effect_recorded`
  - `checkpoint_published`
  - `checkpoint_restored`
  - `side_effect_reconciled`
  - `merge_started`
  - `merge_completed`
  - `terminal_emitted`
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
2. The exported runtime kernel executes without importing shared host implementation modules.
3. Horizontal mode runs concurrent branches and yields deterministic merge outputs across repeated runs.
4. Branch cancellation is explicit and sibling cleanup leaves no orphaned runtime state.
5. Checkpoints are restartable artifacts rather than summary-only objects.
6. Side-effect receipts prevent blind re-execution of non-deterministic actions on resume.
7. Docker execution enforces explicit runtime-wide policy and fails closed on unsupported guarantees.
8. Runtime traces explain branching, checkpointing, merge, and failure behavior from structured events alone.
9. Benchmark-mode, prompt-mode, and batch-eval invocations materialize stable trace-correlation context with normalized request IDs and runtime identity before provider dispatch.

## File Ownership

- `agintor/runner.py`: runtime state machine, branch scheduler, checkpoint boundaries, receipt integration
- `agintor/runtime_api.py`: execution-plan, branch, checkpoint, and cancellation contracts
- `agintor/runtime_host.py`: request construction, normalized benchmark request identity, and request transport of trace context
- `agintor/shell.py`: solve-time state integration points and invariant checks
- `agintor/runtime_sdk/`: bundled solve-time kernel modules
- `agintor/runtime_sdk/runtime_entry.py`: canonical runtime entrypoint and resume entrypoint
- `agintor/container_entry.py`: Docker wrapper only if a separate wrapper remains necessary after runtime-entry consolidation
- `agintor/container_runtime.py`: runtime-wide backend isolation policy and backend preflight
- `templates/baseline_runtime/topology_policy.py`: solve-mode selection, branch proposal, deterministic merge hooks
- `templates/baseline_runtime/control_policy.py`: solve-time-only control methods
- `agintor/prompt_builder.py`: mutator-visible contract cleanup

## Deferred

- Multi-host orchestration
- Process-image checkpointing
- Per-branch container orchestration beyond the runtime-wide boundary
- Service-style long-lived orchestration control planes
