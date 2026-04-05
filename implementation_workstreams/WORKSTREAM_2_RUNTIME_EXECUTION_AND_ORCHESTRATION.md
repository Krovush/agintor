# Workstream 2: Runtime Execution, Orchestration, and Isolation

## Outcome

- The exported runtime owns a real solve-time kernel instead of borrowing host-package internals.
- Benchmark tasks and bounded user requests execute through one runtime state machine built around a shared `ExecutionPlan` contract.
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
- Keep archive scheduling, objective selection, phase control, benchmark planning, verifier definition, and leader selection outside this workstream.
- Keep durable storage implementation details outside this workstream. This workstream defines what must be serializable and when checkpoints must be emitted, not how long-term storage is indexed.
- Keep per-tool asset packaging and provider feature completion outside this workstream. This workstream provides orchestration contracts those later systems consume.

## Non-Goals

- Distributed multi-host orchestration
- Open-ended interactive replanning outside bounded request adaptation
- Remote job queues or cloud schedulers
- Provider and tool lifecycle unification beyond the receipt and checkpoint semantics introduced here

## Baseline

- `agintor/runner.py` already runs a queue-driven solve loop with `single`, `vertical`, and `horizontal` modes.
- `agintor/runtime_api.py` already defines core runtime objects such as `RuntimeState`, `RuntimeBudget`, and `PolicyContext`.
- `agintor/runtime_api.py` already defines `SolveRequest` and `SolveResult`, but prompt-mode adaptation is still too close to benchmark-task compilation details.
- `agintor/shell.py` already owns the canonical solve-time substrate: message board, handles, memory, tools, predictors, and invariants.
- Horizontal execution is still effectively sequential because isolated workers run one after another with deep-copied state.
- Async handles exist, but most runtime flows immediately wait on them instead of exploiting overlap.
- Runtime-wide Docker execution exists, but it behaves more like packaging than like a strict execution policy.
- The control policy still exposes factory-owned methods that do not belong to solve-time runtime control.

## Core Decisions

- Keep the fixed shell plus four mutable policy files as the solve-time architecture. The change is to move the kernel under the bundled runtime boundary and make its semantics explicit.
- Use one runtime-native `ExecutionPlan` contract for both benchmark tasks and user requests.
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

## Phase 2: Remove Factory Leakage from Runtime Control

- Delete `score_interface_scope` and `update_scope_credit` from `templates/baseline_runtime/control_policy.py`.
- Update `agintor/prompt_builder.py` so the `ctl` contract contains only:
  - `assign_model`
  - `request_checks`
  - `stop_policy`
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
  - `BenchmarkTask -> ExecutionPlan`
  - `SolveRequest -> ExecutionPlan`
- The plan must carry at least:
  - origin kind
  - objective text
  - context references
  - file references
  - bounded operation nodes
  - verification mode
  - allowed tool categories
  - budget overrides
  - external-visibility flags
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
- Add explicit cancellation reasons:
  - fatal branch fault
  - budget exhaustion
  - superior branch dominance
  - verification failure
  - parent stop policy
  - external interrupt

## Phase 5: Define Checkpoints and Side-Effect Receipts

- Expand checkpoints from summary objects into restartable `CheckpointEnvelope` contracts that cover:
  - queued frames
  - branch state
  - unresolved goals
  - artifact refs
  - handle or job refs
  - budget state
  - verifier state
  - working memory summary
  - trace cursor
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
  - request digest
  - backend
  - success receipt
  - replay or reconciliation policy
- Add `resume` as a first-class runtime entrypoint that consumes checkpoint references instead of relying on ad hoc host-side restore behavior.
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
- Keep `local` as the development backend.
- Make `docker` the auditable bounded backend.
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

## File Ownership

- `agintor/runner.py`: runtime state machine, branch scheduler, checkpoint boundaries, receipt integration
- `agintor/runtime_api.py`: execution-plan, branch, checkpoint, and cancellation contracts
- `agintor/shell.py`: solve-time state integration points and invariant checks
- `agintor/runtime_sdk/`: bundled solve-time kernel modules
- `agintor/container_entry.py`: runtime entrypoint and resume entrypoint
- `agintor/container_runtime.py`: runtime-wide backend isolation policy and backend preflight
- `templates/baseline_runtime/topology_policy.py`: solve-mode selection, branch proposal, deterministic merge hooks
- `templates/baseline_runtime/control_policy.py`: solve-time-only control methods
- `agintor/prompt_builder.py`: mutator-visible contract cleanup

## Deferred

- Multi-host orchestration
- Process-image checkpointing
- Per-branch container orchestration beyond the runtime-wide boundary
- Service-style long-lived orchestration control planes
