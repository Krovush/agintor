# Workstream 2: Runtime Execution, Orchestration, And Isolation

## Outcome

- The runtime host must execute both benchmark tasks and bounded user-request solves through one orchestration path instead of assuming benchmark-only `task_id` execution.
- Single, vertical, and horizontal solve modes must remain, but horizontal mode must become real concurrent branch execution with deterministic merge, cancellation, and branch-level budget control.
- Checkpoints must become restartable runtime artifacts rather than summary-only byproducts. A stopped run must be resumable from serialized runtime state, not only replayable from scratch.
- Runtime-wide isolation must become part of the runtime contract. Local execution remains available for development, while Docker execution becomes a bounded, auditable runtime envelope with explicit resource and privilege limits.

## Boundaries

- Own the runtime state machine, branch scheduler, runtime-side request adaptation, checkpoint publication, resume semantics, and runtime-wide container isolation.
- Keep factory scheduling, objective selection, archive credit, interface scoring, and mutation acceptance in Workstream 4. The runtime may emit telemetry for those systems, but it must not update factory-side scheduler state directly.
- Keep durable storage backends, long-term memory persistence internals, and replay database design in Workstream 3. This workstream defines which runtime objects must be serializable and when checkpoints must be emitted.
- Keep per-tool sandbox hardening, promoted-tool asset lifecycle, and provider/runtime environment internals in Workstream 5. This workstream owns runtime-wide execution boundaries, not per-tool OS isolation.
- Keep the public CLI solve surface and exported-runtime solve contract aligned with Workstream 1. This workstream owns the runtime-side adapter that executes those requests after they enter the host.

## Non-Goals

- Do not expand the mutable runtime search surface beyond the four policy files. Runtime profiles, benchmark adapters, verifier bundles, and export assets are not solve-time mutation surfaces.
- Do not move benchmark planning, verifier adaptation, archive insertion, or phase scheduling into the runtime host.
- Do not start with distributed orchestration, cluster scheduling, or process-image checkpointing. The MVP must first stabilize single-host deterministic orchestration with serializable runtime state.

## Current Baseline

- `agintor/runner.py` already runs a real task-time state machine with queue-driven execution, single/vertical/horizontal modes, deterministic merge, verification requests, controlled failure, and checkpoint publication.
- `agintor/runtime_api.py` already defines `AgentFrame`, `RuntimeBudget`, `RuntimeState`, and `PolicyContext`, which is the correct nucleus for runtime-side orchestration state.
- `agintor/shell.py` already owns the canonical agent pool, short-term graph, long-term memory, message board, open-handle table, tool registry, tool executor, predictors, and runtime invariants.
- `agintor/templates/baseline_runtime/topology_policy.py` already chooses mode, proposes children, assigns tool scope, selects horizontal workers, merges worker outputs, and creates checkpoint objects.
- `agintor/templates/baseline_runtime/control_policy.py` still exposes legacy factory-adjacent methods `score_interface_scope` and `update_scope_credit`, which no longer belong inside the exported runtime control surface.
- Horizontal workers are still executed sequentially. `_execute_isolated_frame(..., isolate_runtime_state=True)` deep-copies runtime state and restores snapshots after each worker rather than running real overlapping branches.
- Async tool handles already exist, but the runner usually waits immediately after launch, so branch overlap and long-lived background work are still limited.
- `agintor/container_runtime.py` already runs runtimes inside Docker, but it currently acts as a packaging wrapper around the entire runtime batch rather than a hardened isolation policy with quotas and privilege restrictions.
- `agintor solve` is still benchmark-only, and `runner.py` still assumes execution starts from `BenchmarkTask.operations` rather than from a bounded runtime request envelope.

## Execution Model Decisions

- Keep the fixed shell and four mutable runtime policies as the core architecture. The orchestration plan must strengthen runtime execution without collapsing factory ownership boundaries.
- Use structured concurrency semantics for horizontal work. Branches should be launched, cancelled, and joined as a unit, and branch failure must trigger deterministic sibling cancellation and cleanup.
- Use serialized runtime-state checkpoints, not whole-process snapshots. Resume must reconstruct queued frames, branch state, handle state, verifier state, and budget state from durable artifacts.
- Keep deterministic merge independent of wall-clock completion order. Execution may become concurrent, but final merge order must remain a pure function of branch results and stable sort keys.
- Treat Docker as an execution boundary, not only a transport wrapper. Resource limits, privilege reduction, filesystem policy, environment allowlists, and network policy belong in the runtime execution contract.

## Phase 1: Remove Factory Leakage From The Runtime Control Surface

- Remove `score_interface_scope` and `update_scope_credit` from `agintor/templates/baseline_runtime/control_policy.py`.
- Update `agintor/prompt_builder.py` so the `ctl` method contract contains only solve-time methods: `assign_model`, `request_checks`, and `stop_policy`.
- Ensure the exported runtime no longer imports or depends on `ScopeScheduler` or any other factory-side scheduling type.
- Keep runtime telemetry rich enough that Workstream 4 can continue computing scope credit and counterfactual deltas outside the runtime.
- Verify that staged mutation, crossover, and runtime loading still work after the control-surface contraction.

`Exit gate:` runtime control policies own only solve-time behavior, mutator contracts match the target spec, and evolution still runs without runtime-owned scheduler writes.

## Phase 2: Add Runtime-Side Request Adaptation

- Introduce a bounded runtime execution-plan contract that can be created from both benchmark tasks and user-request solves.
- Keep benchmark execution compatible with the current `BenchmarkTask` path, but stop treating benchmark operations as the only entry format the runner can consume.
- Add a runtime-side adapter that converts a normalized solve request into:
  prompt/objective text,
  context items and file references,
  bounded operation steps or execution-plan nodes,
  verification preference,
  allowed tool categories,
  budget overrides,
  and external-visibility flags.
- Keep this adapter narrow. It should translate requests into the runtime's bounded operation model, not create a second benchmark-planning system inside solve.
- Thread verification mode through the execution loop so user-request solves can honestly report verified, partially checked, or best-effort outcomes.

`Exit gate:` the runner can execute both benchmark-originated and user-request-originated plans through the same state machine without requiring a benchmark `task_id`.

## Phase 3: Replace Sequential Horizontal Mode With Real Concurrent Branch Execution

- Add explicit branch-level runtime objects in `agintor/runtime_api.py` or an adjacent runtime module:
  `BranchPlan`,
  `BranchState`,
  `BranchResult`,
  and `CancellationRecord`.
- Refactor `agintor/runner.py` so horizontal workers are scheduled concurrently instead of in a for-loop that executes isolated branches one at a time.
- Introduce branch-level budget slices and cancellation reasons so orchestration can stop low-value or failed branches without corrupting global runtime state.
- Let async tool handles overlap with branch work. The scheduler must own when to poll, await, cancel, or fail branch-local background work instead of immediately waiting after launch.
- Preserve deterministic merge by sorting completed branch outputs on stable fields such as verifier support, predicted solve, unresolved-critical count, and worker ID, not on completion order.
- Extend trace rows so branch launch, cancellation, completion, merge inputs, and merge decisions are all inspectable.

`Exit gate:` horizontal mode runs real concurrent branches, failures cancel sibling work predictably, and repeated smoke runs still normalize to identical merge outputs and traces.

## Phase 4: Promote Checkpoints Into Resumeable Runtime State

- Expand checkpoints from summary objects into restartable runtime snapshots that include:
  queued frames,
  unresolved goals,
  visible tool names,
  branch status,
  artifact refs,
  open-handle refs,
  budget state,
  verifier state,
  and enough trace metadata to resume deterministically.
- Add a checkpoint manager that writes these snapshots to the runtime workspace and reloads them after interruption.
- Emit checkpoints at deterministic orchestration boundaries:
  before launching branch groups,
  after branch completion,
  before awaiting long-lived handles,
  after handle resolution,
  before irreversible verification,
  and on controlled stop/failure.
- Add resume reconciliation rules for async handles: completed, timed out, failed, orphaned, and non-resumable states must each have explicit behavior.
- Keep the first MVP recovery path filesystem-backed and local. Workstream 3 can later move the same state contract onto a more durable store.

`Exit gate:` an interrupted run can reload checkpoint artifacts, rebuild runtime state, and either continue safely or fail closed with a precise recovery reason.

## Phase 5: Harden Runtime-Wide Isolation

- Extend `agintor/container_runtime.py` and `agintor/container_entry.py` so Docker runs apply explicit runtime isolation policy rather than bare `docker run` defaults.
- Add runtime-wide controls for:
  CPU and memory quotas,
  PID limits,
  runtime timeout envelopes,
  read-only root filesystem plus explicit writable mounts,
  environment-variable allowlists,
  capability dropping,
  seccomp policy,
  no-new-privileges,
  and non-root or user-namespace execution where supported.
- Separate development and hardened execution clearly:
  `local` remains the developer convenience backend,
  `docker` becomes the bounded execution backend with declared isolation guarantees.
- Ensure mounted provider files, runtime bundles, and workspace paths remain minimal and explicit so exported runtimes do not inherit broad host filesystem visibility.
- Surface backend capability failures as contract errors instead of silent downgrade behavior.

`Exit gate:` Docker execution runs under explicit quotas and privilege restrictions, and exported runtimes can declare which isolation guarantees they require from the host backend.

## Phase 6: Tighten Orchestration Observability And Failure Semantics

- Add stable orchestration events for branch launch, branch cancellation, checkpoint publish, checkpoint restore, handle reconciliation, and resume outcome.
- Distinguish controlled failure, verification failure, isolation failure, and recovery failure in runtime results and traces.
- Make cancellation and resume decisions inspectable from trace artifacts without requiring the reader to infer them from low-level shell state.
- Keep observability payloads bounded and structured so they can feed evaluator reporting without leaking validation/test data into mutation prompts.

`Exit gate:` runtime traces and checkpoint artifacts are sufficient to explain why a run branched, stopped, resumed, or failed without reopening code.

## MVP Acceptance Sequence

1. The runtime control surface is reduced to solve-time methods only, and factory scheduler ownership is fully removed from exported runtime policies.
2. The runtime host can execute a bounded user-request plan and a benchmark task through the same orchestration path.
3. Horizontal mode runs concurrent branches with deterministic merge and explicit cancellation semantics.
4. Checkpoints are written as restartable runtime-state artifacts, and interrupted runs can resume from them.
5. Docker execution enforces explicit resource and privilege boundaries instead of acting only as a packaging shell.
6. Runtime traces and results expose branch, checkpoint, and failure semantics in a stable, machine-readable form.

## File Ownership

- `agintor/runner.py`: runtime state machine, branch scheduler, checkpoint boundaries, async-handle orchestration, resume hooks.
- `agintor/runtime_api.py`: runtime execution-plan, branch-state, checkpoint-state, and cancellation-state contracts.
- `agintor/shell.py`: message-board/open-handle integration points, invariant checks, and runtime-state restore boundaries.
- `agintor/templates/baseline_runtime/topology_policy.py`: solve-mode selection, child/worker proposal, deterministic merge logic, checkpoint construction inputs.
- `agintor/templates/baseline_runtime/control_policy.py`: solve-time model/check/stop policy only.
- `agintor/prompt_builder.py`: mutator-visible method contracts after the control-surface cleanup.
- `agintor/container_runtime.py`: runtime-wide Docker execution policy, quotas, mounts, backend preflight, isolation contract.
- `agintor/container_entry.py`: container-side runtime entrypoint, request hydration, checkpoint-aware batch execution.

## Deferred Until Post-MVP

- Multi-host or distributed branch orchestration.
- Process-image checkpointing or CRIU-style runtime capture.
- Fine-grained per-branch container execution beyond runtime-wide isolation.
- Rich orchestration UI or long-running service control plane.
- Cross-machine checkpoint portability beyond the bounded runtime/export contract.
